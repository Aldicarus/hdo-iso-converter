# HDO ISO Converter

Aplicación web en contenedor Docker para procesar contenido UHD Blu-ray. Diseñada para correr en NAS QNAP x86 (amd64) pero compatible con cualquier host Linux con Docker.

## Herramientas

Los IDs internos de panel (`tab-panel-1/2/3`) se mantienen por compatibilidad. El **orden visual** y los labels de la UI son:

| Posición visual | Label UI | Panel interno | Descripción |
|---|----------|---|-------------|
| 1 | 💿 **Blu-Ray ISO → MKV** | `tab-panel-1` | Convierte ISOs UHD Blu-ray a MKV con selección automática de pistas (audio/subs/capítulos) y soporte Dolby Vision FEL. Loop mount directo del ISO sin copia previa |
| 2 | ✨ **Upgrade Dolby Vision CMv4.0** | `tab-panel-3` | Inyecta RPU CMv4.0 en un MKV con CMv2.9 del Blu-ray original. Pre-flight rápido del bin antes de la fase pesada, sync visual frame-a-frame con corrección de offset acumulativo, sistema de trust gates con drop-in para bins pre-validados |
| 3 | ✏️ **Consultar / Editar MKV** | `tab-panel-2` | Inspección profunda (codecs comerciales, bitrate real, HDR10 MaxCLL/MaxFALL, cadena de mastering DV completa, perfil de luminancia DV L1 frame-a-frame) y edición in-place via mkvpropedit |

## Inicio rápido

La imagen oficial está publicada en GitHub Container Registry: `ghcr.io/aldicarus/hdo-iso-converter:latest` (linux/amd64). No hay que compilar nada — descargas y arrancas.

### Opción A — Línea de comandos (recomendado)

```bash
# 1. Crear directorio de trabajo
mkdir hdo-iso-converter && cd hdo-iso-converter

# 2. Descargar docker-compose.yml y .env de ejemplo
curl -LO https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/docker-compose.yml
curl -L https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/.env.example -o .env

# 3. Editar .env con las rutas de tu host
nano .env   # o tu editor favorito (ver tabla "Configuración" abajo)

# 4. Tirar la imagen y arrancar (sin build, usa la pre-construida)
docker compose pull
docker compose up -d
```

Acceder a **http://localhost:8090** (o `HDO_PORT` si lo cambiaste).

Para actualizar a una versión más reciente:

```bash
docker compose pull && docker compose up -d
```

### Opción B — Container Station (QNAP) / Portainer

Apps con UI gráfica para gestionar containers Docker. Ambas soportan `docker-compose.yml`:

1. **Descargar** `docker-compose.yml` y `.env.example` desde el repo:
   - https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/docker-compose.yml
   - https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/.env.example
2. **Renombrar** `.env.example` a `.env` y editar las rutas de tu host (ver tabla "Configuración" abajo).
3. **En Container Station / Portainer**:
   - Crear nueva aplicación de tipo "Docker Compose" (Container Station: *Aplicación → Crear → Personalizado* · Portainer: *Stacks → Add stack → Web editor*).
   - Pegar el contenido de `docker-compose.yml`.
   - Subir el `.env` (Container Station lo pide al final · Portainer: pestaña *Environment variables → Advanced mode → Load variables from .env file*).
   - Crear / Deploy.

La imagen se descarga de `ghcr.io/aldicarus/hdo-iso-converter:latest` automáticamente.

### Opción C — Build desde fuente (devs)

Solo si quieres modificar el código:

```bash
git clone https://github.com/Aldicarus/hdo-iso-converter.git
cd hdo-iso-converter
cp docker/.env.example docker/.env
# Editar docker/.env

cd docker
docker compose -f docker-compose.dev.yml up -d --build
```

### Smoke test

```bash
curl http://localhost:8090/api/health
# {"ok": true, ...}
```

## Configuración

Variables de `docker/.env`:

