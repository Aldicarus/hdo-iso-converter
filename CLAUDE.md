# HDO ISO Converter — Reglas del proyecto (v1.9)

## Nombre de la aplicación
La aplicación se llama **HDO ISO Converter**. Este nombre debe usarse en:
- Título del documento HTML
- Texto de bienvenida en la UI
- README y documentación
- Mensajes de la consola del pipeline

El nombre interno del repositorio y ficheros puede seguir siendo `ISO2MKVFEL` por compatibilidad.

## Descripción
Aplicación web multi-herramienta en contenedor Docker (amd64/QNAP) para procesar contenido UHD Blu-ray. Organizada en tres herramientas accesibles desde tabs:
- **Tab 1 — Crear MKV:** Convierte ISOs UHD Blu-ray a MKV con selección automática de pistas y soporte Dolby Vision FEL.
- **Tab 2 — Editar MKV:** Editor de propiedades de ficheros MKV existentes — metadatos de pistas, flags, títulos, capítulos. Sin re-encoding.
- **Tab 3 — CMv4.0 BD:** Pipeline para inyectar RPU Dolby Vision CMv4.0 en un MKV con CMv2.9 del Blu-ray original, con sincronización visual frame-a-frame y multi-proyecto.

---

## Stack técnico
- **Backend:** Python 3.10+ (ubuntu:22.04), FastAPI, uvicorn
- **Frontend:** Vanilla JS ES6+, Sortable.js (CDN), sin framework ni build step
- **Contenedor:** Docker sobre QNAP NAS x86 (amd64), puerto 8090 (configurable)
- **Herramientas:**
  - `mkvmerge` — análisis de MPLS + extracción a MKV
  - `mkvpropedit` — edición in-place de metadatos (Tab 2)
  - `mkvextract` — extracción de capítulos
  - `mediainfo` — metadata extendida: bitrate real, HDR10, codecs comerciales (v1.6+)
  - `ffmpeg` — extracción de Enhancement Layer para análisis DV
  - `dovi_tool` — análisis RPU Dolby Vision: Profile, FEL/MEL, CM version (v1.6+)
- **Acceso al ISO:** Loop mount directo (`mount -t udf -o ro,loop`) — requiere `privileged: true` en Docker
- **Integraciones externas (v1.8):**
  - **TMDb API** — traducción ES→EN de títulos + ficha extendida (poster, sinopsis, géneros, rating) en la cabecera de proyectos de los 3 tabs. Opcional (key en ⚙︎ Configuración)
  - **Google Drive API v3** — listado + descarga de RPUs del repositorio público **DoviTools** (compartido por R3S3T_9999). Opcional (key en ⚙︎ Configuración)
  - **Google Sheets (XLSX export)** — lectura live de la hoja de recomendaciones de DoviTools con extracción de hyperlinks vía openpyxl. Sin auth (endpoint público)
- **~~BDInfoCLI~~:** eliminado en v1.5 — crasheaba con ISOs custom/stripped. Reemplazado por `mkvmerge -J`
- **~~API QTS File Station~~:** eliminada en v1.4
- **~~makemkvcon~~:** eliminado en v1.3

## Estructura del proyecto
```
ISO2MKVFEL/
├── CLAUDE.md
├── spec_paso1_iso_to_mkv_v1.3.md   ← especificación de referencia (v1.3)
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example
│   └── entrypoint.sh
└── app/
    ├── main.py              ← FastAPI app + WebSocket + endpoints Tab 1 y Tab 2
    ├── requirements.txt
    ├── models.py            ← Pydantic models (Session, BDInfoResult, MkvAnalysisResult...)
    ├── storage.py           ← Persistencia JSON en /config + fingerprint ISO
    ├── queue_manager.py     ← Cola FIFO asyncio para Fases D+E
    ├── phases/
    │   ├── iso_mount.py       ← Loop mount/umount de ISOs UDF 2.50
    │   ├── phase_a.py         ← Análisis: mkvmerge -J + MediaInfo + dovi_tool + capítulos
    │   ├── phase_b.py         ← Motor de reglas automáticas (audio, subs, capítulos, nombre)
    │   ├── phase_d.py         ← Extracción: mkvmerge desde MPLS montado
    │   ├── phase_e.py         ← Escritura final: flags, metadatos, validación
    │   ├── mkv_analyze.py     ← Tab 2: análisis + edición de MKVs (mkvmerge + MediaInfo)
    │   └── cmv40_pipeline.py  ← Tab 3: pipeline CMv4.0 (ffmpeg + dovi_tool + sync)
    ├── dev_fixtures.py      ← ⚠️ TEMPORAL (DEV_MODE=1): ISOs fake + sesiones fake
    ├── services/            ← Integraciones externas (v1.8)
    │   ├── settings_store.py    ← Persistencia de API keys en /config/app_settings.json
    │   ├── tmdb.py              ← TMDb search + details (poster, sinopsis, géneros, rating)
    │   ├── rec999_sheet.py      ← Sheet DoviTools: XLSX (con hyperlinks) > HTML > CSV
    │   ├── rec999_drive.py      ← Google Drive API v3 (listado + descarga de RPUs)
    │   ├── rec999_drive_match.py ← Fuzzy match de título con candidatos en el repo
    │   └── cmv40_recommend.py   ← Orquesta filename → TMDb → match contra sheet
    └── static/
        ├── index.html       ← SPA completa (Tab 1 + Tab 2 + Tab 3 + modales + config)
        ├── app.js           ← Toda la lógica UI
        └── style.css
```

