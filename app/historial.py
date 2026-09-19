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

from i18n import t as tr

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
# El quinto: el trabajo no terminó ni falló ni lo paraste — se lo llevó por
# delante un reinicio del contenedor. Existe porque un trabajo muerto así
# llegaba aquí con el estado que tuviera la sesión en ese instante
# (`running`), que NO es del vocabulario y nadie validaba: la columna lo
# pintaba con el chip rojo de error y sin mensaje, para siempre. Hay dos
# líneas así en el NAS, de un deploy a mitad de la cola de Juego de Tronos
# (2026-09-12) que este mismo repo documenta por otro motivo.
ESTADO_INTERRUMPIDO = "interrupted"

# Los estados que cierran un trabajo: una línea con uno de estos ya no se
# reescribe. `esperando` NO está, justamente porque puede resolverse.
_CERRADOS = (ESTADO_HECHO, ESTADO_ERROR, ESTADO_CANCELADO,
             ESTADO_INTERRUMPIDO)

#: Todo lo que puede escribirse en `estado`. Cualquier otra cosa describe un
#: trabajo que no llegó a cerrarse, y se anota como interrumpido.
ESTADOS = (ESTADO_HECHO, ESTADO_ERROR, ESTADO_CANCELADO, ESTADO_ESPERANDO,
           ESTADO_INTERRUMPIDO)

# Por qué se paró, cuando nadie lo dice. Un trabajo cancelado sin motivo se
# quedaba con su icono y nada más: el análisis extendido contaba «Cancelado
# por el usuario» y los otros cuatro tipos, en silencio. Se rellena aquí y no
# en cada sitio para que un tipo nuevo no pueda olvidarlo.
def motivo_cancelado() -> str:
    """Por qué se paró, cuando nadie lo dice.

    Función y no constante: un literal en el ámbito del módulo se evalúa al
    importar y dejaría el motivo en el idioma que hubiera al arrancar el
    contenedor, pasara lo que pasara después con el ajuste.
    """
    return tr('historial.motivo_cancelado')


def motivo_interrumpido() -> str:
    """Por qué se paró un trabajo que nadie paró.

    Función y no constante, por lo mismo que `motivo_cancelado`: un literal
    en el ámbito del módulo congela el idioma del arranque.
    """
    return tr('historial.motivo_interrumpido')
TIPO_ANALISIS_EXTENDIDO = "analisis_extendido"
TIPO_COPIA_BIBLIOTECA = "copia_biblioteca"

# Un registro son ~250 bytes. Con el peor caso realista (un trabajo cada media
# hora, día y noche) el tope tarda año y medio en llegar; cuando llega, el
# fichero pasa a `.jsonl.1` y se empieza de cero. Solo se guarda UNA
# generación: esto es para mirar qué ha pasado, no un archivo histórico.
TOPE_BYTES = 5 * 1024 * 1024


# Cuántas veces ha cambiado el historial en este proceso. La columna de
# trabajo lo lee en su poll para saber si tiene que recargarlo.
#
# Antes la señal era «cambió lo que está en marcha», y eso solo acierta con
# las líneas NUEVAS: una línea ya escrita que se resuelve —el pre-flight que
# pasa de «requiere decisión» a «mantener el MKV» cuando el usuario contesta—
# no mueve la cola ni lo que corre, así que la tarjeta se quedaba pidiendo una
# decisión ya tomada hasta que otro trabajo empezara o terminara. Con esto la
# señal es el hecho mismo, no un síntoma suyo.
#
# Es por proceso y no se persiste: al reiniciar vuelve a 0, que para el
# navegador es un cambio y recarga, que es justo lo que hay que hacer.
_revision = 0


def revision() -> int:
    """Cuántas veces ha cambiado el historial. Solo sirve para comparar."""
    return _revision


def _tocado() -> None:
    global _revision
    _revision += 1


def ruta() -> Path:
    # `paths` se importa aquí dentro y no arriba porque los tests lo parchean:
    # con `from paths import CONFIG_DIR` este módulo se quedaría con la ruta
    # que hubiera al importarse y escribiría en el /config real.
    import paths
    return Path(paths.CONFIG_DIR) / "historial.jsonl"