| Variable | Obligatoria | Default | Descripción |
|---|---|---|---|
| `HDO_PORT` | no | `8090` | Puerto del host expuesto |
| `TZ` | no | `Europe/Madrid` | Zona horaria del contenedor |
| `ISOS_PATH` | **sí** | — | Carpeta con los ISOs UHD origen (read-only) |
| `LIBRARY_PATH` | no | — | Raíz de la biblioteca de MKVs (ro). Tabs 2 y 3 navegan este árbol con file browser. Si no se define, los tabs solo ven `/mnt/output` |
| `OUTPUT_PATH` | **sí** | — | Salida de MKVs finales (rw) |
| `TMP_PATH` | **sí** | — | Workdir temporal — SSD muy recomendado (rw) |
| `CONFIG_PATH` | **sí** | — | Sesiones JSON + cola + `app_settings.json` (rw) |
| `CMV40_RPU_PATH` | **sí** | — | RPUs CMv4.0 externos legacy (Tab 3, ro). Si no la usas, apúntala a un dir vacío — nunca a `/tmp` |
| `TMDB_API_KEY` | no | — | Fallback. Se prefiere la UI (botón ⚙︎) |
| `GOOGLE_API_KEY` | no | — | Fallback para el repo DoviTools en Drive. Se prefiere la UI |

## Volúmenes Docker

| Ruta en contenedor | Tipo | Descripción |
|---|---|---|
| `/mnt/isos` | ro | ISOs UHD Blu-ray origen (Tab 1) |
| `/mnt/library` | ro | Biblioteca de MKVs raíz para file browser (Tabs 2 y 3) |
| `/mnt/output` | rw | MKVs finales (salida Tab 1, entrada Tabs 2 y 3) |
| `/mnt/tmp` | rw | MKVs intermedios + workdir CMv4.0 (SSD recomendado) |
| `/mnt/cmv40_rpus` | ro | RPUs CMv4.0 externos legacy (preferir el repo DoviTools online) |
| `/config` | rw | Sesiones + cola + `app_settings.json` (API keys) |

**Espacio en `/mnt/tmp`:** durante el pipeline CMv4.0 se acumulan ~2× el tamaño del MKV origen. Se libera al hacer "Cleanup" en cada proyecto.

## Tab "Blu-Ray ISO → MKV"

### Flujo

```
Modal "Nuevo proyecto" → fingerprint+análisis (Fase A+B) → revisión en sub-tab → cola FIFO → ejecución → validación
```

- **Detección de duplicados**: SHA-256 sobre primer 1 MB + tamaño del ISO. Si ya hay sesión con la misma huella, ofrece "Abrir existente" o "Re-analizar".
- **No se persiste sesión hasta éxito de Fase A**: si el análisis falla, no quedan ficheros JSON huérfanos.
- **Hasta 5 proyectos abiertos** simultáneamente en sub-tabs.
- **Cola FIFO** con drag & drop y cancelación atómica del subprocess activo.

### Reglas de selección automática

**Audio:** Solo Castellano + VO. Por idioma se elige la pista de mayor calidad (TrueHD Atmos > DD+ Atmos > DTS-HD MA > DTS > DD). Castellano = default; VO = no default. Tag `(DCP 9.1.6)` solo en TrueHD Atmos Castellano.

**Subtítulos:** Detección de forzados por estructura de bloques Blu-ray. Orden de inclusión: Forzados Castellano → Completos VO → Completos Castellano → Forzados VO → Completos Inglés (si VO ≠ Inglés).

**Capítulos:** Extraídos del MPLS en Fase A. Si no hay, generación cada 10 min. Botones "Nombres genéricos" + "Restaurar del disco" para deshacer ediciones.

### Pipeline de análisis (Fase A)

Pipeline de 4 herramientas con el ISO montado:

1. **mkvmerge -J** — identificación de pistas, codecs, idiomas, estructura del disco. Selección inteligente del MPLS principal (10 más grandes → el de más pistas audio).
2. **MediaInfo** — bitrate real por pista, HDR10 metadata (MaxCLL/MaxFALL, mastering display primaries+luminance), detección definitiva de Atmos/DTS:X via `Format_Commercial`, channel layout.
3. **dovi_tool** — análisis RPU Dolby Vision: Profile (4/5/7/8), FEL vs MEL, CM version (v2.9/v4.0), L1/L2/L5/L6/L8/L9/L10/L11/L254 cuando aplica.
4. **mkvextract** — capítulos del MPLS con timestamps precisos.

MediaInfo y dovi_tool son opcionales: si fallan, el análisis sigue con datos de mkvmerge.

### Pipeline de extracción (Fases D+E)

