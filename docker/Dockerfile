# ── Stage 1 — version-detector ────────────────────────────────────────────────
# Lee .git del build context y deriva version+commit. Permite que builds
# locales (sin pasar APP_VERSION/APP_COMMIT como build-args) tengan version
# correcta sin instalar git en la imagen final ni dejar .git dentro.
# Si .git no existe (build context sin repo) escribe los defaults.
FROM alpine/git AS version-detector
WORKDIR /repo
COPY .git ./.git
# `--match 'v*'`: solo los tags de RELEASE cuentan. Sin el filtro,
# cualquier tag auxiliar —`pre-i18n`, el punto de retorno de una rama— se
# convierte en el ancla de `git describe` y la app reporta
# `pre-i18n-8-g811d7fe`. Con eso `_semver_gt` no puede comparar y
# `check-updates` anunció una actualización a v2.8.1 estando POR DELANTE
# de v2.8.1: el pill de la cabecera invitaba a bajar de versión.
RUN sh -c 'git describe --tags --match "v*" --always 2>/dev/null > /VERSION || true; \
    [ -s /VERSION ] || echo dev > /VERSION; \
    git rev-parse HEAD 2>/dev/null > /COMMIT || true; \
    [ -s /COMMIT ] || echo unknown > /COMMIT'

# ── Stage 2 — runtime ─────────────────────────────────────────────────────────
# Ubuntu 24.04 y no 22.04: el repositorio de MKVToolNix dejó de publicar para
# 22.04 en la v88 (2024-10), y 22.04 sale del soporte estándar el 2027-05-31.
FROM ubuntu:24.04

LABEL org.opencontainers.image.source=https://github.com/Aldicarus/hdo-iso-converter

ARG DEBIAN_FRONTEND=noninteractive

# ── Paquetes de Ubuntu ────────────────────────────────────────────────────────
# Su propia capa, y ANTES de declarar los ARG de versión de las herramientas:
# un ARG declarado entra en el entorno de todos los RUN que le siguen, así que
# subir dovi_tool o MKVToolNix invalidaba esta capa —la más cara: con el NAS
# cargado son 15-20 min— y un fallo de red en una descarga de más abajo la
# repetía entera (pasó con el primer build de la base 24.04).
#
# ffmpeg     — el de Ubuntu, de respaldo (el que se usa es el de BtbN, abajo)
# python3    — backend FastAPI, en su propio venv (ver más abajo)
# kmod       — modprobe udf (entrypoint)
# curl       — healthcheck Docker
RUN apt-get update \
    && apt-get install -y wget curl ca-certificates gnupg python3 python3-venv kmod ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

# Las descargas reintentan y DICEN por qué fallan: con `wget -q` el primer
# build de la base 24.04 murió en la clave del repo de MKVToolNix sin una
# línea que explicara qué pasó (desde el host la URL contestaba 200).
ARG WGET="wget -nv --tries=5 --waitretry=5 --retry-connrefused --retry-on-host-error"