def anotar(*, id: str, tab: str, tipo: str, que: str,
           inicio: datetime, fin: datetime | None = None,
           estado: str = "done", error: str | None = None,
           ref_log: str | None = None,
           titulo: str = "", poster: str = "",
           segundos: float | None = None) -> None:
    """Añade una línea al historial. Nunca lanza.

    `ref_log` dice DÓNDE está el log de ese trabajo, no lo copia: el de una
    fase CMv4.0 son 2.000 líneas y ya vive en `/config/cmv40/{id}.log`.
    """
    try:
        fin = fin or datetime.now(timezone.utc)
        # El vocabulario se valida AQUÍ, que es el único sitio por el que se
        # escribe. `anotar` se llama desde el `finally` de cada trabajo, así
        # que un proceso que muere en mitad del rip llega con el estado que
        # tuviera la sesión —`running`— y lo escribía tal cual.
        if estado not in ESTADOS:
            estado = ESTADO_INTERRUMPIDO
        if estado in (ESTADO_CANCELADO, ESTADO_INTERRUMPIDO) and not error:
            error = (motivo_cancelado() if estado == ESTADO_CANCELADO
                     else motivo_interrumpido())
        registro = {
            "id": id, "tab": tab, "tipo": tipo, "que": que,
            # La película y su miniatura, escritas AQUÍ porque aquí la sesión
            # está en la mano. La línea del historial no tiene de dónde
            # sacarlas después: es append-only y no guarda referencia a la
            # sesión, que además puede haberse borrado. Las líneas ya escritas
            # no las llevan y se pintan con su icono — no se migra nada.
            "titulo": titulo, "poster": poster,
            "inicio": inicio.isoformat(), "fin": fin.isoformat(),
            # Por defecto el reloj de pared. `segundos` se pasa a mano cuando
            # eso no es lo que costó: un proyecto CMv4.0 puede pasar tres días
            # esperando una respuesta del usuario, y lo que interesa es el
            # tiempo de PROCESO.
            "segundos": round(
                max(0.0, (fin - inicio).total_seconds())
                if segundos is None else max(0.0, segundos), 1),
            "estado": estado, "error": error, "ref_log": ref_log,
        }
        f = ruta()
        f.parent.mkdir(parents=True, exist_ok=True)
        _rotar_si_toca(f)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
        _tocado()
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
                out.append(_con_el_nombre_de_hoy(registro))
    return out


# Trabajos que cambiaron de nombre, viejo → nuevo.
#
# «Análisis extendido» no decía de qué era —y se confundía con el «Análisis
# del disco» de Tab 1—, así que hoy se llama «Análisis RPU/Luz MKV». Lo que ya
# está escrito conserva el texto viejo: el historial es **append-only y no se
# migra**, que es la regla desde que existe (reescribir el `/config` de un
# usuario para cambiar una palabra no compensa, y un `kill -9` a mitad de la
# reescritura sí hace daño).
#
# Así que el nombre se actualiza **al leer**. El fichero no se toca y en la
# columna no conviven dos nombres para el mismo trabajo, que es lo que se ve.
# **El lado izquierdo NO se traduce, y es la parte importante.** Es el
# prefijo que se busca en lo que ya está ESCRITO en `historial.jsonl`, y ahí
# hay líneas de antes de que la app hablara tres idiomas: son castellano
# literal y lo seguirán siendo, porque el fichero no se migra. Pasarlo por
# `tr()` haría que con la app en inglés no casara ninguna y el renombrado
# dejara de aplicarse en silencio.
#
# El lado derecho sí, que es lo que se enseña, y se resuelve al leer —no
# aquí— porque un literal en el ámbito del módulo congela el idioma.
_RENOMBRADOS = (
    ("Análisis extendido ·", 'historial.analisis_rpu_luz_mkv'),
    ("Análisis RPU/Luz ·",   'historial.analisis_rpu_luz_mkv'),
)


def _con_el_nombre_de_hoy(registro: dict) -> dict:
    # Lo mismo que con el nombre, y por el mismo motivo: el fichero no se
    # migra. Las líneas escritas antes de que `anotar` validara el
    # vocabulario traen el estado que tuviera la sesión al morir el proceso
    # —hay dos con `running` en el NAS—, y la columna las pinta como un error
    # rojo sin mensaje porque su tabla no las conoce.
    estado = registro.get("estado")
    if estado and estado not in ESTADOS:
        registro = {**registro, "estado": ESTADO_INTERRUMPIDO,
                    "error": registro.get("error") or motivo_interrumpido()}
    que = registro.get("que")
    if not isinstance(que, str):
        return registro
    for viejo, clave_nueva in _RENOMBRADOS:
        if que.startswith(viejo):
            nuevo = tr(clave_nueva)
            # Copia: los registros salen a la API y no se guardan de vuelta,
            # pero mutar lo que se acaba de leer del disco invita a sorpresas.
            return {**registro, "que": nuevo + que[len(viejo):]}
    return registro