- **Ruta directa** (con reordenación o exclusión): mkvmerge lee MPLS → MKV final en una pasada con mapeo por idioma+codec (no posicional).
- **Ruta intermedio** (sin reordenación): MKV intermedio → mkvpropedit in-place → `mv` al output.
- **Progreso real** del subprocess (`--gui-mode` parseado a `Progress: XX%`), cancelación atómica.
- **Validación final** automática: `mkvmerge -J` + `mkvextract` sobre el MKV resultante comparando contra lo esperado.

## Tab "Consultar / Editar MKV"

### File browser unificado

El tab abre un browser modal con dos roots:

- **📚 Biblioteca** (`/mnt/library`) — navegación recursiva de la raíz definida en `LIBRARY_PATH`
- **📦 Output** (`/mnt/output`) — salida del converter

Selección por click + botón "Seleccionar" (o doble-click). Búsqueda incremental dentro del directorio actual. Breadcrumb navegable.

### Edición in-place (sin re-encoding)

Solo se modifica metadata via `mkvpropedit` (instantáneo, O(1)):

- Renombrar pistas de audio y subtítulos.
- Cambiar flags `default` / `forced`.
- Editar capítulos (timeline interactivo, añadir/eliminar, renombrar, ajustar timestamps).
- "Nombres genéricos" reemplaza nombres custom por `Capítulo XX`.
- "Deshacer cambios" revierte al estado del análisis. "Cerrar" avisa si hay pendientes.

### Radiografía DV+HDR

Panel detallado con cinco secciones:

1. **Stream técnico** — profile, CM version, frames, duración, FPS, escenas, bit depth, codec, RPU size, EL info.
2. **Cadena de mastering** — 3 cards (Master display / Container HEVC / DV target L10) con primaries, peak/min nits, transfer + bit-depth. Chip "P3 ↑ BT.2020" cuando hay expansión de gamut. Filas auxiliares: trim targets DV (chips ámbar de L2 target_max_pq), HDR10 metadata (MaxCLL/MaxFALL del SEI), L11 content type.
3. **Active area (L5)** — offsets T/B/L/R + área activa + aspect ratio + simetría, con visualizador SVG del frame.
4. **CMv4.0 levels** (solo si v4.0) — pills de presencia + visualización logarítmica de L8 trim targets.
5. **Perfil de luminancia DV L1** — sparkline con 3 curvas superpuestas (peak/avg/min), líneas de referencia (HDR10 MaxCLL/MaxFALL, L2 trims, L6 master), tooltip hover crosshair, mini-card con percentiles (peak/p99/p95/p50/avg) + clasificación de escenas (SDR-like <100n / midtone 100-300n / highlight ≥300n) y histograma de distribución.

### Perfil de luminancia (extracción on-demand)

Botón "Analizar ahora" lanza el análisis completo (3-15 min según peli):

1. ffmpeg extrae HEVC del MKV
2. dovi_tool extract-rpu sobre el HEVC
3. dovi_tool export → JSON → parseo de L1 max_pq/avg_pq/min_pq por frame

Polling resiliente: chained-await (no setInterval — evita races de respuestas tardías), guard monotónico anti-rollback de step, fallback en `state.result` para recuperar el resultado si el POST timeout (timeout frontend 60 min, backend hasta 35 min).

> **Nota:** los valores L1 max_pq son la metadata DV codificada por el colorista — no la luminancia que verás tras tone-mapping. Algunas películas (Blade Runner 2049, etc.) están etiquetadas conservadoramente y pueden mostrar peaks bajos aunque las medidas reales en pantalla sean más altas.

## Tab "Upgrade Dolby Vision CMv4.0"

Pipeline para inyectar un RPU Dolby Vision CMv4.0 (de DoviTools u otras fuentes) en un MKV con CMv2.9 del Blu-ray original, añadiendo metadata de tone-mapping L8-L11 sin re-encoding.

### Modal "Nuevo proyecto"

Flujo en 3 tabs de target source:

- **📦 Repo DoviTools** (default) — listado live del Drive público de DoviTools con fuzzy-match contra el filename del MKV. Descarga directa al workdir.
- **🎬 Extraer de MKV** — extrae el RPU de otro MKV propio que ya tenga CMv4.0.
- **📁 Carpeta local** (legacy) — `.bin` de `/mnt/cmv40_rpus`.