## Volúmenes Docker
| Ruta en contenedor | Tipo | Descripción |
|---|---|---|
| `/mnt/isos` | read-only | ISOs de origen en el NAS |
| `/mnt/output` | read-write | MKVs finales (Tab 1 output + Tab 2/3 input) |
| `/mnt/tmp` | read-write | MKV intermedios + artefactos Tab 3 (preferiblemente SSD) |
| `/mnt/cmv40_rpus` | read-only | Ficheros RPU CMv4.0 externos (Tab 3, legacy — preferir repo DoviTools) |
| `/config` | read-write | Sesiones persistentes + caché sheet/drive/TMDb + `app_settings.json` |

El volumen `/mnt/cmv40_rpus` sigue soportado pero desde v1.8 el usuario puede descargar directamente desde el repositorio **DoviTools** en Google Drive (más cómodo, no requiere mantener la carpeta local).

---

## Tab 1 — Crear MKV

### Flujo en tres pasos

```
1. Crear proyecto (modal "Nuevo proyecto" → mkvmerge -J + reglas)
       ↓
2. Configurar y ejecutar (revisión + cola FIFO + monitorización)
       ↓
3. Revisar resultados (validación + historial + re-ejecución)
```

### Paso 1 — Crear proyecto
- Se hace **exclusivamente desde el modal "Nuevo proyecto"**, nunca desde el sidebar.
- **Detección de duplicados**: antes de analizar, `POST /api/check-duplicate` calcula la huella SHA-256 del ISO (primer 1 MB + tamaño) y busca sesiones existentes con la misma huella. Si se detecta duplicado, ofrece "Abrir existente" o "Re-analizar".
- **Modal de progreso**: al analizar, se muestra un modal con 4 pasos animados (Montar ISO → Identificar pistas → Extraer capítulos → Aplicar reglas).
- **No se persiste nada hasta éxito**: si Fase A falla, no se crea fichero JSON. Sin sesiones huérfanas.
- **Feedback**: modal de progreso durante el análisis. Si falla, toast de error.

### Paso 2 — Configurar y ejecutar
- El análisis (Fases A+B) se ejecuta al crear el proyecto y produce configuración inicial.
- El usuario revisa y ajusta en el panel de proyecto (sub-tab).
- **Botón "🔬 Datos ISO"**: junto a la phase strip, abre modal con datos de diagnóstico en 3 secciones: mkvmerge -J raw, post-heurística, resultado de reglas.
- Hasta 5 proyectos abiertos simultáneamente en sub-tabs.
- **Botón de ejecutar adaptativo**: "▶️ Confirmar y ejecutar" (pending), "↻ Re-ejecutar" (done), "⏳ En ejecución…" (running/queued, deshabilitado).
- **Cola de ejecución**: `QueueManager` (FIFO) con drag & drop para reordenar.
- **Cancelación de ejecución**: botón "🛑 Cancelar" visible en la fase activa del pipeline. Mata el subprocess, limpia temporales, desmonta ISO.
- **Indicadores de ejecución**: spinner animado en tab "Crear MKV", en el proyecto del sidebar, en el subtab del proyecto, y en la sección "En curso" del panel Cola.

