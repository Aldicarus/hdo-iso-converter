# HDO Blu-ray Toolkit

> Suite web para procesar tu biblioteca UHD Blu-ray: **ripeo a MKV con detección de series**, **inspección + edición de metadata in-place** y **upgrade Dolby Vision CMv2.9 → CMv4.0** con clasificación de calidad. Todo en contenedor Docker, pensado para correr en un NAS QNAP/Synology pero compatible con cualquier host Linux x86_64 con Docker y soporte para loop mount.

> [!NOTE]
> El nombre técnico (repositorio GitHub, imagen Docker `ghcr.io/aldicarus/hdo-iso-converter:latest`) se mantiene para no romper despliegues existentes. El nombre público y el de la UI es **HDO Blu-ray Toolkit**.

## Tres herramientas en una

Los IDs internos de panel (`tab-panel-1/2/3`) se mantienen por compatibilidad histórica. El orden visual y los labels de la UI son:

| Pos. | Label UI | Para qué sirve |
|---|----------|---|
| 1 | 💿 **Blu-Ray ISO → MKV** | Ripear UHD Blu-ray a MKV desde 3 tipos de origen (ISO, carpeta BDMV o ficheros M2TS). Toggle obligatorio Película/Serie. Las series se mapean a episodios via TMDb con naming Plex/Jellyfin y una sesión por episodio. |
| 2 | ✏️ **Consultar / Editar MKV** | Inspección profunda de un MKV (codecs comerciales, bitrate real, HDR10, cadena de mastering Dolby Vision completa, perfil de luminancia L1 frame-a-frame). Edición in-place de nombres de pista, flags y capítulos sin re-encoding (instantáneo, vía `mkvpropedit`). |
| 3 | ✨ **Upgrade Dolby Vision CMv4.0** | Pipeline para inyectar un RPU CMv4.0 (de DoviTools u otras fuentes) en un MKV con CMv2.9 del Blu-ray original, añadiendo metadata de tone-mapping L8-L11. Pre-flight rápido del bin, modelo de decisión Mantener/Inyectar según riqueza real del L8, sync visual frame-a-frame, auto-pipeline que sobrevive a cierre del cliente. |

## Inicio rápido

La imagen oficial está publicada en GHCR como `ghcr.io/aldicarus/hdo-iso-converter:latest` (linux/amd64). No hay que compilar nada.

### Opción A — Línea de comandos

```bash
# 1. Crear directorio de trabajo
mkdir hdo-bluray-toolkit && cd hdo-bluray-toolkit

# 2. Descargar docker-compose.yml y .env de ejemplo
curl -LO https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/docker-compose.yml
curl -L https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/.env.example -o .env

# 3. Editar .env con las rutas de tu host (ver tabla "Configuración")
nano .env

# 4. Tirar la imagen y arrancar
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

1. Descarga `docker-compose.yml` y `.env.example` del repo (URLs arriba).
2. Renombra `.env.example` a `.env` y edita las rutas.
3. En Container Station / Portainer:
   - Crea nueva aplicación tipo "Docker Compose" (Container Station: *Aplicación → Crear → Personalizado* · Portainer: *Stacks → Add stack → Web editor*).
   - Pega el contenido de `docker-compose.yml`.
   - Sube el `.env` (Container Station lo pide al final · Portainer: *Environment variables → Advanced mode → Load variables from .env file*).
   - Crear / Deploy.

### Opción C — Build desde fuente

Solo si quieres modificar el código:

```bash
git clone https://github.com/Aldicarus/hdo-iso-converter.git
cd hdo-iso-converter
cp docker/.env.example docker/.env
# Editar docker/.env

cd docker
docker compose up -d --build
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
| `ISOS_PATH` | **sí** | — | Carpeta con los ISOs / carpetas BDMV / ficheros M2TS origen (read-only) |
| `LIBRARY_PATH` | no | — | Raíz de la biblioteca de MKVs (ro). Tabs 2 y 3 navegan este árbol con file browser. Si no se define, los tabs solo ven `/mnt/output` |
| `OUTPUT_PATH` | **sí** | — | Salida de MKVs finales (rw) |
| `TMP_PATH` | **sí** | — | Workdir temporal — SSD muy recomendado (rw) |
| `CONFIG_PATH` | **sí** | — | Sesiones JSON + cola + `app_settings.json` (rw) |
| `CMV40_RPU_PATH` | **sí** | — | RPUs CMv4.0 externos legacy (Tab 3, ro). Si no la usas, apúntala a un dir vacío — nunca a `/tmp` |
| `TMDB_API_KEY` | no | — | Fallback. Se prefiere la UI (botón ⚙︎ Configuración) |
| `GOOGLE_API_KEY` | no | — | Fallback para el repo DoviTools en Drive. Se prefiere la UI |