Banner de recomendación con clasificación del bin (factible / no factible / Not Sure!) según el sheet live de DoviTools.

### Pre-flight bloqueante

Antes de gastar Fase A (~12 min para extraer el HEVC de UHD), un pre-flight rápido (~5s para drive/path, ~30s-2min para extraer de otro MKV) descarga el bin y valida que tenga CMv4.0:

- ✅ **Bin con CMv4.0** → Fase A arranca, Fase B reutiliza el bin del workdir (sin re-descargar).
- ❌ **Bin sin CMv4.0** (v2.9, p.ej. "P5 to P8 transfer") → aborta antes de Fase A. Mensaje claro al log del proyecto. Ahorro: ~12 min.

Es asíncrono (running_phase="preflight"), bloqueante (otras fases no arrancan en paralelo), y cancelable.

### Fases del pipeline

1. **Analizar origen** — ffmpeg + dovi_tool extract-rpu del MKV origen.
2. **Proporcionar RPU target** — drive / mkv / path. Reutiliza pre-flight si pasó.
3. **Extraer BL/EL** — dovi_tool demux + per-frame data muestreado (saltada con bins drop-in).
4. **Verificar sincronización** — gráfico interactivo con dos curvas, zoom, detección automática de offset, correcciones acumulativas, métrica de confianza (correlación Pearson sobre MaxCLL).
5. **Aplicar corrección** — `dovi_tool editor` con `remove`/`duplicate` (parte de Fase D, no avanza fase).
6. **Inyectar RPU** — `dovi_tool inject-rpu`.
7. **Remux final** — `dovi_tool mux` + mkvmerge preservando audio/subs/capítulos.
8. **Validar** — extracción del RPU del MKV resultante + verificación CM v4.0.

### Sistema de trust

Tras Fase B, el bin se clasifica en `target_type`:

| Tipo | Detección | Comportamiento |
|---|---|---|
| `trusted_p7_fel_final` | P7 FEL + CMv4.0 + L8 + gates OK | Drop-in: skip Fase D + skip merge en F |
| `trusted_p7_mel_final` | P7 MEL + CMv4.0 | Skip Fase D si gates OK |
| `trusted_p8_source` | P8 + CMv4.0 + L8 | Skip Fase D si gates OK, Fase F hace merge |
| `generic` | P8 sin L8 / P5 | Flujo completo (revisión visual + merge) |
| `incompatible` | CMv2.9 | Aborta (cubierto por pre-flight ahora) |

Gates críticos: `frames` (Δ=0), `cm_version` (v4.0), `has_l8`, `l5_div` (≤30 px). Soft: `l6_div` (±50 nits), `l1_div` (±5%).

Override manual: `trust_override = "force_interactive"` fuerza ruta A completa aunque el bin sea trusted.

### Recomendación CMv4.0 (sheet live + Drive)

- **Sheet R3S3T_9999** parseado live (XLSX + openpyxl para preservar hyperlinks; fallback HTML/CSV/disk cache).
- **Fuzzy match** del filename: max(SequenceMatcher, token-set Jaccard, containment) sobre acentos strippeados, normalización de números romanos y stop-words.
- **Multi-candidato TMDb** (top-5) con umbrales adaptativos según presencia/ausencia de año.
- **Dedup** por (slug, año) — distingue p.ej. El Rey León 1994 vs 2019.

## Stack técnico

- **Backend:** Python 3.10+ (ubuntu:22.04), FastAPI, uvicorn
- **Frontend:** Vanilla JS ES6+, Sortable.js (CDN), sin framework ni build step
- **Herramientas:** mkvmerge, mkvpropedit, mkvextract, mediainfo, ffmpeg, dovi_tool 2.3.2
- **Acceso al ISO:** Loop mount directo (`mount -t udf -o ro,loop`) — requiere `privileged: true`
- **Integraciones externas (opcionales):** TMDb (poster/sinopsis), Google Drive v3 + Sheets v4 / openpyxl (repo DoviTools)

## Estructura del proyecto