### Paso 3 — Revisar resultados
- **Validación final del MKV**: al completar, `mkvmerge -J` + `mkvextract` sobre el MKV final. Compara pistas, idiomas, flags y capítulos contra lo esperado. Si hay discrepancias, escribe bloque de diagnóstico detallado en el log.
- **Banner contextual**: running/queued → azul con "Ver progreso" + "Cancelar". done/error → en historial.
- **Tabla de historial de ejecuciones** con tiempos por fase y visor de log con coloreado semántico.

---

## Tab 2 — Editar MKV

### Arquitectura
- **Sin persistencia**: estado ephemeral en el frontend (`mkvProject`). Un solo MKV abierto a la vez.
- **Backend stateless**: 3 endpoints bajo `/api/mkv/`.
- **Edición in-place**: solo `mkvpropedit` (O(1), instantáneo). Sin remux.
- **Análisis extendido**: `mkvmerge -J` + MediaInfo (bitrate, format_commercial, HDR).
- **Sin sidebar**: Tab 2 ocupa todo el ancho. Botón "Abrir MKV" centrado.

### Flujo
1. "Abrir MKV" → modal picker lista MKVs de `/mnt/output`
2. `mkvmerge -J` + `mkvextract chapters` + MediaInfo → panel de edición
3. Editar → "Aplicar cambios" → modal informado con output de mkvpropedit
4. "Deshacer cambios" revierte al estado original del análisis
5. "Cerrar" avisa si hay cambios pendientes

### Endpoints
- `GET /api/mkv/files` — lista MKVs en `/mnt/output`
- `POST /api/mkv/analyze` — identifica pistas + capítulos + enriquece con MediaInfo
- `POST /api/mkv/apply` — aplica ediciones (solo mkvpropedit)

### Editable
- Pistas audio: nombre, flag default
- Pistas subtítulos: nombre, flags default/forced
- Capítulos: timeline interactiva, añadir/eliminar, editar nombres/timestamps
- Botón "Nombres genéricos": reemplaza nombres custom por "Capítulo XX"

### Info mostrada (read-only)
- Fichero: nombre, tamaño, duración
- Vídeo: codec, resolución, bitrate real, HDR10 (MaxCLL/MaxFALL), Dolby Vision
- Audio: codec comercial (MediaInfo), bitrate real, channel layout, compresión
- Subtítulos: codec, idioma, tipo (forzados/completos)

---

## Tab 3 — CMv4.0 BD (inyección RPU Dolby Vision CMv4.0)

### Objetivo
Partir de un MKV con DV Profile 7 FEL **CMv2.9** (producto estándar del Blu-ray UHD) y sustituir su RPU por uno **CMv4.0** (que añade metadata de tone-mapping L8-L11). Los RPUs CMv4.0 los genera la comunidad (p. ej. el usuario **REC999**) y se proporcionan como `.bin` externos o extraídos de otros MKVs.

**El punto crítico es la sincronización**: el RPU target puede tener N frames distintos al vídeo (típico por logos de estudio o versiones streaming). Si no se alinea frame-a-frame, el resultado es incorrecto — escenas brillantes se ven oscuras y viceversa. La Fase D aporta un gráfico interactivo + métrica de confianza para validar la alineación antes de inyectar.

### Volúmenes
- Workdir de artefactos: `/mnt/tmp/cmv40/{session_id}/`
- RPUs externos (opcional): `/mnt/cmv40_rpus/` (read-only)
- Sesiones: `/config/cmv40/{session_id}.json`

### Artefactos por proyecto
Cada proyecto CMv4.0 vive en `/mnt/tmp/cmv40/{session_id}/`:

```
source.hevc            ← HEVC extraído del MKV origen (BL+EL+RPU)
BL.hevc                ← Base Layer tras demux
EL.hevc                ← Enhancement Layer CMv2.9 tras demux
RPU_source.bin         ← RPU original CMv2.9 extraído del MKV origen
RPU_target.bin         ← RPU CMv4.0 proporcionado por el usuario
RPU_synced.bin         ← RPU corregido tras dovi_tool editor (opcional)
EL_injected.hevc       ← EL con nuevo RPU inyectado
per_frame_data.json    ← MaxCLL/MaxFALL por frame (muestreado cada 20) para el chart
editor_config.json     ← JSON de corrección aplicado (remove/duplicate)
output.mkv             ← MKV final (se mueve a /mnt/output al validar)
```