## Volúmenes Docker

| Ruta contenedor | Tipo | Descripción |
|---|---|---|
| `/mnt/isos` | ro | Orígenes (ISOs / carpetas BDMV / ficheros M2TS) para Tab 1 |
| `/mnt/library` | ro | Biblioteca de MKVs para file browser de Tabs 2 y 3 |
| `/mnt/output` | rw | MKVs finales (salida Tab 1, entrada Tabs 2 y 3) |
| `/mnt/tmp` | rw | MKVs intermedios + workdir CMv4.0 (SSD recomendado) |
| `/mnt/cmv40_rpus` | ro | RPUs CMv4.0 externos legacy (preferir el repo DoviTools online de Tab 3) |
| `/config` | rw | Sesiones + cola + `app_settings.json` con las API keys |

**Espacio en `/mnt/tmp`:** durante el pipeline CMv4.0 se acumulan ~2× el tamaño del MKV origen. Se libera al hacer "Cleanup" en cada proyecto.

---

## Tab "Blu-Ray ISO → MKV"

Ripear contenido UHD Blu-ray a MKV con selección automática de pistas (audio/subtítulos/capítulos) y soporte Dolby Vision FEL. Modal "Nuevo proyecto" → análisis → cola FIFO → ejecución → validación.

### Modal "Nuevo proyecto"

Dos elecciones obligatorias antes de poder analizar:

**1. Tipo de contenido** (toggle Película / Serie):

| | Película | Serie |
|---|---|---|
| Selección en M2TS | Radio (1 fichero) | Checkbox (varios ficheros) |
| MPLS en ISO/BDMV | Se usa el principal | Mapeo MPLS↔episodio |
| Salida | 1 MKV | 1 MKV por episodio |

La zona de selección de origen está visualmente bloqueada (opacity reducida) hasta que el usuario elige tipo — sin esto los discos atípicos (BDMV con 1 m2ts grande + extras) se clasificaban mal con auto-detect.

**2. Tipo de origen** (3 tabs):

- **💿 ISO** — fichero `.iso` (UDF 2.50+). Se monta con `mount -t udf -o ro,loop` y se desmonta al terminar.
- **📁 Carpeta BDMV** — directorio con `BDMV/PLAYLIST/` dentro (disco extraído sin ISO). Sin mount.
- **🎞️ Ficheros M2TS** — `.m2ts` sueltos. Sin BDMV → sin MPLS → capítulos auto-generados cada 10 min.

Todos los orígenes viven en `/mnt/isos`. File browser embebido con sort configurable (por nombre o por tamaño — útil para identificar los m2ts grandes entre los extras pequeños).

### Detección de duplicados

`POST /api/check-duplicate` calcula la huella SHA-256 (primer 1 MB + tamaño del fichero principal) y devuelve **todas las sesiones existentes** con esa huella (no solo la primera). Comportamiento:

- **Película + 1 sesión previa** → diálogo "Ya existe un proyecto" con Abrir / Reanalizar.
- **Serie + N sesiones previas** → directo al modal de series con la lista de existentes. Cada candidato MPLS/m2ts con sesión previa muestra badge **✓ Existe** y arranca desmarcado. El usuario marca solo lo que quiera añadir o rehacer.

### Series TV multi-episodio

Cuando el modo Serie está activo y hay varios candidatos:

1. **Identificación de la serie** — búsqueda en TMDb (top-5 candidatos con poster) o entrada manual si TMDb no la encuentra.
2. **Selección de temporada** — combo con conteo de episodios por temporada.
3. **Mapeo MPLS↔episodio** — smart-match por offset (prueba todos los offsets de inicio E01/E02/E03/… y elige el que minimiza |Δ runtime| con la temporada TMDb). Funciona para discos 2+ de una temporada donde los episodios son E04-E06 en vez de E01-E03.

