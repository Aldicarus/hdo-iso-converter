FROM ubuntu:22.04

LABEL org.opencontainers.image.source=https://github.com/Aldicarus/hdo-iso-converter

ARG DEBIAN_FRONTEND=noninteractive
ARG DOVI_TOOL_VERSION=2.3.2
# Version e info de commit baked al build. El workflow GHCR los pasa como
# build-args (APP_VERSION = tag git o 'dev-<sha>'; APP_COMMIT = SHA completo).
# El backend los lee de env y los expone en /api/version. En dev local sin
# build-args se queda como 'dev' y el backend ejecuta `git describe` al iniciar.
ARG APP_VERSION=dev
ARG APP_COMMIT=unknown
ENV APP_VERSION=${APP_VERSION}
ENV APP_COMMIT=${APP_COMMIT}

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

# Labels con metadata bakeada — inspecciona con:
#   docker image inspect hdo-iso-converter --format '{{index .Config.Labels "app.version"}}'
LABEL dovi_tool.version="${DOVI_TOOL_VERSION}"
LABEL app.version="${APP_VERSION}"
LABEL app.commit="${APP_COMMIT}"
LABEL org.opencontainers.image.version="${APP_VERSION}"
LABEL org.opencontainers.image.revision="${APP_COMMIT}"

# ── Python app ────────────────────────────────────────────────────────────────
WORKDIR /app
COPY app/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

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