### Fases del pipeline (A-H)

Cada fase produce artefactos reutilizables y tiene endpoint independiente. El usuario controla cada transición explícitamente — sin cascadas automáticas.

1. **Fase A — Analizar MKV origen**: `ffmpeg -map 0:v:0 -c:v copy ... source.hevc` + `dovi_tool extract-rpu` + `dovi_tool info --summary`. Captura `source_fps`, `source_frame_count`, profile y CM version originales.
2. **Fase B — Proporcionar RPU target**: dos opciones UX (tabs):
   - **Desde carpeta NAS** (`/mnt/cmv40_rpus/*.bin`)
   - **Extraer de otro MKV** que ya tenga CMv4.0 (ffmpeg + extract-rpu)
3. **Fase C — Extraer BL/EL + datos per-frame**: `dovi_tool demux` + exportación muestreada de MaxCLL/MaxFALL de ambos RPUs para el chart.
4. **Fase D — Verificar sincronización** (UX clave): gráfico Canvas custom con dos curvas superpuestas (origen rojo, target azul). Incluye:
   - **Zoom**: presets 30s / 1min / 5min / 30min / Todo + inputs manuales
   - **Detección automática de offset** por cross-correlation
   - **Correcciones acumulativas** (cada "Aplicar" suma a las previas)
   - **Botón "Resetear al original"** que descarta todas las correcciones
   - **Panel de confianza** basado en correlación de Pearson sobre MaxCLL (insensible a diferencias absolutas, sensible a desalineación temporal)
   - **Criterio para avanzar**: `Δ frames = 0` Y `confianza ≥ 85%`
5. **Fase E — Aplicar corrección** (parte de D): `dovi_tool editor -j editor_config.json` con `remove`/`duplicate`. NO avanza de fase — el usuario sigue iterando hasta pulsar "Confirmar sync".
6. **Fase F — Inyectar RPU**: `dovi_tool inject-rpu -i EL.hevc --rpu-in RPU_final.bin`.
7. **Fase G — Remux final**: `dovi_tool mux --bl BL.hevc --el EL_injected.hevc` + `mkvmerge -o output.mkv --no-video source.mkv` (preserva audio/subs/capítulos del origen).
8. **Fase H — Validación**: extrae RPU del MKV resultante y verifica `CM version == v4.0`. Si OK, mueve el MKV a `/mnt/output/`.

### Estados de la sesión

- `phase`: última fase completada — `created → source_analyzed → target_provided → extracted → sync_verified → injected → remuxed → validated → done`
- `running_phase`: fase ejecutándose ahora mismo (bloquea la UI en modo modal overlay)
- `error_message`: error de la última acción intentada (no bloquea, se puede descartar)
- `archived`: true tras cleanup — proyecto en modo solo lectura, no se pueden rehacer fases
- `sync_config`: dict con la corrección acumulada (remove/duplicate ranges)
- `sync_delta`: diferencia de frames actual (target - source)

### UI

- **Multi-proyecto con sidebar** (como Tab 1): búsqueda, ordenación (por modificado/nombre/fase), filtros (todos / en progreso / completados / errores), iconos por fase en el badge
- **Sub-tabs** para varios proyectos abiertos en paralelo (máx. 5)
- **Cards apiladas por fase**: todas visibles con estado (done ✅ / active ▶️ / pending 🔒), expandibles/colapsables, con resumen en header
- **Botón "🔄 Rehacer"** en cada fase done: invalida datos y borra artefactos de fases posteriores con modal de previsualización
- **Overlay modal bloqueante** cuando `running_phase != null`: spinner + título de la fase + log en vivo + botón Cancelar. Tamaño fijo, no depende del contenido.
- **Nombre del MKV de salida**: editable en el header del proyecto (no en modal de creación), bloqueado cuando `done` o `archived`

### Endpoints (resumen)