Indicador de confianza por fila (recalculado en cada cambio):
- 🟢 **alto** — runtime del MPLS coincide con TMDb ±1 min.
- 🟡 **bajo** — runtime difiere o TMDb sin runtime para ese episodio.
- ⚪ **sin match** — TMDb sin datos.

El backend monta el origen una vez, analiza cada MPLS/m2ts seleccionado completamente y crea N sesiones en `pending`. Coste: ~30s + 15-30s por episodio.

Si el usuario marca algún episodio que ya existe, dialog con 3 opciones:
- **🗑️ Reemplazar** — borra las sesiones existentes y crea unas nuevas (perdiendo edits / historial).
- **⏭ Saltar existentes** — solo crea las nuevas.
- **Cancelar**.

**Nomenclatura Plex/Jellyfin compatible:**

```
/mnt/output/
  Mad Men (2007)/
    Season 01/
      Mad Men (2007) - S01E01 - Smoke Gets in Your Eyes [DV FEL].mkv
      Mad Men (2007) - S01E02 - Ladies Room [DV FEL].mkv
```

Caracteres prohibidos en NTFS/SMB (`/\\:*?"<>|`) se sanitizan; los acentos UTF-8 se preservan (compatible con ext4/ZFS).

### Reglas de selección automática

**Audio:** Solo Castellano + VO. Por idioma se elige la pista de mayor calidad (TrueHD Atmos > DD+ Atmos > DTS-HD MA > DTS > DD). Castellano = default; VO = no default. Tag `(DCP 9.1.6)` solo en TrueHD Atmos Castellano.

**Subtítulos:** Detección de forzados por estructura de bloques Blu-ray. Orden de inclusión: Forzados Castellano → Completos VO → Completos Castellano → Forzados VO → Completos Inglés (si VO ≠ Inglés).

**Capítulos:** Extraídos del MPLS en Fase A. Si no hay (caso M2TS suelto), generación cada 10 min. Botones "Nombres genéricos" + "Restaurar del disco" para deshacer ediciones.

### Pipeline de análisis (Fase A)

Pipeline de 4 herramientas con el origen abierto:

1. **mkvmerge -J** — identificación de pistas, codecs, idiomas, estructura.
2. **MediaInfo** — bitrate real por pista, HDR10 metadata (MaxCLL/MaxFALL, mastering display primaries+luminance), detección definitiva Atmos/DTS:X via `Format_Commercial`, channel layout.
3. **dovi_tool** — análisis RPU: Profile (4/5/7/8), FEL vs MEL, CM version (v2.9/v4.0), L1/L2/L5/L6/L8/L9/L10/L11/L254.
4. **mkvextract** — capítulos del MPLS con timestamps precisos (solo si el origen tiene MPLS).

MediaInfo y dovi_tool son opcionales: si fallan, el análisis sigue con datos de mkvmerge.

Para M2TS la duración se resuelve con cascada **mkvmerge → MediaInfo → ffprobe** con sanity check de bitrate (descarta valores absurdamente cortos típicos de m2ts con PCR discontinuo donde la herramienta lee solo el primer chunk).

### Pipeline de extracción (Fases D+E)

- **Ruta directa** (con reordenación o exclusión de pistas): mkvmerge lee el origen → MKV final en una pasada con mapeo por idioma+codec (no posicional).
- **Ruta intermedio** (sin reordenación): MKV intermedio → mkvpropedit sobre las cabeceras → `mv` al output.

Progreso real del subprocess (`--gui-mode` parseado a `Progress: XX%`), cancelación atómica del subprocess activo, validación final automática (`mkvmerge -J` + `mkvextract` sobre el MKV resultante comparando contra lo configurado en la sesión).

El panel de cola del sidebar muestra una franja de fases con estado source-aware: para ISO se muestran montar → mkvmerge → desmontar; para BDMV/M2TS las fases mount/unmount aparecen marcadas como **omitidas** (no hay montaje real).

---

## Tab "Consultar / Editar MKV"

### File browser unificado

Dos roots:

- **📚 Biblioteca** (`/mnt/library`) — recursivo de la raíz definida en `LIBRARY_PATH`.
- **📦 Output** (`/mnt/output`) — salida del converter.

