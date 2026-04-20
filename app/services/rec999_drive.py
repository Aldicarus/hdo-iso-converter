"""
rec999_drive.py — Listado y descarga de RPUs del repositorio de R3S3T_9999
en Google Drive, vía Drive API v3 con API key.

La carpeta tiene permisos "cualquiera con el enlace puede ver", por lo
que basta una API key (sin OAuth) para:
  - listar recursivamente todos los `.bin`
  - descargar cualquiera por file_id

Caché del listado en memoria + disco (`/config/rec999_drive_cache.json`),
TTL 24h. Así el listado solo pide a Google una vez al día.

Descarga streaming a disco con fallback para la pantalla anti-virus de
Google Drive en ficheros >100MB (infrecuente con RPUs de ~40MB).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx
from pydantic import BaseModel

from services.settings_store import (
    get_google_api_key,
    get_cmv40_drive_folder_id,
    parse_drive_folder_id,
)

_logger = logging.getLogger(__name__)

# El folder ID ya NO está hardcoded — el usuario debe configurarlo en la UI
# (el acceso al repo de REC_9999 requiere donación previa al autor).
# Se lee en runtime via get_cmv40_drive_folder_id() para que cambios en la
# UI se apliquen inmediatamente sin reiniciar el backend.

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CACHE_PATH = CONFIG_DIR / "rec999_drive_cache.json"
CACHE_TTL_SECONDS = 24 * 3600  # 24h

DRIVE_API  = "https://www.googleapis.com/drive/v3"
PAGE_SIZE  = 1000
REQUEST_TIMEOUT = 20.0


class DriveFile(BaseModel):
    id: str
    name: str                 # nombre del fichero
    path: str                 # ruta relativa desde la carpeta raíz ("sub1/sub2/x.bin")
    size_bytes: int = 0
    modified_time: str = ""


# Caché en memoria
_cache_files: list[DriveFile] | None = None
_cache_fetched_at: float = 0.0


# ── Listado recursivo ──────────────────────────────────────────────────

async def _list_children(client: httpx.AsyncClient, api_key: str,
                         folder_id: str) -> list[dict]:
    """Pagina todos los hijos (ficheros + subcarpetas) de una carpeta."""
    all_items: list[dict] = []
    page_token: str | None = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "key": api_key,
            "fields": "nextPageToken,files(id,name,size,mimeType,modifiedTime)",
            "pageSize": str(PAGE_SIZE),
            "orderBy": "name",
        }
        if page_token:
            params["pageToken"] = page_token
        resp = await client.get(f"{DRIVE_API}/files", params=params)
        if resp.status_code == 403:
            raise PermissionError(
                "Drive API denegó la petición (403). Comprueba que la API "
                "key tenga habilitada 'Drive API' y sin restricciones HTTP "
                "que bloqueen tu despliegue."
            )
        resp.raise_for_status()
        data = resp.json()
        all_items.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return all_items


async def _walk(client: httpx.AsyncClient, api_key: str,
                folder_id: str, prefix: str = "",
                depth: int = 0, max_depth: int = 5) -> list[DriveFile]:
    """Recorre recursivamente la carpeta. Solo extrae `.bin`."""
    if depth > max_depth:
        return []
    items = await _list_children(client, api_key, folder_id)
    files: list[DriveFile] = []
    for it in items:
        name = it.get("name", "")
        mime = it.get("mimeType", "")
        rel = f"{prefix}{name}" if not prefix else f"{prefix}/{name}"
        if mime == "application/vnd.google-apps.folder":
            sub = await _walk(client, api_key, it["id"], rel, depth + 1, max_depth)
            files.extend(sub)
        elif name.lower().endswith(".bin"):
            try:
                size = int(it.get("size", "0") or 0)
            except ValueError:
                size = 0
            files.append(DriveFile(
                id=it["id"],
                name=name,
                path=rel,
                size_bytes=size,
                modified_time=it.get("modifiedTime", ""),
            ))
    return files


def _save_cache(files: list[DriveFile]) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": time.time(),
            "folder_id": get_cmv40_drive_folder_id(),
            "files": [f.model_dump() for f in files],
        }
        CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except OSError as e:
        _logger.warning("No pude persistir cache Drive: %s", e)


def _load_cache() -> list[DriveFile] | None:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("folder_id") != get_cmv40_drive_folder_id():
        return None
    return [DriveFile(**f) for f in data.get("files", [])]


def is_configured() -> bool:
    """Requiere API key Y folder ID. Sin ambos no tiene sentido listar."""
    return bool(get_google_api_key()) and bool(get_cmv40_drive_folder_id())


def is_folder_configured() -> bool:
    return bool(get_cmv40_drive_folder_id())


async def list_bin_files(force_refresh: bool = False) -> list[DriveFile]:
    """Listado recursivo de `.bin` en la carpeta de REC_9999.
    Lanza `PermissionError` si la API key no tiene permisos suficientes.
    """
    global _cache_files, _cache_fetched_at

    now = time.time()
    if (not force_refresh and _cache_files is not None
            and now - _cache_fetched_at < CACHE_TTL_SECONDS):
        return _cache_files

    api_key = get_google_api_key()
    if not api_key:
        # Sin key, intentamos leer de disco (puede estar rancio pero útil)
        disk = _load_cache()
        if disk:
            _cache_files = disk
            _cache_fetched_at = now
            return disk
        return []

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            files = await _walk(client, api_key, get_cmv40_drive_folder_id())
    except PermissionError:
        raise
    except Exception as e:
        _logger.warning("Drive list falló: %s — intento cache en disco", e)
        disk = _load_cache()
        if disk:
            _cache_files = disk
            _cache_fetched_at = now
            return disk
        return []

    _cache_files = files
    _cache_fetched_at = now
    _save_cache(files)
    _logger.info("Drive REC_9999: %d .bin indexados", len(files))
    return files


# ── Test de API key ────────────────────────────────────────────────────

async def test_api_key(api_key: str) -> tuple[bool, str]:
    """Valida la Google API key probando AMBAS APIs: Drive (listar carpeta
    de REC_9999) y Sheets v4 (leer el sheet de recomendaciones).
    Devuelve (ok, mensaje) — ok=True si al menos Drive funciona."""
    key = api_key.strip()
    if not key:
        return False, "API key vacía"

    # Import tardío para evitar ciclos de importación
    from services.settings_store import get_cmv40_sheet_id_gid
    _SHEET_ID, _ = get_cmv40_sheet_id_gid()

    folder_id = get_cmv40_drive_folder_id()
    drive_ok = False
    drive_msg = ""
    sheets_ok = False
    sheets_msg = ""

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            # ── Drive API ────────────────────────────────────────
            if not folder_id:
                drive_msg = "Drive ⏭ (folder no configurado — saltado)"
                drive_ok = True  # no falla si no hay folder
            else:
                resp = await client.get(f"{DRIVE_API}/files", params={
                    "q": f"'{folder_id}' in parents and trashed=false",
                    "key": key,
                    "fields": "files(id)",
                    "pageSize": "1",
                })
                if resp.status_code == 200:
                    drive_ok = True
                    drive_msg = "Drive ✓"
                else:
                    try:
                        emsg = resp.json().get("error", {}).get("message", "")
                    except Exception:
                        emsg = ""
                    if "has not been used" in emsg or "disabled" in emsg:
                        drive_msg = "Drive ✗ (API no habilitada)"
                    elif resp.status_code == 403:
                        drive_msg = f"Drive ✗ (403: {emsg[:80]})"
                    elif resp.status_code == 400:
                        drive_msg = "Drive ✗ (petición mal formada — key inválida?)"
                    else:
                        drive_msg = f"Drive ✗ ({resp.status_code})"

            # ── Sheets API v4 ────────────────────────────────────
            # Primero probamos con un spreadsheet genérico público para
            # aislar "API habilitada?" de "este sheet específico funciona?".
            # Si la key puede acceder al sheet de Google "Getting Started"
            # (ID público estable de Google), la API está bien habilitada.
            GENERIC_SHEET = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
            sresp = await client.get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{GENERIC_SHEET}",
                params={"key": key, "fields": "spreadsheetId"},
            )
            if sresp.status_code == 200:
                sheets_ok = True
                # La API funciona — probamos también el sheet de DoviTools
                # para distinguir "todo perfecto" de "XLSX importado".
                dresp = await client.get(
                    f"https://sheets.googleapis.com/v4/spreadsheets/{_SHEET_ID}",
                    params={"key": key, "fields": "spreadsheetId"},
                )
                if dresp.status_code == 200:
                    sheets_msg = "Sheets ✓"
                else:
                    try:
                        demsg = dresp.json().get("error", {}).get("message", "")
                    except Exception:
                        demsg = ""
                    if "not supported for this document" in demsg.lower():
                        sheets_msg = ("Sheets ✓ (API OK — el sheet DoviTools es "
                                       "XLSX importado, se lee vía fallback openpyxl)")
                    else:
                        sheets_msg = f"Sheets ✓ (pero sheet DoviTools dio {dresp.status_code})"
            else:
                try:
                    emsg = sresp.json().get("error", {}).get("message", "")
                except Exception:
                    emsg = ""
                if "has not been used" in emsg or "disabled" in emsg:
                    sheets_msg = ("Sheets ✗ (API no habilitada en tu proyecto — "
                                   "actívala en console.cloud.google.com/apis/library/sheets.googleapis.com)")
                elif sresp.status_code == 403:
                    if "restriction" in emsg.lower() or "referer" in emsg.lower():
                        sheets_msg = ("Sheets ✗ (key con restricciones — permite HTTP referers genéricos "
                                       "o añade la API Sheets a la lista de APIs permitidas)")
                    else:
                        sheets_msg = f"Sheets ✗ (403: {emsg[:80]})"
                else:
                    sheets_msg = f"Sheets ✗ ({sresp.status_code}: {emsg[:80]})"
    except Exception as e:
        return False, f"Error de red: {e}"

    composite = f"{drive_msg} · {sheets_msg}"
    if drive_ok and sheets_ok:
        return True, f"API key válida — {composite}"
    if drive_ok:
        return True, f"API key OK para descargas, pero {sheets_msg} (habilítala para ver los enlaces del sheet)"
    return False, composite


async def test_folder_access(folder_url_or_id: str, api_key: str = "") -> tuple[bool, str, int]:
    """Prueba que un folder URL/ID de Drive es accesible con la API key dada
    (o la configurada si no se pasa). Devuelve (ok, mensaje, bin_count_sample).

    bin_count_sample: nº de .bin encontrados en la muestra (pageSize=50).
    Útil para confirmar al usuario que el folder es el correcto.
    """
    from services.settings_store import parse_drive_folder_id
    folder_id = parse_drive_folder_id(folder_url_or_id)
    if not folder_id:
        return False, "URL/ID de carpeta no válido (formato esperado: https://drive.google.com/drive/folders/XXX o el ID)", 0

    key = (api_key or get_google_api_key()).strip()
    if not key:
        return False, "Necesitas una Google API key configurada antes de probar el folder", 0

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{DRIVE_API}/files", params={
                "q": f"'{folder_id}' in parents and trashed=false",
                "key": key,
                "fields": "files(id,name,mimeType)",
                "pageSize": "50",
            })
        if resp.status_code != 200:
            try:
                emsg = resp.json().get("error", {}).get("message", "")
            except Exception:
                emsg = ""
            if resp.status_code == 404:
                return False, f"Folder no encontrado (404). Verifica la URL. {emsg[:80]}", 0
            if resp.status_code == 403:
                return False, f"Acceso denegado (403). Permisos del folder o API key restringida. {emsg[:100]}", 0
            return False, f"Error Drive ({resp.status_code}): {emsg[:120]}", 0

        items = resp.json().get("files", [])
        bin_count = sum(1 for it in items if it.get("name", "").lower().endswith(".bin"))
        folder_count = sum(1 for it in items
                           if it.get("mimeType") == "application/vnd.google-apps.folder")
        # Si no hay nada, es sospechoso — puede que el folder sea incorrecto o esté vacío
        if not items:
            return False, "Folder accesible pero vacío. ¿Es la URL correcta del repo DoviTools?", 0
        # Mensaje con muestra
        return True, (f"Folder accesible — {len(items)} entradas en la muestra "
                      f"({bin_count} .bin, {folder_count} subcarpetas). "
                      f"El listado completo se indexará al usar el repo."), bin_count
    except Exception as e:
        return False, f"Error de red consultando Drive: {e}", 0


async def test_sheet_access(sheet_url: str) -> tuple[bool, str, int]:
    """Prueba que un sheet URL es accesible (CSV export). Devuelve (ok, mensaje, row_count)."""
    from services.settings_store import parse_sheet_id_gid
    sid, gid = parse_sheet_id_gid(sheet_url)
    if not sid:
        return False, "URL de sheet no válida (formato esperado: https://docs.google.com/spreadsheets/d/XXX/edit#gid=YYY)", 0
    csv_url = (f"https://docs.google.com/spreadsheets/d/{sid}"
               f"/export?format=csv&gid={gid}")
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(csv_url)
        if resp.status_code != 200:
            if resp.status_code == 404:
                return False, f"Sheet o pestaña no encontrada (404). Verifica el sheet ID y el GID.", 0
            if resp.status_code == 403:
                return False, f"Acceso denegado (403). El sheet debe ser público (compartir con 'cualquiera con el enlace').", 0
            return False, f"Error ({resp.status_code}) descargando el sheet", 0
        text = resp.text
        if not text.strip():
            return False, "Sheet accesible pero vacío", 0
        lines = [l for l in text.split("\n") if l.strip()]
        return True, (f"Sheet accesible — {len(lines)} filas en CSV export. "
                      f"El parser completo se ejecuta al usar las recomendaciones."), len(lines)
    except Exception as e:
        return False, f"Error de red consultando el sheet: {e}", 0


# ── Descarga ───────────────────────────────────────────────────────────

async def download_file(file_id: str, dest_path: Path,
                        progress_cb=None) -> int:
    """Descarga streaming de un file_id de Drive a `dest_path`.
    Devuelve bytes escritos. Llama `progress_cb(bytes_so_far, total_or_None)`
    periódicamente si se pasa.
    """
    api_key = get_google_api_key()
    if not api_key:
        raise RuntimeError("No hay Google API key configurada")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    url = f"{DRIVE_API}/files/{file_id}"
    params = {"alt": "media", "key": api_key}

    bytes_written = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=20.0)) as client:
        async with client.stream("GET", url, params=params) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                raise RuntimeError(
                    f"Drive download falló ({resp.status_code}): {text[:200]!r}"
                )
            total_s = resp.headers.get("content-length")
            total = int(total_s) if total_s and total_s.isdigit() else None
            with dest_path.open("wb") as fp:
                async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                    fp.write(chunk)
                    bytes_written += len(chunk)
                    if progress_cb:
                        try:
                            await progress_cb(bytes_written, total)
                        except Exception:
                            pass
    return bytes_written