```
POST   /api/cmv40/create
GET    /api/cmv40
GET    /api/cmv40/{id}                      (incluye campo artifacts con sizes)
DELETE /api/cmv40/{id}                      (?clean_artifacts=true para borrar workdir)
POST   /api/cmv40/{id}/rename-output        (edita output_mkv_name)
POST   /api/cmv40/{id}/analyze-source       (Fase A)
GET    /api/cmv40/rpu-files                 (lista /mnt/cmv40_rpus/*.bin)
POST   /api/cmv40/{id}/target-rpu-path      (Fase B opción 1)
POST   /api/cmv40/{id}/target-rpu-from-mkv  (Fase B opción 2)
POST   /api/cmv40/{id}/extract              (Fase C)
GET    /api/cmv40/{id}/sync-data            (per_frame_data + confidence + suggested_offset)
POST   /api/cmv40/{id}/apply-sync           (Fase E, correcciones acumulativas)
POST   /api/cmv40/{id}/reset-sync           (descartar todas las correcciones)
POST   /api/cmv40/{id}/mark-synced          (usuario confirma sync OK → avanza a sync_verified)
POST   /api/cmv40/{id}/inject               (Fase F)
POST   /api/cmv40/{id}/remux                (Fase G)
POST   /api/cmv40/{id}/validate             (Fase H)
POST   /api/cmv40/{id}/reset-to/{phase}     (rehacer desde una fase)
GET    /api/cmv40/{id}/reset-preview/{phase} (previsualiza artefactos a borrar)
POST   /api/cmv40/{id}/cleanup              (borra workdir → archived=true)
POST   /api/cmv40/{id}/clear-error          (descarta error_message)
POST   /api/cmv40/{id}/cancel               (mata subprocess de running_phase)
WS     /ws/cmv40/{id}                       (streaming de log)
```

### Métricas de sincronización (Fase D)

- **Δ frames** (`target_frame_count - source_frame_count`): alineación temporal exacta
- **Correlación de Pearson** sobre MaxCLL de ambas series: similitud de forma. Insensible a diferencias de escala (los valores absolutos pueden diferir entre CMv2.9 y CMv4.0 por distinto grading) pero sensible a desalineación temporal
- **Umbral de confianza**: 85% (>= 0.85 Pearson). Protege contra RPUs de películas incompatibles

### Reglas y principios

- Cada transición de fase es explícita (click del usuario) — no hay cascadas automáticas
- Los artefactos de fases anteriores se preservan para permitir reentrar tras crash/reinicio
- Cleanup es destructivo e irreversible → archived=true → proyecto en modo solo lectura
- Errores NO bloquean el proyecto: mantienen la fase previa, solo escriben `error_message` descartable
- Validación de CM version target = v4.0 (aviso, no bloqueante)

---

## Integraciones externas (v1.8)

### Settings UI (⚙︎ Configuración)
- Botón engranaje arriba-derecha abre el modal de configuración
- API keys persisten en `/config/app_settings.json` (atomic write con `.tmp` + rename)
- Prioridad: valor en settings.json > env var > vacío
- Nunca se exponen secretos crudos al frontend — solo `{configured, source, last4}`
- Validación live: botón "Probar" contra endpoint oficial de cada API

### TMDb — Ficha de película
- Fetch de **poster (w342), backdrop (w780), sinopsis, géneros, runtime, rating** via `/movie/{id}?language=es-ES`
- Para películas no-ASCII (cine asiático) hace llamada extra `?language=en-US` para obtener título inglés fiable
- **Ficha visible en los 3 tabs** (Crear MKV, Editar MKV, CMv4.0) — reusa `renderTmdbCardHTML`
- Cache persistente en `/config/tmdb_cache.json` (TTL 30 días)
- Backdrop como ambient (opacity 0.10 + blur 24px), overlay sólido 0.82 para legibilidad — colores hex explícitos sin depender de variables CSS

### Recomendación CMv4.0 (Tab 3)
- Parser de filename: trunca tags después del año (`UHD.BluRay.x265`, `[DV FEL]`, etc.)
- Matching fuzzy compuesto: max(SequenceMatcher, token-set Jaccard, containment) sobre acentos strippeados
- Normalización de romanos (II→2, III→3…) y stop-words (the, la, de…)
- Multi-candidato TMDb (top-5) — prueba cada uno contra el sheet
- Umbrales adaptativos: **0.72 año exacto · 0.82 año ±1 · 0.88 sin año**
- Dedup por (slug, año) — permite matches distintos con mismo título (El Rey León 1994 vs 2019)