Selección por click + botón "Seleccionar" (o doble-click). Búsqueda incremental dentro del directorio actual. Breadcrumb navegable.

### Edición in-place (sin re-encoding)

Solo se modifica metadata vía `mkvpropedit` (instantáneo):

- Renombrar pistas de audio y subtítulos.
- Cambiar flags `default` / `forced`.
- Editar capítulos (timeline interactivo, añadir/eliminar, renombrar, ajustar timestamps).
- "Nombres genéricos" reemplaza nombres custom por `Capítulo XX`.
- "Deshacer cambios" revierte al estado del análisis. "Cerrar" avisa si hay cambios pendientes.

Si el MKV está en `/mnt/library` (read-only), la app copia primero a `/mnt/output` con progreso real y cancelación cooperativa antes de aplicar mkvpropedit.

### Radiografía DV+HDR

Panel detallado con cinco secciones:

1. **Stream técnico** — profile, CM version, frames, duración, FPS, escenas, bit depth, codec, RPU size, EL info.
2. **Cadena de mastering** — 3 cards (Master display / Container HEVC / DV target L10) con primaries, peak/min nits, transfer + bit-depth. Chip "P3 ↑ BT.2020" cuando hay expansión de gamut. Filas auxiliares: trim targets DV (chips ámbar de L2 target_max_pq), HDR10 metadata (MaxCLL/MaxFALL del SEI), L11 content type.
3. **Active area (L5)** — offsets T/B/L/R + área activa + aspect ratio + simetría, con visualizador SVG del frame.
4. **CMv4.0 levels** (solo si v4.0) — pills de presencia + visualización logarítmica de L8 trim targets.
5. **Perfil de luminancia DV L1** — sparkline con 3 curvas superpuestas (peak/avg/min), líneas de referencia (HDR10 MaxCLL/MaxFALL, L2 trims, L6 master), tooltip hover con crosshair, mini-card con percentiles (peak/p99/p95/p50/avg) + clasificación de escenas (SDR-like <100n / midtone 100-300n / highlight ≥300n) y histograma de distribución.

### Perfil de luminancia (extracción on-demand)

Botón "Analizar ahora" lanza el análisis completo (3-15 min según peli):

1. ffmpeg extrae HEVC del MKV.
2. dovi_tool extract-rpu sobre el HEVC.
3. dovi_tool export → JSON → parseo de L1 max_pq/avg_pq/min_pq por frame.

Polling resiliente: chained-await (no `setInterval` — evita races de respuestas tardías), guard monotónico anti-rollback de step, fallback en `state.result` para recuperar el resultado si el POST timeout.

> **Nota:** los valores L1 max_pq son la metadata DV codificada por el colorista — no la luminancia que verás tras tone-mapping. Algunas películas (Blade Runner 2049, etc.) están etiquetadas conservadoramente y pueden mostrar peaks bajos aunque las medidas reales en pantalla sean más altas.

---

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

Asíncrono (`running_phase="preflight"`), bloqueante (otras fases no arrancan en paralelo), cancelable.

### Auto-pipeline backend-driven

Cuando el modo auto está activado (toggle del modal "Nuevo proyecto"), el servidor encadena por sí mismo todas las fases que no requieren intervención manual: pre-flight → Fase A → Fase B → trust gates → (Fase C/D solo si los gates no pasan) → Fase F → Fase G → Fase H → done. El cliente no participa en la orquestación.

El job sobrevive a Mac sleep, cierre de pestaña, navegador crashado, pérdida de WiFi. El estado se persiste atómicamente en `/config/cmv40/{session_id}.json` tras cada fase y el log se escribe en disco con throttle (saves inmediatos en marcadores clave + flush al terminar cada fase). Al reabrir la app se hidrata el panel desde disco y se reengancha al WebSocket de log en ~1-3 segundos.

Recovery tras reinicio del servidor: las sesiones que estaban con `running_phase != null` se marcan como error con mensaje "Sesión interrumpida por reinicio del servidor" (comportamiento intencional — no se reanudan auto-mágicamente para evitar arranques no deseados tras un kill).

### Fases del pipeline

