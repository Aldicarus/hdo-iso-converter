"""phases/mkv_reubicar.py — Encontrar el MKV analizado que cambió de carpeta.

La caché de «Consultar / Editar MKV» identifica cada MKV por su CONTENIDO —el
SHA del primer MB y el tamaño, `storage.compute_mkv_fingerprint`—, así que
moverlo no invalida su análisis. Lo que se queda atrás es la ruta guardada, y
de ella sale la disponibilidad que pinta la columna: la tarjeta salía «No
encontrado», sin botón de abrir, sobre un análisis válido. Y es el flujo
NORMAL desde que CMv4.0 y Multiplexar dejan el análisis hecho: la caché se
escribe con el MKV en Output y el usuario lo mueve a su biblioteca acto
seguido. Hasta ahora solo lo arreglaba abrir el MKV desde su sitio nuevo con
«Abrir MKV» (`analyze_mkv` reapunta en el cache hit), y nada lo decía.

Aquí se busca por contenido en las tres carpetas que la app puede abrir
(`paths.LIBRARY_ROOTS`) y, si aparece, `storage.repoint_mkv_cache` pone la
ruta al día. Medido en el NAS el 2026-10-27: la columna tenía 10 MKV y 3 «no
encontrados», los tres de Output y con el análisis extendido del precache de
CMv4.0; dos estaban en la biblioteca con el tamaño exacto, y el tercero no es
un movimiento —el fichero analizado ya no existe—. La biblioteca son 212
carpetas y 1.639 MKV, y recorrerla solo por metadatos tarda ~0,2 s.

Decisiones que no son obvias:

* **Por tamaño exacto y huella, nunca por nombre.** El tamaño es un dato
  medido de un fichero de decenas de GB, así que filtra sin leer nada, y la
  huella (1 MB del candidato) confirma que es el MISMO contenido. El nombre no
  sirve: se renombra al mover, un MKV editado con el mismo nombre no puede
  quedarse con el análisis viejo, y en la biblioteca del NAS hay **4.733
  ficheros «.mkv» dentro de `.@__thumb`** —las miniaturas de QNAP— frente a
  1.639 MKV de verdad.
* **Las carpetas de sistema no se recorren**: ocultas, papeleras y snapshots
  (`.`, `@`, `#`, `$` y `paths.LIBRARY_HIDDEN_DIRS`). Una copia en un snapshot
  tiene el mismo contenido, y reapuntar ahí daría por vivo un fichero borrado.
  Tampoco se siguen los enlaces simbólicos: la ruta tiene que poder abrirse
  después, y eso exige que viva bajo una de las tres carpetas.
* **«No está» solo se concluye habiéndolo mirado TODO**: el recorrido llegó al
  final, ninguna carpeta (ni raíz) faltó o no se dejó leer, y se comprobaron
  todos los candidatos del tamaño. Si no, el resultado es «no se sabe», que ni
  entra en la memoria, ni deja al relato afirmar que se buscó, ni permite a
  Limpieza ofrecer borrar (revisión de Codex del 2026-10-27).
* **Lo que no aparece se recuerda `REBUSCAR_TRAS_S`.** La columna se pide al
  entrar en la pestaña y tras cada análisis; sin memoria, un MKV borrado de
  verdad recorrería las tres carpetas en cada petición. Un MKV que acaba de
  desaparecer no está en la memoria, así que se busca en la primera petición
  —es justo el que el usuario acaba de mover— y `forzar` (el botón «Volver a
  buscar» y el escaneo de Limpieza) se la salta.
* **Una entrada que decía estar en la ruta nueva con OTRO contenido se
  busca también** (solo al reapuntar). La huella acaba de demostrar que en esa
  ruta hay otro fichero, así que su contenido o se movió o ya no existe: si
  aparece en otra carpeta se reapunta, y solo si se concluye que no está se
  olvida —la regla de `storage.invalidate_mkv_cache_by_path`—. Pasa al
  sustituir en la biblioteca una versión vieja por la nueva con el mismo
  nombre; sin esto la columna enseñaba dos tarjetas con la misma ruta, y
  «Borrar» en una (que va por ruta) se llevaba las dos. Olvidarla sin
  buscarla borraba el análisis de un MKV que solo se había archivado en otra
  carpeta.
* **`buscar` no escribe nada**, para el escaneo de Limpieza, que es de solo
  lectura; **`reubicar`** busca y reapunta, para la columna.

No lanza por un fichero: una carpeta ilegible o un candidato que desaparece
a mitad dejan la búsqueda sin concluir. Recorre disco, así que quien lo llame
desde una corrutina, a un thread.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import paths

_logger = logging.getLogger(__name__)

#: Cuánto vale un «no está en ninguna carpeta» antes de volver a buscar.
REBUSCAR_TRAS_S = 600.0
#: Tope del recorrido de las tres carpetas. Medido, el de la biblioteca del NAS
#: son ~0,2 s con los metadatos en la ARC: esto es para un pool parado, no
#: para el caso normal.
TOPE_RECORRIDO_S = 20.0
#: Candidatos del mismo tamaño que se comprueban como mucho por MKV. Con
#: ficheros de decenas de GB coincidir al byte es casi imposible; el tope es
#: para que un caso raro (muestras pequeñas repetidas) no lea de más, y
#: pasarse de él deja la búsqueda sin concluir.
MAX_CANDIDATOS = 5

_PREFIJOS_DE_SISTEMA = (".", "@", "#", "$")

# Huella → instante (monotónico) de la última búsqueda CONCLUYENTE sin éxito.
_no_encontrados: dict[str, float] = {}
_cerrojo = threading.Lock()


@dataclass
class Busqueda:
    """Lo que dio una búsqueda.

    `encontrados`: huella → (ruta nueva, fingerprint del fichero encontrado).
    `no_encontrados`: huellas buscadas en esta llamada que NO están en ninguna
    de las tres carpetas, con todo mirado. Lo buscado sin poder concluir no
    está en ninguno de los dos.
    `descartadas`: entradas olvidadas porque su ruta tiene otro contenido y el
    suyo no está en ninguna carpeta (solo las rellena `reubicar`).
    """
    encontrados: dict[str, tuple[str, dict]] = field(default_factory=dict)
    no_encontrados: set[str] = field(default_factory=set)
    descartadas: set[str] = field(default_factory=set)


def olvidar() -> None:
    """Vacía la memoria de búsquedas sin éxito. La usan los tests: la memoria
    es de proceso y sobreviviría de uno a otro."""
    with _cerrojo:
        _no_encontrados.clear()


def buscado_sin_exito(sha: str) -> bool:
    """¿La última búsqueda de este MKV miró las tres carpetas enteras sin
    encontrarlo?"""
    return sha in _no_encontrados


def _buscables(entradas: list[dict]) -> list[dict]:
    """Las entradas legibles con lo que hace falta para buscarlas."""
    return [e for e in entradas
            if not e.get("corrupt")
            and isinstance(e.get("original_file_path"), str) and e["original_file_path"]
            and isinstance(e.get("fingerprint_sha"), str) and e["fingerprint_sha"]
            and isinstance(e.get("mkv_size_bytes"), int) and e["mkv_size_bytes"] > 0]


def _ausentes(entradas: list[dict]) -> list[dict]:
    """Las entradas legibles cuyo MKV no está en la ruta guardada."""
    out = []
    for e in _buscables(entradas):
        try:
            if Path(e["original_file_path"]).exists():
                continue
        except OSError:
            continue
        out.append(e)
    return out


def _es_de_sistema(nombre: str) -> bool:
    return nombre[:1] in _PREFIJOS_DE_SISTEMA or nombre in paths.LIBRARY_HIDDEN_DIRS


def _mkv_por_tamano(raices: list[str], tamanos: set[int],
                    tope_s: float) -> tuple[dict[int, set[str]], bool]:
    """`{tamaño: rutas}` de los `.mkv` de las raíces cuyo tamaño está en
    `tamanos`, y si se miró todo (sin tope, sin carpetas ni entradas que no se
    dejaran leer)."""
    hallados: dict[int, set[str]] = {}
    completo = True
    limite = time.monotonic() + tope_s
    pendientes = list(raices)
    while pendientes:
        if time.monotonic() > limite:
            _logger.warning("[reubicar] recorrido cortado a los %.0f s; quedan %d carpetas",
                            tope_s, len(pendientes))
            return hallados, False
        carpeta = pendientes.pop()
        try:
            with os.scandir(carpeta) as it:
                for e in it:
                    if _es_de_sistema(e.name):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            pendientes.append(e.path)
                        elif (e.name.lower().endswith(".mkv")
                              and e.is_file(follow_symlinks=False)):
                            tamano = e.stat(follow_symlinks=False).st_size
                            if tamano in tamanos:
                                hallados.setdefault(tamano, set()).add(e.path)
                    except OSError as err:
                        # Lo que no se ha podido mirar puede ser justo lo que se busca.
                        _logger.info("[reubicar] no se puede mirar %s: %s", e.path, err)
                        completo = False
        except OSError as err:
            _logger.info("[reubicar] no se puede leer %s: %s", carpeta, err)
            completo = False
    return hallados, completo


def _orden(ruta: str, nombre: str, raices: list[str]) -> tuple:
    """El candidato más probable primero: mismo nombre, después por carpeta
    (biblioteca, Output, Downloaded) y por ruta, para que sea determinista."""
    puesto = next((i for i, r in enumerate(raices)
                   if ruta.startswith(r.rstrip("/") + "/")), len(raices))
    return (Path(ruta).name != nombre, puesto, ruta)


def _buscar_por_contenido(objetivo: list[dict], *, forzar: bool) -> Busqueda:
    """El núcleo de `buscar`, sobre una lista de entradas ya elegida (estén o
    no en su ruta). Lleva el cerrojo: dos peticiones a la vez no recorren las
    carpetas dos veces, y la segunda ve lo que concluyó la primera."""
    from storage import compute_mkv_fingerprint

    res = Busqueda()
    with _cerrojo:
        if not forzar:
            ahora = time.monotonic()
            objetivo = [e for e in objetivo
                        if ahora - _no_encontrados.get(e["fingerprint_sha"], float("-inf"))
                        >= REBUSCAR_TRAS_S]
        if not objetivo:
            return res
        raices = [str(r) for r in paths.LIBRARY_ROOTS.values()]
        presentes = [r for r in raices if os.path.isdir(r)]
        hallados, completo = _mkv_por_tamano(
            presentes, {e["mkv_size_bytes"] for e in objetivo}, TOPE_RECORRIDO_S)
        # Una carpeta de la app que no está (un montaje caído) es una carpeta
        # sin mirar, no una carpeta vacía.
        completo = completo and len(presentes) == len(raices)
        for e in objetivo:
            sha, tamano = e["fingerprint_sha"], e["mkv_size_bytes"]
            nombre = Path(e["original_file_path"]).name
            candidatos = sorted(hallados.get(tamano, ()),
                                key=lambda r: _orden(r, nombre, raices))
            concluye = completo and len(candidatos) <= MAX_CANDIDATOS
            for c in candidatos[:MAX_CANDIDATOS]:
                try:
                    fp = compute_mkv_fingerprint(c)
                except OSError as err:
                    _logger.info("[reubicar] no se puede leer %s: %s", c, err)
                    concluye = False
                    continue
                if (fp and fp.get("sha256_1mb") == sha
                        and fp.get("size_bytes") == tamano):
                    res.encontrados[sha] = (c, fp)
                    break
            if sha in res.encontrados or not concluye:
                # Encontrado, o sin poder saberlo: ninguno de los dos es un «no está».
                _no_encontrados.pop(sha, None)
            else:
                res.no_encontrados.add(sha)
                _no_encontrados[sha] = time.monotonic()
    return res


def buscar(entradas: list[dict], *, forzar: bool = False) -> Busqueda:
    """Busca por contenido las entradas cuyo MKV ya no está en su ruta. No
    escribe nada en la caché.

    `entradas` es lo que devuelve `storage.list_mkv_audit_entries()`, que ya
    trae la huella (`fingerprint_sha`) y el tamaño del MKV (`mkv_size_bytes`).
    Sin `forzar`, lo que se concluyó hace menos de `REBUSCAR_TRAS_S` que no
    está no se vuelve a buscar.
    """
    ausentes = _ausentes(entradas)
    if not ausentes:
        return Busqueda()
    return _buscar_por_contenido(ausentes, forzar=forzar)


def reubicar(entradas: list[dict], *, forzar: bool = False) -> Busqueda:
    """`buscar` y, lo que aparezca, reapuntado en la caché.

    Las entradas que decían estar en una ruta donde ha aparecido otro
    contenido se buscan también: se reapuntan si están en otra carpeta y se
    olvidan solo si se concluye que no están (ver el docstring del módulo).
    """
    from storage import invalidate_mkv_cache, repoint_mkv_cache

    res = buscar(entradas, forzar=forzar)
    rutas_nuevas = {ruta: sha for sha, (ruta, _fp) in res.encontrados.items()}
    desplazadas = [e for e in _buscables(entradas)
                   if e["original_file_path"] in rutas_nuevas
                   and e["fingerprint_sha"] != rutas_nuevas[e["original_file_path"]]
                   and e["fingerprint_sha"] not in res.encontrados]
    if desplazadas:
        otra = _buscar_por_contenido(desplazadas, forzar=True)
        res.encontrados.update(otra.encontrados)
        for sha in otra.no_encontrados:
            if invalidate_mkv_cache(sha):
                res.descartadas.add(sha)
                _logger.info("[reubicar] olvidado el análisis de %s: su ruta tiene "
                             "otro contenido y el suyo no está en ninguna carpeta", sha[:12])
    for sha, (ruta, fp) in res.encontrados.items():
        repoint_mkv_cache(fp, ruta)
        _logger.info("[reubicar] %s estaba en otra carpeta: %s", Path(ruta).name, ruta)
    return res