### Sheet DoviTools (R3S3T_9999)
- El sheet es un **XLSX importado en Drive** (no Sheet nativo) — Sheets API v4 falla con "not supported"
- Prioridad de fetch: **Sheets API v4 (Google key)** → **XLSX + openpyxl (rich-text hyperlinks)** → gviz HTML → CSV export → disk cache
- XLSX + openpyxl es la única vía que preserva los rich-text hyperlinks incrustados manualmente en celdas
- Extracción de URLs: primero `cell.hyperlink.target`, fallback a regex sobre texto plano
- Parseo de 3 secciones: cols 0-4 (no factible), 6-11 (factible), 13-18 ("Not Sure! / probably ok")
- Sheet ID + GID hardcodeados a la hoja de DoviTools (variables de entorno para override)

### Drive DoviTools
- Listado recursivo de `.bin` con Drive API v3 + paginación (profundidad máx 5)
- Caché en memoria + disco (`/config/rec999_drive_cache.json`, TTL 24h)
- Match fuzzy de filename → candidatos rankeados por score (reusa lógica de `cmv40_recommend`)
- Descarga streaming vía `files.get?alt=media&key=...` al workdir del proyecto
- Nueva Fase B3 en el pipeline: `target-rpu-from-drive` (hermana de B1-path / B2-mkv)

### Modal "Nuevo proyecto CMv4.0"
- Ancho 980px, responsive hasta viewport-48
- 3 tabs de target (v1.9 reordenados): **📦 Repo DoviTools** (default) · **🎬 Extraer de MKV** · **📁 Carpeta local** (residual)
- Banner de recomendación con status-badge pill + chips en fila (Fuente · Sync · Verificación) + note con botón "Abrir ↗"
- Footer compacto en 1 línea: toggle auto-pipeline + Cancelar/Crear

---

## Sistema de Trust para bins pre-validados (v1.9)

Spec base: [spec_bins_reset9999_integration_v3.md](spec_bins_reset9999_integration_v3.md).

### Clasificación del target RPU

Tras `_analyze_target_rpu` en Fase B, el bin se clasifica automáticamente en `session.target_type`:

| target_type | Detección | Comportamiento |
|---|---|---|
| `generic` | P8 sin L8 o P5 | Flujo completo (merge CMv4.0 + revisión visual en Fase D) |
| `trusted_p8_source` | Profile 8 + CMv4.0 + L8 presente | Rama B spec: skip Fase D si gates OK, Fase F hace merge clásico |
| `trusted_p7_fel_final` | Profile 7 FEL + CMv4.0 | Rama C-FEL spec: **drop-in**, skip merge en Fase F + skip Fase D |
| `trusted_p7_mel_final` | Profile 7 MEL + CMv4.0 | Rama C-MEL spec |
| `incompatible` | CMv2.9 u otros | No sirve como target (aborta o pide otro bin) |

### Gates de trust

`_evaluate_trust_gates` compara source BD vs target:

- **Críticos** (deben pasar para `target_trust_ok=True`):
  - `frames`: coincidencia exacta (0 tolerancia)
  - `cm_version`: debe ser v4.0
  - `has_l8`: requerido para transfer útil
  - `l5_div`: ≤5 px ok · 5-30 warn · >30 aborta (edición distinta del disco)
- **Soft** (no bloquean):
  - `l6_div`: ±50 nits MaxCLL
  - `l1_div`: ±5% MaxCLL avg

### Skip automático de fases

Si `target_trust_ok=True` y `trust_override == "auto"`:
- **Fase D** (revisión visual) se salta — `_cmv40AutoMarkSynced` avanza directo a `sync_verified`
- **Fase F merge** se salta solo en `trusted_p7_fel_final` — inject directo del bin sin `_merge_cmv40_into_p7`

Se registra en `session.phases_skipped` para UI (phase-strip muestra las fases omitidas con opacity + borde punteado).

### Override manual

`session.trust_override = "force_interactive"` fuerza rama A completa incluso con target trusted — todas las validaciones manuales vuelven a ejecutarse.

---

## Reglas de desarrollo

### Idiomas
- El código (variables, funciones, clases) en **inglés**
- Strings de UI, mensajes de lógica y comentarios en **español**
- Los literales de pistas siguen exactamente la spec: "Castellano TrueHD Atmos 7.1", "Inglés DTS-HD MA 5.1", etc.

### Análisis del disco (Fase A — pipeline extendido v1.6)

Fase A ejecuta un pipeline de 4 herramientas mientras el ISO está montado:

1. **mkvmerge -J** (requerido) — identificación de pistas, estructura del disco
   - Selección inteligente del MPLS: 10 más grandes, elige el de más pistas audio
   - Adaptador BDInfoResult: JSON → modelos que consume phase_b
   - Codecs: mkvmerge → estilo BDInfo (ej: "TrueHD Atmos" → "Dolby TrueHD/Atmos Audio")
   - Idiomas: ISO 639-2 → nombres en inglés via `ISO639_TO_ENGLISH`
   - TrueHD + AC-3 core: se filtra el core subordinado via `multiplexed_tracks`
   - Subtítulos forzados: detección por estructura de bloques Blu-ray
   - Capítulos: mini MKV + `mkvextract chapters --simple`
   - Duración: `playlist_duration / 1_000_000_000` (nanosegundos → segundos)

2. **MediaInfo** (opcional, no bloquea si falla) — metadata extendida sobre el m2ts principal
   - `find_main_m2ts()`: el fichero más grande en `BDMV/STREAM/`
   - `mediainfo --Output=JSON` → parseo del JSON
   - **Audio**: bitrate real por pista, `Format_Commercial_IfAny` (detección definitiva Atmos/DTS:X), channel layout, compression mode (Lossless/Lossy)
   - **Vídeo**: bitrate real, HDR10 metadata (MaxCLL, MaxFALL, mastering display luminance), color primaries (BT.2020), transfer characteristics (PQ), bit depth
   - **Subtítulos**: resolución (ej: 1920x1080)
   - Los cores AC-3 embebidos en TrueHD se filtran por PID compartido

3. **dovi_tool** (opcional, solo si hay EL, no bloquea si falla) — análisis RPU Dolby Vision
   - ffmpeg extrae 30s del EL: `-map 0:v:1 -c:v copy -bsf:v hevc_mp4toannexb -t 30`
   - `dovi_tool extract-rpu` → RPU.bin
   - `dovi_tool info --summary` → parseo regex
   - **Resultado**: Profile (4/5/7/8), FEL/MEL (definitivo), CM version (v2.9/v4.0), L1/L2/L5/L6 metadata
   - Ficheros temporales limpiados en `finally`

4. **Enriquecimiento** — los datos de MediaInfo y dovi_tool se inyectan en BDInfoResult
   - `enrich_tracks_with_mediainfo()`: bitrate, format_commercial, HDR, channel_layout en cada pista
   - `enrich_dovi()`: actualiza `has_fel` y `fel_reason` con dato definitivo de dovi_tool
   - Orquestado por `run_full_analysis()` que ejecuta todo secuencialmente

- **Datos raw**: mkvmerge -J raw + MediaInfo raw guardados en `bdinfo_result` para diagnóstico
- **Atmos**: detección definitiva via `format_commercial` de MediaInfo; fallback heurístico por canales ≥8 si MediaInfo no disponible

### Reglas de audio (spec §5.1)
- Solo se incluyen pistas en Castellano y en VO
- **Detección de VO**: siempre English primero; fallback Spanish; emergencia con `vo_warning`
- Por idioma: solo la pista de mayor calidad según: TrueHD Atmos > DD+ Atmos > DTS-HD MA > DTS > DD
- Castellano: default=yes; VO: default=no
- **Tag DCP**: sufijo `(DCP 9.1.6)` solo en TrueHD Atmos **Castellano**
- Los idiomas se muestran en español via `LANGUAGE_MAP`

### Reglas de subtítulos (spec §5.2)
- Descartar código de idioma `qad` (Audio Description)
- Forzados: detección por estructura de bloques (ver Fase A)
- **Orden de inclusión**:
  1. Forzados Castellano (default=True, forced=True)
  2. Completos VO
  3. Completos Castellano
  4. Forzados VO
  5. Completos Inglés (si VO ≠ Inglés)
- Labels: `{Idioma} Forzados (PGS)`, `{Idioma} Completos (PGS)`

### Capítulos (spec §5.3)
- **Capítulos reales** extraídos del MPLS en Fase A (no en Fase D). Disponibles desde la creación del proyecto.
- Si no hay capítulos → generación automática cada 10 min desde el primer intervalo
- **Botón "🏷️ Nombres genéricos"**: visible si algún capítulo tiene nombre custom. Reemplaza todos por "Capítulo XX".
- **Botón "🔄 Restaurar del disco"**: visible tras ediciones manuales. Re-monta ISO y extrae capítulos frescos (con confirmación).
- **`name_custom: bool`**: `false` = auto-nombrado, `true` = editado manualmente

