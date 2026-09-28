"""La firma con la que un MKV dice que salió de esta aplicación.

El objetivo es **reconocer la procedencia**, no demostrarla ante nadie ni
rastrear qué copia acabó dónde: si un día aparece un MKV por ahí, poder
saber si lo hizo esta app.

De eso salen las tres decisiones del diseño:

- **Va en el `SegmentUID`**, que es un campo que ya llevan todos los MKV del
  mundo y que siempre vale 128 bits aleatorios. MediaInfo sigue enseñando su
  línea `Unique ID` de siempre y no hay nada fuera de sitio — un tag propio
  se vería como una línea suelta que desentona. El precio es que **no
  sobrevive a un remux ajeno**: mkvmerge genera un UID nuevo. Se asume, y es
  el canje correcto para estos ficheros: un P7 FEL dual-layer con CMv4.0 es
  justo el que nadie remuxea a la ligera, porque puede romperle la
  señalización Dolby Vision.

- **Los rasgos que se firman son los que `mkvpropedit` NO toca** — duración,
  codecs y número de pistas. Así la firma sobrevive a que el usuario edite el
  fichero con Tab 2, que es lo que más le pasa a un MKV de esta app. Con el
  título dentro se rompía en cuanto se renombraba una pista.

- **No se anuncia en el log.** Una marca discreta que se anuncia deja de
  serlo, y además el usuario no tiene ninguna decisión que tomar al respecto.
  Lo que falle se registra con `logger`, que es donde se mira un diagnóstico.

Y una que es de seguridad y conviene no confundir: la clave **no es un
secreto**. Está aquí y la imagen de GHCR es pública, así que quien quiera
puede fabricar un MKV que esta app dé por suyo. Da igual para lo que la
firma hace: no abre ninguna puerta ni afirma nada ante terceros, y nadie
tiene incentivo para imitar una marca invisible que no concede nada. Lo que
sí impide es que un MKV ajeno coincida por casualidad, que es lo único que
produciría una respuesta equivocada.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

MKVMERGE_BIN = "mkvmerge"
MKVPROPEDIT_BIN = "mkvpropedit"

# El `SegmentUID` de Matroska son 128 bits, o sea 32 caracteres hexadecimales.
LONGITUD_UID = 32

# Ver el docstring del módulo: esto no es un secreto, es una constante que
# distingue nuestros ficheros de los de cualquier otro. `HDO_FIRMA_CLAVE`
# permite usar una propia sin tocar el código.
CLAVE = os.environ.get("HDO_FIRMA_CLAVE", "uhd-blu-ray-toolkit/firma/v1").encode()

# Un `mkvmerge -J` de un MKV lee cabeceras, no datos: es cuestión de
# segundos incluso en un UHD de 90 GB. El tope generoso es por si el pool
# del NAS está saturado por el trabajo que acaba de terminar.
TIMEOUT_S = 300


def rasgos_de(datos: dict) -> str:
    """La huella del contenido sobre la que se calcula la firma.

    Recibe el JSON de ``mkvmerge -J`` ya parseado. Se eligen los tres campos
    que sobreviven a una edición de metadatos con `mkvpropedit`: la duración
    en nanosegundos, los codecs en orden y cuántas pistas hay. El título y
    los nombres de pista quedan fuera A PROPÓSITO — son justo lo que Tab 2
    edita.

    Devuelve "" si el JSON no trae lo mínimo, que es la señal de que aquí no
    se puede firmar ni verificar nada.
    """
    try:
        propiedades = datos.get("container", {}).get("properties", {}) or {}
        duracion = propiedades.get("duration")
        pistas = datos.get("tracks") or []
        if not duracion or not pistas:
            return ""
        codecs = "+".join(str(p.get("codec") or "") for p in pistas)
        return f"{int(duracion)}|{codecs}|{len(pistas)}"
    except (AttributeError, TypeError, ValueError):
        return ""


def firma_de(datos: dict) -> str:
    """El `SegmentUID` que le corresponde a este contenido, o "" si no se sabe."""
    rasgos = rasgos_de(datos)
    if not rasgos:
        return ""
    return hmac.new(CLAVE, rasgos.encode(), hashlib.sha256).hexdigest()[:LONGITUD_UID]


def lleva_nuestra_firma(datos: dict) -> bool:
    """¿El `SegmentUID` de este MKV es el que le tocaría si lo hubiéramos hecho?

    La respuesta negativa no distingue «lo hizo otro» de «lo hicimos nosotros
    y después alguien lo remuxeó»: las dos son indistinguibles desde el
    fichero, y por eso quien la enseña dice «no consta», no «no es tuyo».
    """
    esperada = firma_de(datos)
    if not esperada:
        return False
    actual = datos.get("container", {}).get("properties", {}).get("segment_uid") or ""
    return str(actual).lower().replace("0x", "") == esperada


async def _identificar(mkv_path: str) -> dict | None:
    """``mkvmerge -J`` sobre el MKV, o None si no se pudo."""
    proc = await asyncio.create_subprocess_exec(
        MKVMERGE_BIN, "-J", mkv_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    salida, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
    # mkvmerge sale con 1 en avisos (contenedor con rarezas) y aun así emite
    # el JSON; solo el 2 es un fallo de verdad.
    if proc.returncode not in (0, 1) or not salida:
        return None
    return json.loads(salida.decode("utf-8", errors="replace"))


async def firmar(mkv_path: str) -> bool:
    """Escribe la firma en el `SegmentUID` del MKV. Devuelve si lo consiguió.

    **No lanza nunca.** Se llama al final de un trabajo que ha podido durar
    cuarenta minutos y que ya produjo su fichero: perder la marca es un
    inconveniente, tirar el trabajo por ella sería absurdo. Es el mismo
    criterio que `historial.anotar`.

    `mkvpropedit` reescribe la cabecera y no toca los clusters, así que el
    coste es de milisegundos y no depende del tamaño del fichero.
    """
    try:
        if not Path(mkv_path).exists():
            return False
        datos = await _identificar(mkv_path)
        if datos is None:
            logger.info("[firma] mkvmerge no pudo identificar %s", mkv_path)
            return False
        uid = firma_de(datos)
        if not uid:
            logger.info("[firma] sin rasgos con los que firmar %s", mkv_path)
            return False
        proc = await asyncio.create_subprocess_exec(
            MKVPROPEDIT_BIN, mkv_path, "--edit", "info", "--set", f"segment-uid=0x{uid}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
        # Igual que en Fase E: el 1 de mkvpropedit es un aviso, no un fallo.
        if proc.returncode >= 2:
            logger.info(
                "[firma] mkvpropedit devolvió %s en %s — %s",
                proc.returncode, mkv_path,
                (error or b"").decode("utf-8", errors="replace")[:200],
            )
            return False
        return True
    except Exception as e:  # se traga TODO a propósito: ver el docstring
        logger.info("[firma] no se pudo firmar %s — %s", mkv_path, e)
        return False