```
hdo-iso-converter/
├── .github/workflows/
│   └── publish-docker.yml       ← Publica imagen a ghcr.io en cada push a main
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example
│   └── entrypoint.sh
├── app/
│   ├── main.py                  ← FastAPI + WebSocket + endpoints (3 tabs)
│   ├── models.py                ← Pydantic models
│   ├── storage.py               ← Persistencia JSON + fingerprint ISO
│   ├── queue_manager.py         ← Cola FIFO asyncio
│   ├── dev_fixtures.py          ← Fixtures cuando DEV_MODE=1
│   ├── phases/
│   │   ├── iso_mount.py         ← Loop mount/umount UDF 2.50
│   │   ├── phase_a.py           ← Análisis ISO completo
│   │   ├── phase_b.py           ← Reglas de selección automática
│   │   ├── phase_d.py           ← Extracción mkvmerge desde MPLS
│   │   ├── phase_e.py           ← Escritura final + validación
│   │   ├── mkv_analyze.py       ← Tab 2: análisis + edición MKV
│   │   └── cmv40_pipeline.py    ← Tab 3: pipeline completo CMv4.0
│   ├── services/
│   │   ├── settings_store.py    ← /config/app_settings.json
│   │   ├── tmdb.py              ← TMDb search + details
│   │   ├── rec999_sheet.py      ← Sheet DoviTools live
│   │   ├── rec999_drive.py      ← Drive API v3 listado + descarga
│   │   ├── rec999_drive_match.py← Fuzzy match filename → bin
│   │   └── cmv40_recommend.py   ← Orquesta filename → TMDb → sheet
│   └── static/
│       ├── index.html
│       ├── app.js
│       └── style.css
├── archive/                     ← Specs históricas
├── CLAUDE.md
├── README.md
└── run_local.sh
```

## API REST (resumen)

### Tab 1 — ISO → MKV
- `GET /api/isos`, `POST /api/check-duplicate`, `POST /api/analyze`
- `GET/PUT/DELETE /api/sessions/{id}`, `POST /api/sessions/{id}/execute|cancel`
- `WS /ws/{id}` — log en vivo

### Tab 2 — Editar MKV
- `GET /api/library/browse?root=library|output&path=...` — file browser multi-root
- `GET /api/mkv/files` — listado plano de `/mnt/output` (legacy)
- `POST /api/mkv/analyze` — análisis (mkvmerge -J + MediaInfo + dovi_tool)
- `POST /api/mkv/light-profile` — extrae perfil L1 (peak/avg/min + percentiles + refs)
- `GET /api/mkv/light-profile/progress` — polling del análisis on-demand
- `POST /api/mkv/apply` — aplicar ediciones (mkvpropedit)

### Tab 3 — CMv4.0
- `GET /api/cmv40`, `POST /api/cmv40/create`, `GET/DELETE /api/cmv40/{id}`
- `POST /api/cmv40/{id}/preflight-target` — pre-flight bloqueante (kind: drive | path | mkv)
- `POST /api/cmv40/{id}/analyze-source` — Fase A
- `POST /api/cmv40/{id}/target-rpu-{path|mkv|from-drive}` — Fase B
- `POST /api/cmv40/{id}/extract` — Fase C
- `GET /api/cmv40/{id}/sync-data` — datos del chart de Fase D
- `POST /api/cmv40/{id}/{apply-sync|reset-sync|mark-synced|inject|remux|validate|cleanup}` — fases siguientes
- `POST /api/cmv40/{id}/cancel` — mata subprocess activo
- `WS /ws/cmv40/{id}` — log en vivo

### Settings + utilidades
- `GET/PUT /api/settings` — configuración de API keys (TMDb / Google)
- `GET /api/health` — healthcheck

## Requisitos del host

- Docker con soporte `privileged: true` (para loop mount de ISOs UDF 2.50)
- Arquitectura amd64/x86_64
- Espacio en `/mnt/tmp` ≥ 2× tamaño del MKV de mayor tamaño que vayas a procesar (workdir CMv4.0)
- Espacio en `/mnt/output` para los MKVs finales

## Desarrollo local

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r app/requirements.txt

# Modo desarrollo con datos fake (sin ISOs reales):
echo "DEV_MODE=1" > .env.local
./run_local.sh
```

`DEV_MODE=1` activa fixtures: ISOs falsas, sesiones simuladas, RPUs fake. Útil para iterar UI sin tener UHD discos.

## Licencia

MIT