def _reescribir(transformar) -> bool:
    """Reescribe el historial aplicando `transformar` a cada registro.

    `transformar(registro)` devuelve el registro (cambiado o no) o `None`
    para quitarlo. Devuelve si algo cambió.

    El fichero es append-only, así que cualquier cambio obliga a reescribirlo.
    Se hace donde es aceptable: a petición del usuario, sobre un fichero con
    tope de 5 MB y con `.tmp` + `os.replace`, para que un fallo a medias no se
    lleve el historial entero. Se recorren las DOS generaciones porque `leer`
    también lo hace: si no, tocar una entrada justo después de rotar no tendría
    efecto y la línea seguiría apareciendo.
    """
    cambiado = False
    f = ruta()
    for candidato in (f, f.with_suffix(f.suffix + ".1")):
        try:
            texto = candidato.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        salida, toca = [], False
        for linea in texto.splitlines():
            cruda = linea.strip()
            if not cruda:
                continue
            try:
                registro = json.loads(cruda)
            except ValueError:
                # Una línea ilegible se conserva: no es de nadie y tirarla
                # aquí sería aprovechar un cambio para perder datos.
                salida.append(linea)
                continue
            nuevo = transformar(registro) if isinstance(registro, dict) else registro
            if nuevo is None:
                toca = True
                continue
            if nuevo is not registro or nuevo != registro:
                toca = True
            salida.append(json.dumps(nuevo, ensure_ascii=False))
        if not toca:
            continue
        tmp = candidato.with_suffix(candidato.suffix + ".tmp")
        try:
            tmp.write_text("".join(l + "\n" for l in salida), encoding="utf-8")
            os.replace(tmp, candidato)
            cambiado = True
            _tocado()
        except OSError as e:
            logger.warning("[historial] no se pudo reescribir: %s", e)
            tmp.unlink(missing_ok=True)
    return cambiado


def registrar_estado(*, id: str, **campos) -> None:
    """Deja **UNA** línea para ese trabajo, reemplazando la que no esté cerrada.

    Un proyecto CMv4.0 es un trabajo único que atraviesa siete fases, y cada
    fase dejaba su propia línea: una película llenaba el historial con siete
    «Fase X terminada» y, peor, un proyecto parado esperando respuesta se leía
    igual que uno acabado.

    Aquí la línea es del PROYECTO y se reescribe según su estado: `esperando`
    mientras necesita al usuario, y `done`/`error`/`cancelled` al cerrarse. Una
    ya cerrada NO se toca: si el usuario rehace una fase, se abre otra, que es
    lo mismo que hace Tab 1 con una sesión re-ejecutada.
    """
    if not _reescribir(lambda r: (None if (r.get("id") == id
                                           and r.get("estado") not in _CERRADOS)
                                  else r)):
        pass          # no había ninguna sin cerrar; se añade igual
    anotar(id=id, **campos)


def resolver_espera(id: str, *, nuevo_estado: str | None,
                    nuevo_que: str | None = None) -> bool:
    """Cierra la entrada `esperando` de ese trabajo, si la hay.

    Un pre-flight que acaba pidiendo una decisión deja una línea en
    `ESTADO_ESPERANDO`. En cuanto el usuario responde, esa línea **deja de
    pedir**: o pasa a su desenlace real, o desaparece porque el trabajo
    continúa y lo que venga después escribirá el suyo.

    No hace falta el `inicio` para identificarla: solo puede haber una
    decisión pendiente por proyecto a la vez.

    `nuevo_estado=None` la quita.
    """
    def _t(r):
        if r.get("id") != id or r.get("estado") != ESTADO_ESPERANDO:
            return r
        if nuevo_estado is None:
            return None
        r = dict(r, estado=nuevo_estado)
        if nuevo_que:
            r["que"] = nuevo_que
        return r
    try:
        return _reescribir(_t)
    except Exception as e:                      # noqa: BLE001
        logger.warning("[historial] no se pudo resolver %s: %s", id, e)
        return False


def quitar_sin_cerrar(id: str) -> bool:
    """Retira la línea sin cerrar de ese trabajo, si la hay.

    La llama el orquestador cuando el proyecto vuelve a ejecutarse: mientras
    corre, el sitio donde se ve es «En curso», no el historial.
    """
    return _reescribir(lambda r: (None if (r.get("id") == id
                                           and r.get("estado") not in _CERRADOS)
                                  else r))


def borrar(id: str, inicio: str) -> bool:
    """Quita una línea del historial. Devuelve si había algo que quitar.

    **La clave es `(id, inicio)`, no el id.** Una sesión re-ejecutada deja
    varias líneas con el mismo id, y borrar «la del rip de Dune» tendría que
    decidir cuál — así que se identifica por cuándo empezó, que es lo único
    que las distingue.
    """
    return _reescribir(
        lambda r: None if (r.get("id") == id and r.get("inicio") == inicio)
        else r)