1. **Analizar origen** — ffmpeg + dovi_tool extract-rpu del MKV origen.
2. **Proporcionar RPU target** — drive / mkv / path. Reutiliza pre-flight si pasó.
3. **Extraer BL/EL** — dovi_tool demux + per-frame data muestreado (saltada con bins drop-in).
4. **Verificar sincronización** — gráfico interactivo con dos curvas, zoom, detección automática de offset, correcciones acumulativas, métrica de confianza (correlación Pearson sobre MaxCLL).
5. **Aplicar corrección** — `dovi_tool editor` con `remove`/`duplicate` (parte de Fase D, no avanza fase).
6. **Inyectar RPU** — drop-in con `inject-rpu` (sustitución íntegra del bin) o merge frame-a-frame con `dovi_tool editor --allow-cmv4-transfer`. Los niveles transferidos dependen del source workflow:
   - **Source P7 FEL** → `[1, 2, 3, 6, 8, 9, 10, 11, 254]` (incluye L1/L2/L6 deliberadamente — el grading WEB restaurado refina la stats L1 legacy del BD)
   - **Source P7 MEL / P8** → `[3, 8, 9, 11, 254]` (solo niveles CMv4.0-exclusivos + L3 + marker; L1/L2/L5/L6 del BD se preservan porque describen tus píxeles)
7. **Remux final** — `dovi_tool mux` + mkvmerge preservando audio/subs/capítulos.
8. **Validar** — fast path para drop-in (ffprobe + mkvmerge -J, ~segundos) o extract-rpu COMPLETO del HEVC pre-mux con validación rigurosa para merge (frame count ±2 vs expected, CM v4.0, el_type según workflow, **L8 presente**). Si falla, el `.mkv.tmp` se preserva para inspección.

### Sistema de trust

Tras Fase B, el bin se clasifica en `target_type`:

| Tipo | Detección | Comportamiento |
|---|---|---|
| `trusted_p7_fel_final` | P7 FEL + CMv4.0 + L8 + gates OK | Drop-in: skip Fase D + skip merge en F |
| `trusted_p7_mel_final` | P7 MEL + CMv4.0 | Skip Fase D si gates OK |
| `trusted_p8_source` | P8 + CMv4.0 + L8 | Skip Fase D si gates OK, Fase F hace merge |
| `generic` | P8 sin L8 / P5 | Flujo completo (revisión visual + merge) |
| `incompatible` | CMv2.9 | Aborta (cubierto por pre-flight) |

Gates críticos: `frames` (Δ=0), `cm_version` (v4.0), `has_l8`, `l5_div` (≤30 px). Soft: `l6_div` (±50 nits), `l1_div` (±5%).

Override manual: `trust_override = "force_interactive"` fuerza ruta completa aunque el bin sea trusted.

### Validación L8 post-merge (Fase H)

Cuando el path clásico (merge) llega a la fase de validación, se hace `dovi_tool extract-rpu` completo sobre el HEVC pre-mux y se exige `has_l8 == True` en el RPU resultante. Es defensa en profundidad contra un eventual bug de `dovi_tool editor` que dejara el RPU marcado como v4.0 pero sin los trims L8 que dan utilidad real al upgrade. Si falla, el `.mkv.tmp` queda preservado para inspección.

### Modelo de decisión Mantener MKV / Inyectar RPU

Tras descargar el bin, la app ejecuta `dovi_tool export -d all` y analiza la riqueza real del L8 (no se fía del nombre del fichero ni del tag del repo). Decide entre 4 acciones según un árbol de 3 preguntas:

1. **¿El bin tiene L8 trabajado?** Si todos los trims son neutros → bin sintético → **Mantener MKV actual** (el reproductor compatible con CMv4.0 hace la conversión al vuelo con el mismo resultado).
2. **¿Profile match (source ↔ bin)?** P7 FEL↔P7 FEL, P7 MEL↔P7 MEL, P8↔P8 → drop-in posible. Mismatch → siempre merge.
3. **¿L2 idéntico entre source y bin?** Comparación byte-a-byte. Idéntico → **Inyectar RPU (rápido)** sustituyendo el RPU del bin íntegro (~30s). Diferente → **Inyectar RPU (preserva L2)** transfiriendo solo niveles CMv4.0 `[3,8,9,11,254]` manteniendo el L2 original.

Clasificación de calidad del bin (se aplica como sufijo al filename del MKV final):

