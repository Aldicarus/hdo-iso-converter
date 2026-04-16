# HDO ISO Converter

Aplicación web en contenedor Docker para procesar contenido UHD Blu-ray. Diseñada para correr en NAS QNAP x86 (amd64) pero compatible con cualquier host Linux con Docker.

## Herramientas

| Tab | Nombre | Descripción |
|-----|--------|-------------|
| 1 | **Crear MKV** | Convierte ISOs UHD Blu-ray a MKV con selección automática de pistas y soporte Dolby Vision FEL |
| 2 | **Editar MKV** | Editor de propiedades de MKVs existentes: nombres de pistas, flags, capítulos. Sin re-encoding |
| 3 | **CMv4.0 BD** | *(futuro)* Pipeline para añadir Dolby Vision CMv4.0 a discos Blu-ray UHD |

## Inicio rápido

### Opción A — Imagen pre-construida (recomendado)

```bash
# 1. Pull de la imagen
docker pull ghcr.io/aldicarus/hdo-iso-converter:latest

# 2. Descargar docker-compose.yml y .env.example
mkdir hdo-iso-converter && cd hdo-iso-converter
curl -LO https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/docker-compose.yml
curl -LO https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/.env.example

# 3. Configurar rutas
cp .env.example .env
# Editar .env con las rutas de tu sistema

# 4. Arrancar (usa la imagen descargada, sin build)
docker compose up -d
```

### Opción B — Build local

```bash
git clone https://github.com/Aldicarus/hdo-iso-converter.git
cd hdo-iso-converter

# Configurar rutas
cp docker/.env.example docker/.env
# Editar docker/.env con las rutas de tu sistema

# Build y arrancar
cd docker
docker compose up -d --build
```

Acceder a **http://localhost:8090**

## Configuración

Copiar `docker/.env.example` a `docker/.env` y ajustar:

```env
# Puerto web (default 8090)
HDO_PORT=8090

# Zona horaria
TZ=Europe/Madrid

# Rutas del host
ISOS_PATH=/ruta/a/tus/isos        # ISOs UHD Blu-ray (solo lectura)
OUTPUT_PATH=/ruta/a/salida/mkvs    # MKVs finales
TMP_PATH=/ruta/temporal            # MKVs intermedios (SSD recomendado)
CONFIG_PATH=/ruta/config           # Sesiones JSON + cola
```

## Volúmenes Docker

| Ruta en contenedor | Tipo | Descripción |
|---|---|---|
| `/mnt/isos` | solo lectura | ISOs UHD Blu-ray de origen |
| `/mnt/output` | lectura-escritura | MKVs finales (salida Tab 1, entrada Tab 2) |
| `/mnt/tmp` | lectura-escritura | MKVs intermedios (SSD recomendado) |
| `/config` | lectura-escritura | Sesiones persistentes (JSON) + cola |

> **Espacio en /mnt/tmp:** el MKV intermedio ocupa aprox. lo mismo que el ISO (~50-100 GB). Se borra automáticamente al completar.

## Tab 1 — Crear MKV

### Flujo

```
Nuevo proyecto → mkvmerge -J analiza el ISO → reglas automáticas → revisión → ejecución → validación
```

### Reglas de selección automática

**Audio:** Solo Castellano + VO (idioma original). Por idioma, la pista de mayor calidad (TrueHD Atmos > DD+ Atmos > DTS-HD MA > DTS > DD). Castellano = default.

**Subtítulos:** Detección de forzados por estructura de bloques Blu-ray. Orden: Forzados Castellano → Completos VO → Completos Castellano → Forzados VO.

**Capítulos:** Extraídos del MPLS. Si no hay, generación automática cada 10 min. Timeline visual interactivo para editar.

### Pipeline

- **Ruta directa** (con reordenación): `mkvmerge` lee MPLS → MKV final. Una sola copia de datos.
- **Ruta intermedia** (sin reordenación): MKV intermedio → `mkvpropedit` in-place → mover a output.
- Progreso real en vivo, cancelable, con validación final del MKV resultante.

## Tab 2 — Editar MKV

Editor de propiedades instantáneo via `mkvpropedit` (O(1), sin copiar datos):

- Renombrar pistas de audio y subtítulos
- Cambiar flags default/forced
- Añadir, eliminar y editar capítulos
- Deshacer todos los cambios antes de aplicar

## Stack técnico

- **Backend:** Python 3.10+ (ubuntu:22.04), FastAPI, uvicorn
- **Frontend:** Vanilla JS ES6+, Sortable.js (CDN), sin framework ni build step
- **Herramientas:** mkvmerge, mkvpropedit, mkvextract, ffmpeg, dovi_tool
- **Acceso al ISO:** Loop mount directo (`mount -t udf -o ro,loop`) — requiere `privileged: true`

## Estructura del proyecto

```
hdo-iso-converter/
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example
│   └── entrypoint.sh
├── app/
│   ├── main.py              ← FastAPI app + WebSocket + endpoints
│   ├── models.py            ← Pydantic models
│   ├── storage.py           ← Persistencia JSON
│   ├── queue_manager.py     ← Cola FIFO asyncio
│   ├── phases/
│   │   ├── iso_mount.py     ← Loop mount/umount de ISOs UDF 2.50
│   │   ├── phase_a.py       ← mkvmerge -J + capítulos MPLS
│   │   ├── phase_b.py       ← Motor de reglas automáticas
│   │   ├── phase_d.py       ← mkvmerge extracción desde MPLS
│   │   ├── phase_e.py       ← mkvmerge/mkvpropedit escritura final
│   │   └── mkv_analyze.py   ← Tab 2: análisis + edición MKVs
│   └── static/
│       ├── index.html
│       ├── app.js
│       └── style.css
├── CLAUDE.md
└── run_local.sh
```

## API REST

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/api/isos` | Lista ISOs en /mnt/isos |
| GET | `/api/sessions` | Lista todas las sesiones |
| POST | `/api/analyze` | Analiza un ISO (mkvmerge -J + reglas) |
| GET | `/api/sessions/{id}` | Obtiene una sesión |
| PUT | `/api/sessions/{id}` | Actualiza una sesión |
| DELETE | `/api/sessions/{id}` | Elimina una sesión |
| POST | `/api/sessions/{id}/execute` | Inicia la ejecución |
| POST | `/api/sessions/{id}/cancel` | Cancela la ejecución |
| GET | `/api/mkv/files` | Lista MKVs en /mnt/output |
| POST | `/api/mkv/analyze` | Analiza un MKV existente |
| POST | `/api/mkv/apply` | Aplica ediciones (mkvpropedit) |
| GET | `/api/health` | Healthcheck |
| WS | `/ws/{id}` | Streaming de output en tiempo real |

## Requisitos del host

- Docker con soporte `privileged: true` (para loop mount de ISOs)
- Arquitectura amd64/x86_64
- Espacio en disco para MKVs intermedios (~tamaño del ISO)

## Desarrollo local

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r app/requirements.txt
# Activar modo desarrollo con datos fake:
# Editar .env.local → DEV_MODE=1
./run_local.sh
```

## Licencia

MIT
