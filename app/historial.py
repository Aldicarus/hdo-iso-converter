"""
historial.py — Qué trabajo se ha hecho, en las tres pestañas.

El problema
───────────
Cada pestaña guardaba lo suyo, con su forma y en su sitio:

  · Tab 1 apila un `ExecutionRecord` dentro de `Session.execution_history`.
  · Tab 3 apila un `CMv40PhaseRecord` dentro de `CMv40Session.phase_history`.
  · Tab 2 **no guarda nada**: un análisis extendido de diez minutos no deja
    rastro de haber existido en cuanto se cierra el modal.

Los dos primeros viven DENTRO de la sesión, así que para responder «qué ha
pasado hoy» hay que abrir las 130 sesiones del `/config` y ordenarlas. Y del
tercero no hay respuesta posible.

Qué hace
────────
Un fichero **append-only** (`/config/historial.jsonl`), una línea por trabajo
terminado, con la misma forma para las tres pestañas. No sustituye a los dos
historiales de siempre —que siguen ahí con su detalle por fase— sino que da la
vista transversal que no existía.

Lo que NO hace, a propósito
───────────────────────────
**No se migra lo viejo.** Empieza a anotar desde hoy y lo anterior se lee de
donde está. Reescribir el `/config` de un usuario para rellenar un historial no
compensa el riesgo, y las sesiones ya terminadas no vuelven a escribirse.

**No puede tumbar un trabajo.** `anotar` se traga cualquier error: perder una
línea del historial es un inconveniente; que un rip de 40 minutos muera al
terminar porque el disco de `/config` está lleno, no.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Los `tab_id` son los mismos que expone `/api/activity` (`workload.TAB_IDS`),
# para que el dashboard no tenga que traducir entre dos vocabularios.
TAB_RIP = "rip"
TAB_MKV = "mkv"
TAB_CMV40 = "cmv40"

# Tipos de trabajo. Lo que distingue a dos trabajos de la misma pestaña.
TIPO_RIP = "rip"
TIPO_FASE_CMV40 = "fase_cmv40"
TIPO_PREFLIGHT = "preflight"

# Los desenlaces. `esperando` es el cuarto y no existía: un trabajo que
# terminó su parte y ahora depende de una decisión del usuario. Sin él, un
# pre-flight que acaba recomendando «mantener el MKV» desaparecía de la
# columna sin dejar rastro y había que ir a buscarlo a la pestaña.
ESTADO_HECHO = "done"
ESTADO_ERROR = "error"
ESTADO_CANCELADO = "cancelled"
ESTADO_ESPERANDO = "esperando"
TIPO_ANALISIS_EXTENDIDO = "analisis_extendido"
TIPO_COPIA_BIBLIOTECA = "copia_biblioteca"

# Un registro son ~250 bytes. Con el peor caso realista (un trabajo cada media
# hora, día y noche) el tope tarda año y medio en llegar; cuando llega, el
# fichero pasa a `.jsonl.1` y se empieza de cero. Solo se guarda UNA
# generación: esto es para mirar qué ha pasado, no un archivo histórico.
TOPE_BYTES = 5 * 1024 * 1024


def ruta() -> Path:
    # `paths` se importa aquí dentro y no arriba porque los tests lo parchean:
    # con `from paths import CONFIG_DIR` este módulo se quedaría con la ruta
    # que hubiera al importarse y escribiría en el /config real.
    import paths
    return Path(paths.CONFIG_DIR) / "historial.jsonl"


def anotar(*, id: str, tab: str, tipo: str, que: str,
           inicio: datetime, fin: datetime | None = None,
           estado: str = "done", error: str | None = None,
           ref_log: str | None = None) -> None:
    """Añade una línea al historial. Nunca lanza.

    `ref_log` dice DÓNDE está el log de ese trabajo, no lo copia: el de una
    fase CMv4.0 son 2.000 líneas y ya vive en `/config/cmv40/{id}.log`.
    """
    try:
        fin = fin or datetime.now(timezone.utc)
        registro = {
            "id": id, "tab": tab, "tipo": tipo, "que": que,
            "inicio": inicio.isoformat(), "fin": fin.isoformat(),
            "segundos": round(max(0.0, (fin - inicio).total_seconds()), 1),
            "estado": estado, "error": error, "ref_log": ref_log,
        }
        f = ruta()
        f.parent.mkdir(parents=True, exist_ok=True)
        _rotar_si_toca(f)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
    except Exception as e:                      # noqa: BLE001
        # Ver el docstring del módulo: una línea perdida no vale un job.
        logger.warning("[historial] no se pudo anotar %s: %s", id, e)


def _rotar_si_toca(f: Path) -> None:
    try:
        if f.exists() and f.stat().st_size >= TOPE_BYTES:
            os.replace(f, f.with_suffix(f.suffix + ".1"))
    except OSError as e:
        logger.warning("[historial] no se pudo rotar: %s", e)


def leer(limite: int = 200) -> list[dict]:
    """Los últimos `limite` trabajos, **del más reciente al más antiguo**.

    Tolera una última línea a medias: el fichero se escribe con `append` y un
    `kill -9` en mitad de la escritura la deja cortada. Una línea ilegible se
    salta; el resto del historial es perfectamente válido y perderlo entero por
    un byte sería el peor canje posible.
    """
    out: list[dict] = []
    f = ruta()
    # La generación anterior solo se toca si la actual no llena el límite —
    # justo después de rotar, si no, el historial parecería vacío.
    for candidato in (f, f.with_suffix(f.suffix + ".1")):
        if len(out) >= limite:
            break
        try:
            texto = candidato.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lineas = texto.splitlines()
        for linea in reversed(lineas):
            if len(out) >= limite:
                break
            linea = linea.strip()
            if not linea:
                continue
            try:
                registro = json.loads(linea)
            except ValueError:
                continue
            if isinstance(registro, dict):
                out.append(registro)
    return out


def borrar(id: str, inicio: str) -> bool:
    """Quita una línea del historial. Devuelve si había algo que quitar.

    **La clave es `(id, inicio)`, no el id.** Una sesión re-ejecutada deja
    varias líneas con el mismo id, y borrar «la del rip de Dune» tendría que
    decidir cuál — así que se identifica por cuándo empezó, que es lo único
    que las distingue.

    El fichero es append-only, así que quitar una línea obliga a reescribirlo.
    Se hace en el sitio en que es aceptable: a petición del usuario, sobre un
    fichero con tope de 5 MB, y con `.tmp` + `os.replace` para que un fallo a
    medias no se lleve el historial entero. Se recorren las DOS generaciones
    porque `leer` también lo hace: si no, borrar una entrada justo después de
    rotar no tendría efecto y la línea seguiría apareciendo.
    """
    borrado = False
    f = ruta()
    for candidato in (f, f.with_suffix(f.suffix + ".1")):
        try:
            texto = candidato.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        conservadas, quita = [], False
        for linea in texto.splitlines():
            cruda = linea.strip()
            if not cruda:
                continue
            try:
                registro = json.loads(cruda)
            except ValueError:
                # Una línea ilegible se conserva: no es de nadie y tirarla
                # aquí sería aprovechar un borrado para perder datos.
                conservadas.append(linea)
                continue
            if (isinstance(registro, dict) and registro.get("id") == id
                    and registro.get("inicio") == inicio):
                quita = True
                continue
            conservadas.append(linea)
        if not quita:
            continue
        tmp = candidato.with_suffix(candidato.suffix + ".tmp")
        try:
            tmp.write_text("".join(l + "\n" for l in conservadas),
                           encoding="utf-8")
            os.replace(tmp, candidato)
            borrado = True
        except OSError as e:
            logger.warning("[historial] no se pudo borrar %s: %s", id, e)
            tmp.unlink(missing_ok=True)
    return borrado