# ── MKVToolNix y MediaInfo, FIJADOS a una versión ─────────────────────────────
# Hasta la base 22.04 no hacía falta fijarlos: el repo de MKVToolNix se quedó
# en la 88 para 22.04 y Ubuntu congela MediaInfo en la 21.09, así que un build
# desde cero instalaba siempre lo mismo. En 24.04 los dos repos siguen vivos y
# sin fijar cada build traería la versión de ese día, que es la que NO cubre el
# contrato de los fakes (`test_contrato_de_los_fakes`) ni la regresión real
# (`tools/regresion_real.py`). Subir una es cambiar su ARG, recapturar y pasar
# la regresión.
#
# - MKVToolNix desde el repo oficial con `=versión`: su índice de noble
#   conserva TODAS las versiones (de la 83 a la 103, comprobado), así que el
#   pin sobrevive a que salga la siguiente. Hace falta v81+ para el Dolby
#   Vision dual-layer (combina BL+EL en una pista con su DOVI config record).
# - MediaInfo desde las URL por versión de MediaArea, con su sha256: su
#   repositorio solo indexa la última, pero sirve cada versión en
#   /download/binary/<paquete>/<versión>/ (la 24.06 sigue ahí), y los .deb
#   son idénticos byte a byte a los de su pool firmado (comprobado con los
#   SHA256 de su índice de noble).
#
# mkvtoolnix — mkvmerge (análisis + extracción), mkvpropedit, mkvextract
# mediainfo  — bitrate real, HDR10, nombre comercial (Atmos, DTS:X)
ARG MKVTOOLNIX_VERSION=103.0-0~ubuntu2404bunkus01
ARG MEDIAINFO_VERSION=26.10
ARG LIBZEN_VERSION=0.4.41
ARG MEDIAINFO_SHA256=bbd2228ef9d0288c0050954b2045dd4e5ee6382db8d3be03c2b5c4c518ac987a
ARG LIBMEDIAINFO_SHA256=457705ed9c0e4384150fce2271ed01c49fb6e4b35a27071f0f0a1c30c497560c
ARG LIBZEN_SHA256=56591441f8f475337ae3bbc2f8ceb76104ede84964fe9df8d0c1cc96460b37f0
RUN ${WGET} -O /usr/share/keyrings/gpg-pub-moritzbunkus.gpg \
       https://mkvtoolnix.download/gpg-pub-moritzbunkus.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/gpg-pub-moritzbunkus.gpg] https://mkvtoolnix.download/ubuntu/ noble main" \
       > /etc/apt/sources.list.d/mkvtoolnix.download.list \
    && apt-get update \
    && apt-get install -y "mkvtoolnix=${MKVTOOLNIX_VERSION}" \
    && cd /tmp \
    && ${WGET} "https://mediaarea.net/download/binary/libzen0/${LIBZEN_VERSION}/libzen0v5_${LIBZEN_VERSION}-1_amd64.Ubuntu_24.04.deb" \
    && ${WGET} "https://mediaarea.net/download/binary/libmediainfo0/${MEDIAINFO_VERSION}/libmediainfo0v5_${MEDIAINFO_VERSION}-1_amd64.Ubuntu_24.04.deb" \
    && ${WGET} "https://mediaarea.net/download/binary/mediainfo/${MEDIAINFO_VERSION}/mediainfo_${MEDIAINFO_VERSION}-1_amd64.Ubuntu_24.04.deb" \
    && echo "${LIBZEN_SHA256}  libzen0v5_${LIBZEN_VERSION}-1_amd64.Ubuntu_24.04.deb" | sha256sum -c - \
    && echo "${LIBMEDIAINFO_SHA256}  libmediainfo0v5_${MEDIAINFO_VERSION}-1_amd64.Ubuntu_24.04.deb" | sha256sum -c - \
    && echo "${MEDIAINFO_SHA256}  mediainfo_${MEDIAINFO_VERSION}-1_amd64.Ubuntu_24.04.deb" | sha256sum -c - \
    && apt-get install -y ./libzen0v5_*.deb ./libmediainfo0v5_*.deb ./mediainfo_*.deb \
    && rm -f /tmp/*.deb \
    && rm -rf /var/lib/apt/lists/*

# ── dovi_tool (pre-built musl binary — usado en Tab 3 CMv4.0 BD) ─────────────
# Cuando DOVI_TOOL_VERSION cambia en Dockerfile/docker-compose.yml, este RUN
# se invalida y se re-descarga el binario en el siguiente `docker compose
# build --pull`. Sin cambio de version Docker usa cache (correcto). El sha256
# es el `digest` que GitHub publica para el asset: al subir de versión se
# cambian los dos ARG a la vez.
ARG DOVI_TOOL_VERSION=2.3.4
ARG DOVI_TOOL_SHA256=1844258e13c26607b32224bf1fa82b595d3b35949f5467405fda560daad32b3f
RUN ${WGET} \
    "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_TOOL_VERSION}/dovi_tool-${DOVI_TOOL_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
    -O /tmp/dovi_tool.tar.gz \
    && echo "${DOVI_TOOL_SHA256}  /tmp/dovi_tool.tar.gz" | sha256sum -c - \
    && tar xzf /tmp/dovi_tool.tar.gz -C /tmp \
    && mv /tmp/dovi_tool /usr/local/bin/dovi_tool \
    && chmod +x /usr/local/bin/dovi_tool \
    && rm /tmp/dovi_tool.tar.gz

# Label de dovi_tool (constante por version, no invalida cache de app)
LABEL dovi_tool.version="${DOVI_TOOL_VERSION}"

# ── ffmpeg moderno (build estatico de BtbN) ───────────────────────────────────
# Ubuntu trae su ffmpeg (4.4.2 en 22.04, 6.1 en 24.04). Se instala 7.1 encima
# en /usr/local/bin, que precede a /usr/bin en el PATH; el de apt se queda
# como red de seguridad.
#
# NO se hace por velocidad: medido en el NAS sobre el mismo tramo de vídeo,
# 4.4.2 y 7.1.5 tardan lo mismo (10 s vs 11 s, dentro del ruido) porque ffmpeg
# aqui hace stream-copy y esta limitado por el disco, no por CPU. Se hace por
# los cuatro años de correcciones — incluido el demuxer de Matroska, que es
# justo donde hemos visto MKVs dar "File ended prematurely".
#
# Compatibilidad verificada bit a bit con nuestro uso (stream-copy del v:0 +
# hevc_mp4toannexb + muxer tee hacia un pipe): el HEVC y el RPU resultantes
# tienen el mismo md5 con las dos versiones.
#
# Para volver atras: comentar este bloque y reconstruir; el ffmpeg de apt
# vuelve a ser el que se usa, sin tocar nada mas.
# Pin a un autobuild FECHADO, no a la release rodante `latest`. Su asset
# `ffmpeg-n7.1-latest-...` desapareció el 2026-08-17 cuando BtbN rotó a n8.1 y
# n9.0, y el build de la imagen v2.8.0 murió con wget exit 8 — mientras el NAS
# seguía compilando bien porque tenía la capa en caché. Un pin contra una
# etiqueta móvil no es un pin.
#
# **BtbN BORRA sus autobuilds diarios y solo conserva el ÚLTIMO DE CADA MES**,
# y de esos, los 24 más recientes (`util/prunetags.sh`: KEEP_LATEST=14,
# KEEP_MONTHLY=24). Este pin, el de julio de 2026, desaparece hacia agosto de
# 2028: antes hay que cambiarlo o subir el tarball a nuestra release de
# fuentes.
# Medido el 2026-09-09 sobre las 35 releases vivas de su repositorio: las que
# aún tienen assets de la línea 7.1 son 2026-07-31, 06-30, 05-31, 04-30,
# 03-31, 02-28… todas fin de mes. El pin anterior era `2026-08-16-13-00`, un
# diario de mitad de mes, y ya devuelve 404 — el NAS seguía construyendo solo
# porque tenía la capa en caché, y en cuanto hubo que purgarla por el bug ZFS
# el build murió con `wget` código 8.
#
# Regla: pinear SIEMPRE al último autobuild del mes. Es el único que sobrevive.
#
# Se mantiene la línea 7.1.5, la misma con la que se validó el pipe de la Fase
# A: cambiar de rama mayor (8.1 o 9.0, las que BtbN construye ahora) es tocar
# el binario del que depende el paso íntegro del HEVC y del RPU, y eso no se
# hace de rebote para arreglar un build.
#
# El build de BtbN es un .tar.xz y la imagen base de 24.04 ya no trae `xz`
# (la de 22.04 sí): sin él, `tar xJf` muere con «xz: Cannot exec». Va en su
# capa, junto al único que lo necesita.
RUN apt-get update \
    && apt-get install -y xz-utils \
    && rm -rf /var/lib/apt/lists/*
ARG FFMPEG_RELEASE=autobuild-2026-07-31-14-10
ARG FFMPEG_BUILD=ffmpeg-n7.1.5-12-g1fdbca85aa-linux64-gpl-7.1
RUN ${WGET} \
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_RELEASE}/${FFMPEG_BUILD}.tar.xz" \
    -O /tmp/ffmpeg.tar.xz \
    && tar xJf /tmp/ffmpeg.tar.xz -C /tmp \
    && mv /tmp/${FFMPEG_BUILD}/bin/ffmpeg /tmp/${FFMPEG_BUILD}/bin/ffprobe /usr/local/bin/ \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf /tmp/ffmpeg.tar.xz /tmp/${FFMPEG_BUILD} \
    && ffmpeg -version | head -1
LABEL ffmpeg.build="${FFMPEG_RELEASE}/${FFMPEG_BUILD}"

# ── Lo instalado es lo fijado ─────────────────────────────────────────────────
# Los tests miran el Dockerfile, y un ARG que dice 103 no demuestra que se
# instale la 103: un `apt-get install mkvtoolnix` sin el `=` traería la del
# día con el ARG, el contrato y los avisos diciendo otra cosa. Se comprueba
# aquí, contra los binarios, y el build falla si no coinciden (revisión de
# Codex del 2026-10-28).
RUN mkvmerge --version | head -1 | grep -q "v${MKVTOOLNIX_VERSION%%-*} " \
    && mediainfo --Version | tail -1 | grep -qx "MediaInfoLib - v${MEDIAINFO_VERSION}" \
    && dovi_tool --version | head -1 | grep -qx "dovi_tool ${DOVI_TOOL_VERSION}" \
    && v="${FFMPEG_BUILD#ffmpeg-}" && ffmpeg -version | head -1 | grep -q "version ${v%%-linux64*}-" \
    && echo "Versiones instaladas = versiones fijadas"

# ── Python app ────────────────────────────────────────────────────────────────
# Ubuntu 24.04 marca su Python como «externally managed» (PEP 668) y un
# `pip install` al sistema falla. Venv propio y delante en el PATH: `python3`
# —el CMD, `docker exec … python3`, el generador de avisos— es el del venv.
# pip se actualiza dentro: el de la rueda de Ubuntu arrastra avisos de
# seguridad que no afectan a la app, pero que un escáner sí lee.
RUN python3 -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Version e info de commit baked al build. El workflow GHCR los pasa como
# build-args (APP_VERSION = tag git o 'dev-<sha>'; APP_COMMIT = SHA completo).
# El backend los lee de env y los expone en /api/version. En dev local sin
# build-args se queda como 'dev' y el backend ejecuta `git describe` al iniciar.
#
# IMPORTANTE: estos ARG/ENV/LABEL viven al FINAL para no invalidar la cache
# de las capas pesadas (apt, dovi_tool, pip) en cada commit. Cambiar el commit
# solo invalida estas pocas lineas + el COPY app/ siguiente.
ARG APP_VERSION=dev
ARG APP_COMMIT=unknown
ENV APP_VERSION=${APP_VERSION}
ENV APP_COMMIT=${APP_COMMIT}
LABEL app.version="${APP_VERSION}"
LABEL app.commit="${APP_COMMIT}"
LABEL org.opencontainers.image.version="${APP_VERSION}"
LABEL org.opencontainers.image.revision="${APP_COMMIT}"

# La imagen NO es solo MIT: lleva binarios GPL. El SPDX describe el conjunto,
# que es lo que un escáner de cumplimiento espera encontrar aquí.
LABEL org.opencontainers.image.licenses="MIT AND GPL-3.0-or-later AND GPL-2.0 AND BSD-2-Clause AND BSD-3-Clause AND Apache-2.0 AND MPL-2.0"
LABEL org.opencontainers.image.documentation="https://github.com/Aldicarus/hdo-iso-converter/blob/main/THIRD-PARTY-NOTICES.md"

# La clave de TMDb con la que la app funciona sin configurar nada. NO está en
# el repositorio: la inyecta el workflow de GHCR desde un secreto, así que
# quien mire el código en GitHub no encuentra nada que descodificar. Queda
# visible en la imagen (`docker inspect`), y eso es inevitable — la app tiene
# que poder usarla. Ver `services/settings_store.clave_tmdb_de_la_app`.
#
# Vacío por defecto a propósito: un build sin el arg produce una imagen sin
# clave, y la app se comporta como antes de que esto existiera (TMDb sin
# configurar hasta que el usuario ponga la suya en ⚙︎).
ARG TMDB_APP_KEY=""
ENV TMDB_APP_KEY=${TMDB_APP_KEY}

# El repositorio DoviTools que trae la app. Mismo trato que la clave de TMDb:
# fuera del repositorio, horneado desde un secreto. Vacío por defecto — un
# build sin el arg deja la pestaña Repo como estaba, con la explicación de la
# donación y el campo para pegar el enlace propio.
ARG CMV40_DRIVE_FOLDER_APP=""
ENV CMV40_DRIVE_FOLDER_APP=${CMV40_DRIVE_FOLDER_APP}

# Fallback files: si APP_VERSION llega como 'dev' (build local sin args) el
# backend lee /app/VERSION para mostrar la version derivada del git context.
# Stage version-detector escribio estos ficheros leyendo .git del build ctx.
COPY --from=version-detector /VERSION /app/VERSION
COPY --from=version-detector /COMMIT /app/COMMIT

COPY app/ .

# ── Avisos de terceros ────────────────────────────────────────────────────────
# La obligación de la GPL es sobre el OBJETO que se distribuye, y el objeto es
# ESTA IMAGEN, no el repositorio de GitHub: los avisos y la oferta de fuentes
# tienen que viajar dentro. Ver THIRD-PARTY-NOTICES.md.
#
# Los dos ficheros se GENERAN aquí en vez de escribirse a mano porque los
# paquetes de apt NO están fijados a una versión: cada build puede traer otra,
# y la fuente que hay que poder entregar es la de esta imagen. Una lista
# escrita a mano diría la versión de otro día — es el mismo fallo que un
# subset de idiomas mantenido en paralelo.
#
# `dpkg-query -W` sin `-f` ya emite `paquete<TAB>versión`. Se evita el formato
# explícito a propósito: un `${Package}` en un RUN lo expande DOCKER como
# build-arg, y al no existir se sustituye por la cadena vacía sin dar error.
COPY LICENSE THIRD-PARTY-NOTICES.md ./
RUN cp THIRD-PARTY-NOTICES.md ./static/ \
    && dpkg-query -W | sort > ./static/licenses/INSTALLED-PACKAGES.txt \
    && python3 tools/generar_avisos_python.py > ./static/licenses/PYTHON-DEPENDENCIES.md \
    && echo "Avisos: $(wc -l < ./static/licenses/INSTALLED-PACKAGES.txt) paquetes de sistema"

# ── Volumes ───────────────────────────────────────────────────────────────────
# Declaracion para `docker run` standalone. Con docker-compose los binds del
# .env tienen prioridad (ver docker-compose.yml). Incluye:
# - /mnt/isos       ISOs UHD origen (Tab 1, ro)
# - /mnt/library    biblioteca de MKVs (Tabs 2 y 3, ro)
# - /mnt/output     MKVs finales (rw)
# - /mnt/tmp        temporales + workdir CMv4.0 (rw)
# - /mnt/cmv40_rpus RPUs externos legacy (Tab 3, ro)
# - /config         sesiones + cola + app_settings.json (rw)
VOLUME ["/mnt/isos", "/mnt/library", "/mnt/output", "/mnt/tmp", "/mnt/cmv40_rpus", "/config"]

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
