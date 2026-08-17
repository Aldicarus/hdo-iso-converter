# ── Stage 1 — version-detector ────────────────────────────────────────────────
# Lee .git del build context y deriva version+commit. Permite que builds
# locales (sin pasar APP_VERSION/APP_COMMIT como build-args) tengan version
# correcta sin instalar git en la imagen final ni dejar .git dentro.
# Si .git no existe (build context sin repo) escribe los defaults.
FROM alpine/git AS version-detector
WORKDIR /repo
COPY .git ./.git
RUN sh -c 'git describe --tags --always 2>/dev/null > /VERSION || true; \
    [ -s /VERSION ] || echo dev > /VERSION; \
    git rev-parse HEAD 2>/dev/null > /COMMIT || true; \
    [ -s /COMMIT ] || echo unknown > /COMMIT'

# ── Stage 2 — runtime ─────────────────────────────────────────────────────────
FROM ubuntu:22.04

LABEL org.opencontainers.image.source=https://github.com/Aldicarus/hdo-iso-converter

ARG DEBIAN_FRONTEND=noninteractive
ARG DOVI_TOOL_VERSION=2.3.3

# ── MKVToolNix oficial (v81+ requerido para Dolby Vision dual-layer) ──────────
# Ubuntu 22.04 trae mkvtoolnix v65 que NO soporta DV. El repo oficial
# proporciona v81+ que combina BL+EL en un solo track con DOVI config record.
# Se añade el repo antes de instalar paquetes para que apt lo use.
#
# ── System deps ───────────────────────────────────────────────────────────────
# mkvtoolnix    — mkvmerge (análisis + extracción), mkvpropedit, mkvextract
# ffmpeg        — utilidades multimedia (futuro Tab 3)
# python3       — backend FastAPI
# kmod          — modprobe udf (entrypoint)
# curl          — healthcheck Docker
RUN apt-get update \
    && apt-get install -y wget curl ca-certificates gnupg python3 python3-pip kmod ffmpeg tzdata \
    && wget -qO /usr/share/keyrings/gpg-pub-moritzbunkus.gpg \
       https://mkvtoolnix.download/gpg-pub-moritzbunkus.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/gpg-pub-moritzbunkus.gpg] https://mkvtoolnix.download/ubuntu/ jammy main" \
       > /etc/apt/sources.list.d/mkvtoolnix.download.list \
    && apt-get update \
    && apt-get install -y mkvtoolnix mediainfo \
    && rm -rf /var/lib/apt/lists/*

# ── dovi_tool (pre-built musl binary — usado en Tab 3 CMv4.0 BD) ─────────────
# Cuando DOVI_TOOL_VERSION cambia en Dockerfile/docker-compose.yml, este RUN
# se invalida y se re-descarga el binario en el siguiente `docker compose
# build --pull`. Sin cambio de version Docker usa cache (correcto).
RUN wget -q \
    "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_TOOL_VERSION}/dovi_tool-${DOVI_TOOL_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
    -O /tmp/dovi_tool.tar.gz \
    && tar xzf /tmp/dovi_tool.tar.gz -C /tmp \
    && mv /tmp/dovi_tool /usr/local/bin/dovi_tool \
    && chmod +x /usr/local/bin/dovi_tool \
    && rm /tmp/dovi_tool.tar.gz

# Label de dovi_tool (constante por version, no invalida cache de app)
LABEL dovi_tool.version="${DOVI_TOOL_VERSION}"

# ── ffmpeg moderno (build estatico de BtbN) ───────────────────────────────────
# Ubuntu 22.04 trae ffmpeg 4.4.2, de 2021. Se instala 7.1 encima en
# /usr/local/bin, que precede a /usr/bin en el PATH; el de apt se queda como
# red de seguridad.
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
# Este es exactamente el binario con el que se validó el pipe de la Fase A y el
# que corre en el NAS: n7.1.5-16-g9a4bb2c579-20260816.
ARG FFMPEG_RELEASE=autobuild-2026-08-16-13-00
ARG FFMPEG_BUILD=ffmpeg-n7.1.5-16-g9a4bb2c579-linux64-gpl-7.1
RUN wget -q \
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_RELEASE}/${FFMPEG_BUILD}.tar.xz" \
    -O /tmp/ffmpeg.tar.xz \
    && tar xJf /tmp/ffmpeg.tar.xz -C /tmp \
    && mv /tmp/${FFMPEG_BUILD}/bin/ffmpeg /tmp/${FFMPEG_BUILD}/bin/ffprobe /usr/local/bin/ \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf /tmp/ffmpeg.tar.xz /tmp/${FFMPEG_BUILD} \
    && ffmpeg -version | head -1
LABEL ffmpeg.build="${FFMPEG_RELEASE}/${FFMPEG_BUILD}"

# ── Python app ────────────────────────────────────────────────────────────────
WORKDIR /app
COPY app/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

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

# Fallback files: si APP_VERSION llega como 'dev' (build local sin args) el
# backend lee /app/VERSION para mostrar la version derivada del git context.
# Stage version-detector escribio estos ficheros leyendo .git del build ctx.
COPY --from=version-detector /VERSION /app/VERSION
COPY --from=version-detector /COMMIT /app/COMMIT

COPY app/ .

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