### Acceso al ISO — Loop mount directo (UDF 2.50)
- `mount -t udf -o ro,loop {iso} /mnt/bd/{nombre}_{pid}/` → mkvmerge lee desde el mount point → `umount` en `finally`
- Requiere `privileged: true` en Docker
- **MOUNT_BASE**: `/mnt/bd` (creado en entrypoint.sh)
- `unmount_iso()`: umount normal, fallback lazy, limpia directorio

### Pipeline optimizado (v1.4+) — 1 sola copia de datos
- **Ruta directa** (con reordenación/exclusión): mkvmerge lee MPLS → MKV final en `/mnt/output`. Mapeo de pistas por idioma+codec (no posicional).
- **Ruta intermedio** (sin reordenación): Phase D → MKV intermedio → mkvpropedit in-place → `mv` al output.
- **`--gui-mode`**: fuerza output de progreso en mkvmerge. Traducido de `#GUI#progress XX%` a `Progress: XX%`.
- **Cancelación**: `_cancel_flags` + `_active_processes` permiten matar el subprocess activo. Limpieza de temporales y desmontaje en finally.
- **Validación final**: tras crear el MKV, `mkvmerge -J` + `mkvextract` verifican pistas, idiomas, flags y capítulos contra lo esperado.

### Mapeo de pistas (Phase E)
- **Por contenido, no por posición**: `_match_tracks_to_source()` busca cada pista incluida en el source por coincidencia de idioma + codec.
- Audio: compara `raw.language` (inglés) con ISO 639-2 del source + subcadenas de codec.
- Subtítulos: solo por idioma (no tienen codec en `RawSubtitleTrack`). Consume IDs en orden (used_ids).
- Tabla de matching: `_ISO639` (ISO → nombre inglés), `_codec_matches()` (BDInfo → mkvmerge).

### Persistencia
- Cada sesión es un fichero JSON en `/config/{session_id}.json`
- ID de sesión: `{titulo}_{año}_{timestamp_unix}`
- **`iso_fingerprint`**: SHA-256 del primer 1 MB + tamaño. Permite detectar duplicados por contenido.
- Estados: `pending` → `queued` → `running` → `done` | `error`
- **No se persiste si Fase A falla**: la sesión solo se crea tras éxito de A+B.
- **Timestamps UTC-aware**: `datetime.now(timezone.utc)` siempre.
- **Cola**: `queue_state.json`. Al arrancar, sesiones zombie → `pending`.

### Docker
- **Base**: `ubuntu:22.04` + MKVToolNix (v81+ oficial) + mediainfo + ffmpeg + dovi_tool 2.1.2 + Python 3.10
- **`privileged: true`**: para loop mount
- **Puerto**: 8090 → 8080 (configurable)
- **Healthcheck**: `GET /api/health` cada 30s
- **Recuperación**: sesiones zombie → `pending` al arrancar

### Desarrollo local
- Python 3.12 en `.venv/`
- `./run_local.sh` — carga `.env.local`, crea directorios, lanza uvicorn con `--reload`
- `DEV_MODE=1` activa fixtures fake

---

## Reglas de UX / Diseño visual

### Estilo general — macOS moderno
- Paleta oscura, acentos azul/teal, radios generosos, transiciones fluidas
- Variables CSS centralizadas en `:root`

### Indicadores de ejecución
- **Spinner inline** (`.spinner-inline`): aparece en tab "Crear MKV", subtab del proyecto, sidebar del proyecto y sección "En curso" cuando hay job activo
- **Barra de progreso real**: en la fase de extracción (mkvmerge), conectada a `Progress: XX%`
- **Botón cancelar**: dentro de la fase activa del pipeline, se muestra/oculta automáticamente

### Posición original de pistas
- Badge `#N` (`.track-orig-pos`) en cada pista incluida y descartada, mostrando la posición original en el ISO
- Tooltip: "Posición original de la pista en el ISO"

### Consola en vivo
- Fondo negro, fuente monospace, coloreado semántico
- Sin botones decorativos falsos (semáforos macOS eliminados)

### No hacer
- No usar `alert()`, `confirm()` o `prompt()` del navegador
- No mostrar IDs técnicos internos al usuario
- No copiar el ISO bajo ninguna circunstancia
- No hardcodear rutas de directorio en el backend
- No añadir abstracción para un solo uso