| Tier | Filename label | Criterio |
|---|---|---|
| FULL | `[CMv4 FULL]` | `target_mid_contrast` o `clip_trim` poblados (campos exclusivos CMv4.0). Sub-variante "FULL minimal" cuando hay pocos combos únicos (≥3) pero con trims significativos (>50 unidades del neutro) |
| CORE+ | `[CMv4 CORE+]` | Grading dinámico intenso (>=0.1 combos/shot o >=400 absolutos) |
| CORE | `[CMv4 CORE]` | Master estándar streaming con trims básicos |
| (sintético) | — | Bin no aporta sobre conversión al vuelo, recomendación: mantener |

El usuario ve la recomendación en una card en el panel del proyecto con badge de color + razón legible. Cuando la recomendación es "mantener", aparecen dos botones: `[✓ Mantener MKV actual]` (cierra el proyecto sin tocar el MKV) y `[🔬 Inyectar RPU igualmente]` (override del usuario para procesar igualmente, útil para archivar la versión CMv4.0 completa).

El campo `output_workflow` de la sesión distingue cómo terminó cada proyecto: `keep_cmv29` (mantener) · `restore_dropin` (drop-in) · `restore_merge` (merge selectivo).

### Auditoría retroactiva (`tools.audit_cmv40_bins`)

Herramienta CLI para revisar todas las sesiones CMv4.0 históricas y reclasificarlas con el modelo actual:

```bash
docker exec hdo-iso-converter python3 -m tools.audit_cmv40_bins

# Solo jobs desperdiciados (procesados con bins sintéticos)
docker exec hdo-iso-converter python3 -m tools.audit_cmv40_bins --filter-keep

# Re-descargar bins desde Drive si no quedan en workdir
docker exec hdo-iso-converter python3 -m tools.audit_cmv40_bins --redownload

# Detalle de un caso concreto: vuelca combos L8 con deltas vs neutro
docker exec hdo-iso-converter python3 -m tools.audit_cmv40_bins --detail "Black Phone 2"
```

Útil para calibrar umbrales del classifier o decidir qué jobs históricos rehacer.

### Recomendación CMv4.0 (sheet live + Drive)

- **Sheet DoviTools** parseado live (XLSX + openpyxl para preservar hyperlinks; fallback HTML/CSV/disk cache).
- **Fuzzy match** del filename: max(SequenceMatcher, token-set Jaccard, containment) sobre acentos strippeados, normalización de números romanos y stop-words.
- **Multi-candidato TMDb** (top-5) con umbrales adaptativos según presencia/ausencia de año.
- **Dedup** por (slug, año) — distingue p.ej. El Rey León 1994 vs 2019.

---

## Stack técnico

- **Backend:** Python 3.10+ (ubuntu:22.04), FastAPI, uvicorn
- **Frontend:** Vanilla JS ES6+, Sortable.js (CDN), sin framework ni build step
- **Herramientas:** mkvmerge, mkvpropedit, mkvextract, mediainfo, ffmpeg, ffprobe, dovi_tool 2.3.2
- **Acceso al ISO:** Loop mount directo (`mount -t udf -o ro,loop`) — requiere `privileged: true` en Docker
- **Integraciones externas (opcionales):** TMDb (poster/sinopsis), Google Drive v3 + Sheets v4 / openpyxl (repo DoviTools)

## Estructura del proyecto

