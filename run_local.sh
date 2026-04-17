#!/usr/bin/env bash
# run_local.sh — Arranca la app en modo desarrollo local (sin Docker)
#
# Primera vez:
#   python3.12 -m venv .venv
#   .venv/bin/pip install -r app/requirements.txt
#
# Herramientas del pipeline disponibles en macOS:
#   brew install mkvtoolnix            # mkvmerge, mkvpropedit, mkvextract
#   brew install ffmpeg
#
# BDInfoCLI (tetrahydroc fork) solo tiene binario Linux x64.
# Para Fase A en local: usar Docker, o colocar un informe BDInfo .txt de prueba
# en local_data/tmp/ y ajustar la lógica de phase_a para leerlo (modo stub).
#
# Acceso al ISO: configurar QNAP_HOST/USER/PASS en .env.local apuntando al NAS real.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Cargar variables de entorno desde .env.local (opcional, solo si existe)
if [ -f "$SCRIPT_DIR/.env.local" ]; then
  set -a
  source "$SCRIPT_DIR/.env.local"
  set +a
fi

# Crear directorios locales si no existen
mkdir -p "$SCRIPT_DIR/local_data/isos"
mkdir -p "$SCRIPT_DIR/local_data/output"
mkdir -p "$SCRIPT_DIR/local_data/tmp"
mkdir -p "$SCRIPT_DIR/local_data/config"
mkdir -p "$SCRIPT_DIR/local_data/cmv40_rpus"

# Rutas absolutas (uvicorn se ejecuta desde app/)
export ISOS_DIR="$SCRIPT_DIR/local_data/isos"
export OUTPUT_DIR="$SCRIPT_DIR/local_data/output"
export TMP_DIR="$SCRIPT_DIR/local_data/tmp"
export CONFIG_DIR="$SCRIPT_DIR/local_data/config"
export CMV40_RPU_DIR="$SCRIPT_DIR/local_data/cmv40_rpus"

echo "▶  ISO2MKVFEL — modo local"
echo "   ISOS_DIR  : $ISOS_DIR"
echo "   OUTPUT_DIR: $OUTPUT_DIR"
echo "   TMP_DIR   : $TMP_DIR"
echo "   CONFIG_DIR: $CONFIG_DIR"
echo "   URL       : http://localhost:8080"
echo ""

# Activar venv si existe
UVICORN="$SCRIPT_DIR/.venv/bin/uvicorn"
if [ ! -f "$UVICORN" ]; then
  UVICORN="uvicorn"
fi

cd "$SCRIPT_DIR/app"
exec "$UVICORN" main:app --host 0.0.0.0 --port 8080 --reload
