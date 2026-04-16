#!/bin/bash
set -eo pipefail

echo "[entrypoint] HDO ISO Converter arrancando..."

# ── Validar volúmenes montados ───────────────────────────────────────
for dir in /mnt/isos /mnt/output /mnt/tmp /config; do
  if [ ! -d "$dir" ]; then
    echo "[entrypoint] ERROR: Volumen $dir no montado" >&2
    exit 1
  fi
done

# Verificar escritura en directorios que lo requieren
for dir in /mnt/output /mnt/tmp /config; do
  if [ ! -w "$dir" ]; then
    echo "[entrypoint] ERROR: Volumen $dir no tiene permisos de escritura" >&2
    exit 1
  fi
done

# ── Crear directorio de mount points para ISOs ──────────────────────
mkdir -p /mnt/bd

# ── Verificar que mount UDF está disponible ─────────────────────────
if ! command -v mount &> /dev/null; then
  echo "[entrypoint] ERROR: 'mount' no disponible" >&2
  exit 1
fi

# Verificar que el kernel soporta UDF (modprobe puede fallar sin ser fatal)
modprobe udf 2>/dev/null || true

echo "[entrypoint] Volúmenes verificados. Modo: loop mount directo (privileged)."

exec "$@"