```
hdo-iso-converter/                ← repo / docker image (técnico)
├── .github/workflows/
│   └── publish-docker.yml        ← Publica imagen a ghcr.io en cada release
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example
│   └── entrypoint.sh
├── app/
│   ├── main.py                   ← FastAPI + WebSocket + endpoints (3 tabs)
│   ├── models.py                 ← Pydantic models
│   ├── storage.py                ← Persistencia JSON + fingerprint
│   ├── queue_manager.py          ← Cola FIFO asyncio
│   ├── dev_fixtures.py           ← Fixtures cuando DEV_MODE=1
│   ├── phases/
│   │   ├── iso_mount.py          ← Loop mount/umount UDF + abstracción Source
│   │   ├── phase_a.py            ← Análisis completo del origen
│   │   ├── phase_b.py            ← Reglas de selección automática
│   │   ├── phase_d.py            ← Extracción mkvmerge desde origen
│   │   ├── phase_e.py            ← Escritura final + validación
│   │   ├── mkv_analyze.py        ← Tab 2: análisis + edición MKV
│   │   ├── rpu_analyze.py        ← Tab 3: análisis L2/L8 del bin
│   │   └── cmv40_pipeline.py     ← Tab 3: pipeline completo CMv4.0
│   ├── services/
│   │   ├── settings_store.py     ← /config/app_settings.json
│   │   ├── tmdb.py               ← TMDb search + details (movies + tv)
│   │   ├── rec999_sheet.py       ← Sheet DoviTools live
│   │   ├── rec999_drive.py       ← Drive API v3 listado + descarga
│   │   ├── rec999_drive_match.py ← Fuzzy match filename → bin
│   │   └── cmv40_recommend.py    ← Orquesta filename → TMDb → sheet
│   ├── tools/
│   │   └── audit_cmv40_bins.py   ← CLI: auditoría retroactiva
│   └── static/
│       ├── index.html
│       ├── app.js
│       └── style.css
├── archive/                      ← Specs históricas
├── CLAUDE.md
├── README.md
└── run_local.sh
```

## API REST (resumen)

### Tab 1 — Crear MKV
- `GET /api/sources` — listado de orígenes en `ISOS_PATH` clasificados por tipo (iso / bdmv_folder / m2ts)
- `POST /api/check-duplicate` — fingerprint + lista de sesiones existentes
- `POST /api/disc-probe` — clasifica el origen, identifica candidatos a episodio (acepta `media_type_hint`)
- `GET /api/disc-probe/progress` — polling del paso actual del probe
- `GET /api/tv-search` · `/api/tv-details/{id}` · `/api/tv-season/{id}/{n}` — TMDb TV endpoints
- `POST /api/create-series-sessions` — crea N sesiones de un golpe (acepta `mode`: `add_only` / `replace` / `skip_existing`)
- `POST /api/analyze` — flujo película (Fase A+B)
- `GET/PUT/DELETE /api/sessions/{id}`, `POST /api/sessions/{id}/execute|cancel`
- `WS /ws/{id}` — log en vivo del job

### Tab 2 — Editar MKV
- `GET /api/library/browse?root=library|output&path=...` — file browser
- `POST /api/mkv/analyze` — análisis (mkvmerge -J + MediaInfo + dovi_tool)
- `POST /api/mkv/light-profile` — extrae perfil L1 (peak/avg/min + percentiles + refs)
- `GET /api/mkv/light-profile/progress` — polling del análisis on-demand
- `POST /api/mkv/apply` — aplicar ediciones (mkvpropedit + copy desde library si aplica)
- `POST /api/mkv/apply/cancel` — cancelación cooperativa de la copia
- `GET /api/mkv/apply/progress` — polling de la copia + edición

### Tab 3 — CMv4.0
- `GET /api/cmv40`, `POST /api/cmv40/create` (acepta `auto_pipeline` + `pending_target` para que el backend orqueste solo), `GET/DELETE /api/cmv40/{id}`
- `POST /api/cmv40/{id}/auto-pipeline` — toggle del modo automático mid-pipeline
- `POST /api/cmv40/{id}/preflight-target` — pre-flight bloqueante (kind: drive | path | mkv); con auto on encadena Fase A al terminar
- `POST /api/cmv40/{id}/analyze-source` — Fase A
- `POST /api/cmv40/{id}/target-rpu-{path|mkv|from-drive}` — Fase B
- `POST /api/cmv40/{id}/extract` — Fase C
- `GET /api/cmv40/{id}/sync-data` — datos del chart de Fase D
- `POST /api/cmv40/{id}/{apply-sync|reset-sync|mark-synced|inject|remux|validate|cleanup}` — fases siguientes
- `POST /api/cmv40/{id}/cancel` — mata subprocess activo
- `WS /ws/cmv40/{id}` — log en vivo (replay automático desde watermark al reconectar)

### Settings + utilidades
- `GET/PUT /api/settings` — configuración de API keys (TMDb / Google)
- `GET /api/health` — healthcheck
- `GET /api/version` · `GET /api/version/check-updates` — versión actual + comprobación de actualizaciones contra GitHub Releases

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
