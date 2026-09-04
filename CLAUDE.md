# HDO Blu-ray Toolkit — Reglas del proyecto

## Nombre de la aplicación
La aplicación se llama **HDO Blu-ray Toolkit**. Este nombre debe usarse en:
- Título del documento HTML
- Texto de bienvenida en la UI
- README y documentación
- Mensajes de la consola del pipeline

Nombres técnicos que **se mantienen por compatibilidad** (no romper docker pulls / URLs existentes):
- Repositorio interno: `ISO2MKVFEL` (path local)
- GitHub repo: `hdo-iso-converter`
- Imagen Docker: `ghcr.io/aldicarus/hdo-iso-converter:latest`

El cambio "HDO ISO Converter → HDO Blu-ray Toolkit" se hizo cuando la app dejó de ser solo Tab 1 (ripeo de ISOs) y maduró como suite con 3 herramientas — el nombre antiguo daba a entender que solo hace una cosa, lo cual era engañoso.

## Descripción
Aplicación web multi-herramienta en contenedor Docker (amd64/QNAP) para procesar contenido UHD Blu-ray. Organizada en tres herramientas accesibles desde tabs:

| Pos. visual | Label UI | Panel interno | Propósito |
|---|----------|---|-----------|
| 1 | 💿 **Blu-Ray ISO → MKV** | `tab-panel-1` | ISO UHD Blu-ray → MKV con selección automática de pistas y soporte Dolby Vision FEL |
| 2 | ✨ **Upgrade Dolby Vision CMv4.0** | `tab-panel-3` | Inyecta RPU CMv4.0 en un MKV con CMv2.9 del Blu-ray original (sync visual frame-a-frame, multi-proyecto) |
| 3 | ✏️ **Consultar / Editar MKV** | `tab-panel-2` | Inspección profunda de la metadata del MKV (codecs comerciales, bitrate, HDR10 MaxCLL/MaxFALL, Dolby Vision profile + CM version + niveles L1-L11, procedencia CMv4.0, paquetes PGS, etc.) y edición in-place de nombres de pistas, flags default/forced y capítulos sin re-encoding |

**Nota importante para desarrollo UI**: los IDs internos de panel y las llamadas `switchTab(N)` **no** coinciden con el orden visual — `switchTab(1)` abre el panel ISO→MKV, `switchTab(2)` el de Editar, `switchTab(3)` el de CMv4.0. La reorden visual se hace solo cambiando el orden de los `<button class="tab">` en `index.html`. Cualquier código que active un tab debe usar el ID (`tab-btn-N`) y **nunca** la posición DOM.

---

## Stack técnico
- **Backend:** Python 3.10+ (ubuntu:22.04), FastAPI, uvicorn
- **Frontend:** Vanilla JS ES6+, Sortable.js (CDN), sin framework ni build step
- **Contenedor:** Docker sobre QNAP NAS x86 (amd64), puerto 8090 (configurable)
- **Herramientas:**
  - `mkvmerge` — análisis de MPLS + extracción a MKV
  - `mkvpropedit` — edición in-place de metadatos (Tab 2)
  - `mkvextract` — extracción de capítulos
  - `mediainfo` — metadata extendida: bitrate real, HDR10, codecs comerciales
  - `ffmpeg` — extracción de Enhancement Layer para análisis DV
  - `dovi_tool` — análisis RPU Dolby Vision: Profile, FEL/MEL, CM version
- **Acceso al ISO:** Loop mount directo (`mount -t udf -o ro,loop`) — requiere `privileged: true` en Docker
- **Integraciones externas:**
  - **TMDb API** — traducción ES→EN de títulos + ficha extendida (poster, sinopsis, géneros, rating) en la cabecera de proyectos de los 3 tabs. Opcional (key en ⚙︎ Configuración)
  - **Google Drive API v3** — listado + descarga de RPUs del repositorio público **DoviTools** (compartido por R3S3T_9999). Opcional (key en ⚙︎ Configuración)
  - **Google Sheets (XLSX export)** — lectura live de la hoja de recomendaciones de DoviTools con extracción de hyperlinks vía openpyxl. Sin auth (endpoint público)
## Estructura del proyecto
```
ISO2MKVFEL/
├── .github/workflows/
│   └── publish-docker.yml      ← Publica imagen a ghcr.io en cada push a main
├── CLAUDE.md
├── archive/                    ← Specs históricas (no spec activa)
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example
│   └── entrypoint.sh
└── app/
    ├── main.py              ← FastAPI app + endpoints transversales + arranque
    ├── paths.py             ← Los directorios de la app, en un solo sitio
    ├── workload.py          ← Control de admisión: un trabajo pesado a la vez
    ├── analysis_progress.py ← El paso del análisis, compartido por Tab 1 y Tab 2
    ├── routers/
    │   ├── tab1.py          ← Tab 1: endpoints + orquestador del pipeline + WS
    │   ├── tab2.py          ← Tab 2: /api/mkv/* + browser + calidad + apply
    │   └── cmv40.py         ← Tab 3: los 37 endpoints + WS del log + auto-pipeline
    ├── requirements.txt
    ├── models.py            ← Pydantic models (Session, BDInfoResult, MkvAnalysisResult, CMv40Session...)
    ├── storage.py           ← Persistencia JSON en /config + fingerprint ISO
    ├── queue_manager.py     ← Cola FIFO asyncio para Fases D+E
    ├── phases/
    │   ├── iso_mount.py       ← Loop mount/umount de ISOs UDF 2.50
    │   ├── phase_a.py         ← Análisis: mkvmerge -J + MediaInfo + dovi_tool + capítulos
    │   ├── phase_b.py         ← Motor de reglas automáticas (audio, subs, capítulos, nombre)
    │   ├── phase_d.py         ← Extracción: mkvmerge desde MPLS montado
    │   ├── phase_e.py         ← Escritura final: flags, metadatos, validación
    │   ├── mkv_analyze.py     ← Tab 2: análisis + edición de MKVs (mkvmerge + MediaInfo + light-profile DV L1)
    │   ├── rpu_analyze.py     ← Clasificación L8 (classify_l8 + tiers) para el modelo Mantener/Inyectar (pre-flight)
    │   ├── cmv40_strategy.py  ← Tab 3: la matriz (workflow × target_type × trust) como datos
    │   └── cmv40_pipeline.py  ← Tab 3: pipeline CMv4.0 (ffmpeg + dovi_tool + sync + pre-flight)
    ├── dev_fixtures.py      ← Fixtures (DEV_MODE=1): ISOs/MKVs/RPUs fake para devel UI sin discos reales
    ├── services/            ← Integraciones externas
    │   ├── settings_store.py    ← Persistencia de API keys en /config/app_settings.json
    │   ├── tmdb.py              ← TMDb search + details (poster, sinopsis, géneros, rating)
    │   ├── rec999_sheet.py      ← Sheet DoviTools: XLSX (con hyperlinks) > HTML > CSV
    │   ├── rec999_drive.py      ← Google Drive API v3 (listado + descarga de RPUs)
    │   ├── rec999_drive_match.py ← Fuzzy match de título con candidatos en el repo
    │   └── cmv40_recommend.py   ← Orquesta filename → TMDb → match contra sheet
    ├── static/
    │   ├── index.html       ← SPA completa (Tab 1 + Tab 2 + Tab 3 + modales + config)
    │   ├── core.js          ← Estado global, arranque, tooltips, tabs, toasts, modales
    │   ├── settings.js      ← ⚙︎ Configuración, versión/actualizaciones, mantenimiento
    │   ├── cmv40_modals.js  ← Consulta rápida, manual CMv4.0, limpieza masiva
    │   ├── tab1.js          ← Tab 1: nuevo proyecto, series, sesión, cola, consola
    │   ├── tab2.js          ← Tab 2: editar MKV + radiografía DV+HDR
    │   ├── tab3.js          ← Tab 3: proyectos CMv4.0, fases, sync, overlay
    │   ├── browser.js       ← El file browser modal (lo usan Tab 2 y Tab 3)
    │   └── style.css
    ├── tests/               ← unittest (test_series_*, test_tmdb_tv_match, test_rpu_analyze, test_mkv_*, test_source_abstraction, test_track_mapping, test_subtitle_classification, test_pgs_sampling, test_playlist_fallback, test_mpls_chapters, test_movie_naming, test_cmv40_*)
    │   ├── cmv40_harness.py  ← binarios falsos para ejecutar las fases CMv4.0 en un test (no es un test)
    │   └── api_harness.py    ← TestClient con /config aislado + espía de lanzamiento de fases (no es un test)
    └── tools/
        └── audit_cmv40_bins.py  ← CLI standalone: re-clasifica sesiones CMv4.0 históricas (ver "Auditoría retroactiva")
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
- El modal exige **dos elecciones en orden** (v2.7+):
  1. **Tipo de contenido** (Película / Serie). Toggle obligatorio — la zona de selección de origen está bloqueada (opacity 0.35 + pointer-events:none + banner "👆 Selecciona arriba…") hasta que el usuario elige. Sin auto-detect (que fallaba en discos atípicos con 1 m2ts grande + extras).
  2. **Tipo de origen** (3 tabs):
     - **💿 ISO** — `mount -t udf -o ro,loop` automático
     - **📁 Carpeta BDMV** — disco extraído (con BDMV/PLAYLIST/ dentro), sin mount
     - **🎞️ Ficheros M2TS** — selección de fichero(s) sueltos. En modo Película el browser usa radios (1 fichero); en modo Serie usa checkboxes (N ficheros).
- El tipo de contenido elegido se envía como `media_type_hint` al backend en `/api/disc-probe` — el backend respeta la elección y omite el auto-detect.
- Todos los orígenes viven en `/mnt/isos`. El endpoint `GET /api/sources` (depth máx 3, cache 60s) escanea recursivamente y devuelve cada entrada clasificada por tipo.
- **File browser embebido** por tab con sort configurable (nombre A→Z / Z→A · tamaño ↓/↑). Default `size_desc` en M2TS (los episodios son los m2ts grandes; los pequeños son extras).
- **Detección de duplicados** (v2.7+): `POST /api/check-duplicate` calcula la huella SHA-256 (primer 1 MB + tamaño) del fichero principal y devuelve la lista completa de sesiones que comparten fingerprint (`sessions[]`), no solo la primera. Para BDMV/ISO de serie con N episodios ya procesados eso son N. Comportamiento:
  - **Modo Película + duplicado**: diálogo "Ya existe un proyecto" con Abrir/Reanalizar (1 sesión esperada por fingerprint).
  - **Modo Serie + ≥1 sesión previa**: NO diálogo. Se pasa directo al series-modal con la lista de existentes; cada candidato MPLS/m2ts con sesión previa muestra badge `✓ Existe` en la columna del episodio y arranca DESMARCADO. El usuario marca solo lo que quiera añadir o rehacer.
- **Modal "Detectando contenido"**: tras click en Analizar el frontend abre un modal que polla `/api/disc-probe/progress` cada 400ms y muestra el paso real (montaje del ISO → escaneo de candidatos N/M → clasificación) con barra real en lugar de "Conectando con el servidor…" estático.
- **Modal de análisis** (`analyze-modal`): durante Fase A muestra cartela TMDb + título cuando hay match (lookup `/api/cmv40/tmdb-lookup` lanzado en paralelo, best-effort). Sin match queda el icono 💿/📁/🎞️ según source_type.
- **Warning de modo confundido**: en modo Película si el BDMV/ISO tiene ≥3 m2ts grandes (>5GB cada uno), `disc-probe` devuelve `movie_warning` y el frontend abre `showConfirm` antes del análisis: *"Este origen tiene N ficheros M2TS de más de 5 GB. Parece un disco de serie con varios episodios. Si confirmas modo película se usará el MPLS principal."*
- **No se persiste nada hasta éxito**: si Fase A falla, no se crea fichero JSON. Sin sesiones huérfanas.

### Paso 2 — Configurar y ejecutar
- El análisis (Fases A+B) se ejecuta al crear el proyecto y produce configuración inicial.
- El usuario revisa y ajusta en el panel de proyecto (sub-tab).
- **Tarjetas informativas del disco (v2.8+, solo lectura)**: bajo el nombre del MKV hay dos tarjetas que describen lo que el análisis encontró — **Dolby Vision** (FEL / MEL / Perfil N / dual-layer sin confirmar / sin DV, con perfil, CM version, niveles y escenas) y **Vídeo · HDR** (codec, resolución, bitrate real, HDR10 MaxCLL/MaxFALL, primaries/transfer/bit depth, master display). Un chip muestra el tag que se añadirá al nombre (`[DV FEL]`), y otro bajo el input avisa de dónde sale `[Audio DCP]` (del nombre del ISO).
  - **Sustituyen a los antiguos toggles FEL y Audio DCP**, que confundían a los usuarios: no cambiaban el contenido del MKV, solo los tags del nombre — se podía renombrar un disco MEL como FEL. `has_fel` y `audio_dcp` ya no se aceptan en `PUT /api/sessions/{id}`; son resultado del análisis (dovi_tool / nombre del ISO). El escape ante una detección errónea es editar el nombre del MKV, que sigue siendo editable (`mkv_name_manual`).
  - **Estado "dual-layer sin confirmar"**: `_detect_fel` solo sabe si HAY Enhancement Layer y asume FEL; quien distingue FEL de MEL es `dovi_tool` en `enrich_dovi`, paso opcional que puede fallar. Cuando no hay `dovi` en la pista BL, la tarjeta lo dice en vez de afirmar FEL.
  - La construcción del nombre del MKV vive **solo en el backend** — con los toggles desapareció `recalcMkvNameLocal` (la réplica de las reglas de naming en `app.js`).
- **Chip "💾 Ocupará ~N GB"** bajo el nombre del MKV. No es una predicción: el pipeline **copia** los flujos sin recodificar, así que es contabilidad — vídeo (por diferencia) más los `StreamSize` de las pistas de audio que sobreviven, por el factor del contenedor. `estimate_output_size_bytes` (en `phase_b`) lo calcula y `GET /api/sessions/{id}` lo sirve como `estimated_size_bytes`, **campo calculado y NO persistido**: cambia en cuanto el usuario toca la selección de pistas.
  - `FACTOR_CONTENEDOR = 0.89` no sale de la teoría sino de contrastar **18 rips reales del NAS** contra su m2ts: error mediano −1,9 % y los 16 casos válidos dentro de ±10 %. Con 0.907 la mediana mejora a −0,1 % pero un caso se va al +11,9 %, así que se prefiere el conservador.
  - **Devuelve `None` —y el chip se oculta— en cuanto el dato no es fiable**, en vez de enseñar un cero o un "?". El guard que importa es `TOPE_MBPS_FISICO = 128`: si el tamaño del m2ts entre la duración del playlist da un bitrate por encima del pico físico de un BD100 UHD, MediaInfo midió **otro fichero** (pasa cuando `_resolve_m2ts_from_mpls` cae al fallback de "el m2ts más grande" en un disco de serie). Caso real: dos episodios de Juego de Tronos con 146 y 128 Mbps implícitos, cuya estimación salía al doble. Una cifra inventada con pinta de dato es peor que un hueco. Cubierto por `test_estimacion_tamano.py`.
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

### Modo serie multi-episodio (v2.5+)

ISOs/BDMV/M2TS de series TV con varios episodios se procesan como **una sesión por episodio** — reutiliza toda la infraestructura (cola FIFO, validación, persistencia). Tipo elegido explícitamente por el usuario en el modal (sin auto-detect).

**Detección de candidatos** ([phase_a.py](app/phases/phase_a.py)):
- `identify_episode_candidates(share_path)` (iso/bdmv): lista MPLS con duración 15-90 min y ≥1 audio (top 20 por tamaño). Aplica dos filtros adicionales:
  1. **Dedupe por m2ts referenciado** — varios MPLS pueden apuntar al mismo m2ts (Director's Cut, Theatrical Cut, seamless branching). Mantiene solo la de duración mayor para evitar contar variantes como episodios independientes.
  2. **Tamaño relativo** — `m2ts_size ≥ max(40% mediana, 25% máximo)`. La parte de mediana cubre featurettes con still time; la del máximo cubre la distribución bimodal (1 m2ts grande + N pequeños) donde la mediana se acerca al pequeño.
- `identify_episode_candidates_from_m2ts_list(paths)` (m2ts multi-fichero): cada m2ts del set es un candidato. Sin filtro de duración estricto (el usuario los eligió). Cascada de duración: mkvmerge → MediaInfo → ffprobe con sanity check de bitrate (descarta valores inverosímiles tipo "50GB en 168s").
- `detect_disc_type(candidates)` (legacy): solo se usa cuando el frontend NO envía `media_type_hint` (compat). Con el toggle Película/Serie del modal, este auto-detect ya no aplica.

**Flujo end-to-end:**

```
Modal "Nuevo proyecto" (con tipo elegido) → analyzeSelectedISO()
  → POST /api/check-duplicate         (devuelve sessions[] con todas las
                                       sesiones que comparten fingerprint)
  → si serie + ≥1 sesión previa: bypass del diálogo de duplicado,
    pasa directo al siguiente paso con la lista de existentes
  → POST /api/disc-probe              (con media_type_hint='movie'|'series';
                                       polling /api/disc-probe/progress)
  └─ Según hint:
      ├─ movie → flujo normal (Fase A+B completa)
      └─ series → abre #series-modal con las sesiones previas
          ├─ GET /api/tv-search?query=...   (TMDb /search/tv)
          ├─ GET /api/tv-details/{id}        (TMDb /tv/{id})
          ├─ GET /api/tv-season/{id}/{N}?mpls_durations=...
          │     (TMDb /tv/{id}/season/{N} + smart-match runtime)
          └─ POST /api/create-series-sessions  (mode='add_only')
                Monta origen una vez, analiza cada MPLS con
                run_full_analysis_for_mpls/for_m2ts, aplica reglas
                Fase B, crea N sesiones pending.
```

**Endpoints REST nuevos** (todos en [main.py](app/main.py)):

| Endpoint | Función |
|---|---|
| `POST /api/disc-probe` | Detecta candidatos a episodio. Respeta `media_type_hint`; devuelve `movie_warning` si Película+BDMV con ≥3 m2ts grandes. |
| `GET /api/disc-probe/progress` | Polling del paso actual + % del scan (modal "Detectando contenido"). |
| `GET /api/tv-search?query=X&year=Y` | Top 5 candidatos TMDb (first_air_date_year filter) |
| `GET /api/tv-details/{tmdb_id}` | Detalles + seasons[] |
| `GET /api/tv-season/{tmdb_id}/{N}?mpls_durations=42,41.5,...` | episodes[] + mpls_matches[] con confianza |
| `POST /api/create-series-sessions` | Crea N sesiones de un golpe. Param `mode`: `add_only` (default, 409 si conflicto) · `replace` (borra existentes) · `skip_existing` (omite conflictos). |

**Smart-match por offset** (`match_episodes_to_mpls` en [services/tmdb.py](app/services/tmdb.py)):
- Asume MPLS consecutivos pero NO necesariamente empezando en E01. Caso típico: disco 2 de una temporada con episodios E04-E06.
- Prueba todos los offsets de inicio (E01, E02, …) y se queda con el que MINIMIZA la suma de `|delta runtime|` entre MPLS consecutivos y episodios consecutivos.
- Confianza por fila:
  - `high` si `|runtime_mpls - runtime_tmdb| ≤ 1 min`
  - `low` si fuera de tolerancia o TMDb sin runtime para ese episodio
  - `unknown` si MPLS sin episode asignado (más MPLS que episodios)
- El frontend **recalcula la confianza on-the-fly** en cada render (`_computeMatchConfidence` en [tab1.js](app/static/tab1.js)) basándose en el episodio actualmente asignado, no en la sugerencia inicial del backend. Sin esto, al corregir el match manualmente el indicador 🟢/🟡 se quedaba en el valor inicial.

**Episodios ya procesados** (v2.7+, frontend [tab1.js](app/static/tab1.js)):

Al reabrir un BDMV/ISO de serie con N episodios procesados:
- `check-duplicate` devuelve `sessions[]` (todas las que comparten fingerprint).
- El frontend filtra las que tienen `season_number + episode_number` (signal robusto de "es serie"; no se fía del `media_type` por si está como `"movie"` por default de Pydantic en sesiones muy antiguas).
- Las pasa al series-modal en `probe.existing_series_sessions`.
- `openSeriesModal` construye DOS índices para lookup O(1):
  - `existingByMpls` (**primario**) — key = basename del `mpls_path` persistido. Identifica físicamente el fichero del disco. Robusto frente a cambios de numeración entre runs.
  - `existingByEp` (secundario) — key = `"season.episode"`. Fallback si la sesión es muy antigua sin `mpls_path`.
- Helper `_findExistingForCandidate(c, season, epNum)` consulta primero por mpls, fallback por season+ep. Lo usan: cálculo de `include` por defecto, badge `✓ Existe`, detección de conflictos.
- Cada candidato con sesión previa: badge **`✓ Existe`** en la columna del episodio (no en la columna MPLS — esa es 130px con overflow:hidden y recortaba el badge) + fila con fondo ámbar sutil + checkbox **desmarcado por defecto**.
- Si el usuario marca algún existente al crear, `_seriesConfirmConflicts` abre un diálogo con 3 opciones:
  - **🗑️ Reemplazar** (envía `mode='replace'` → backend borra las sesiones existentes con `delete_session` antes de crear las nuevas)
  - **⏭ Saltar existentes** (envía `mode='skip_existing'` → backend omite conflictos, crea solo los nuevos)
  - **Cancelar**

**Análisis por episodio** (`run_full_analysis_for_mpls` / `run_full_analysis_for_m2ts` en [phase_a.py](app/phases/phase_a.py)):
- Variantes de `run_full_analysis` que operan sobre un MPLS/m2ts específico (no buscan el principal).
- Helper `_resolve_m2ts_from_mpls`: deriva el m2ts del MPLS específico con 3 estrategias en cascada:
  1. `container.properties.playlist_file` del JSON de mkvmerge -J
  2. Convención `00800.mpls` → `00800.m2ts` (~95% acierto)
  3. Fallback `find_main_m2ts` (último recurso)
- MediaInfo, dovi_tool y PGS counting operan sobre el m2ts del episodio, no del disco entero.

**Nomenclatura Plex/Jellyfin** (`build_series_mkv_name` en [phase_b.py](app/phases/phase_b.py)):

```
Serie Name (Año)/Season NN/Serie Name (Año) - SNNeNN - Episode Title [DV FEL][Audio DCP].mkv
```

- Sanitiza caracteres prohibidos en NTFS/SMB (`/\\:*?"<>|` → `-`).
- Preserva acentos UTF-8 (ext4/ZFS-safe).
- Subdirectorios creados con `Path.mkdir(parents=True)` en Fase E antes de mkvmerge.

**Campos de sesión específicos de serie** (en [models.py](app/models.py) `Session`):

```python
media_type: "movie" | "series"  # default "movie"
series_tmdb_id, series_name, series_year
season_number, episode_number, episode_title, episode_runtime_minutes
mpls_path  # solo el nombre, el mount point cambia entre montajes
```

Sesiones legacy (anteriores a v2.5) cargan sin problema con `media_type="movie"` por default.

---

## Tab 2 — Editar MKV

### Arquitectura
- **Sin persistencia**: estado ephemeral en el frontend (`mkvProject`). Un solo MKV abierto a la vez.
- **Backend stateless**: 3 endpoints bajo `/api/mkv/`.
- **Edición in-place**: solo `mkvpropedit` (O(1), instantáneo). Sin remux.
- **Análisis extendido**: `mkvmerge -J` + MediaInfo (bitrate, format_commercial, HDR).
- **Sin sidebar**: Tab 2 ocupa todo el ancho. Botón "Abrir MKV" centrado.

### Flujo
1. "Abrir MKV" → file browser unificado con roots Library + Output
2. `mkvmerge -J` + `mkvextract chapters` + MediaInfo → panel de edición
3. Editar → "Aplicar cambios". Si el MKV está en Library (read-only) modal de confirmación → backend copia a /mnt/output con barra de progreso real, luego `mkvpropedit`. Si está en /mnt/output, edita in-place.
4. "Deshacer cambios" revierte al estado original del análisis
5. "Cerrar" avisa si hay cambios pendientes

### Library read-only — copia bajo demanda
- `/mnt/library` está montada `read-only` en docker-compose. Editar in-place no es posible (mkvpropedit fallaría con permission denied).
- Al pulsar "Aplicar cambios" sobre un MKV de Library, el frontend muestra un `showConfirm` advirtiendo que se copiará a /mnt/output. Tras "Copiar y aplicar" envía `copy_to_output: true` en el payload.
- Backend `POST /api/mkv/apply`:
  - Detecta library con helper `_mkv_needs_copy_to_output`. Sin `copy_to_output=true` → HTTP 409.
  - Con `copy_to_output=true`: copia src → `/mnt/output/{mismo_nombre}.mkv` en chunks de 8 MB con `asyncio.to_thread`, emitiendo progreso (`bytes_copied`, `total_bytes`, `pct`, `eta_s`) al estado global `_mkv_apply_state`.
  - Si ya existe ese nombre en /mnt/output → HTTP 409 (no sobrescribe).
  - Tras copia exitosa, ejecuta `apply_mkv_edits` sobre la copia + devuelve `new_file_path` + `copied_from_library: true`.
  - Frontend actualiza `mkvProject.filePath` al nuevo path para ediciones posteriores.
- Polling: `GET /api/mkv/apply/progress` devuelve `_mkv_apply_state`. Frontend hace polling cada 1s mientras el POST está en curso → barra de progreso real con bytes/ETA. Single-job singleton (mismo patrón que `_light_profile_state`).
- **Cancelación cooperativa**: botón "🛑 Cancelar copia" en el modal mientras `step=copying`. Llama a `POST /api/mkv/apply/cancel` que setea `_mkv_apply_cancel["requested"]=True`. El thread de copia chequea el flag al inicio de cada chunk (<1s detección), aborta limpiamente y borra el destino parcial. El endpoint principal raise `MkvApplyCancelled` → HTTP 499. Frontend muestra "Cancelado — biblioteca intacta y destino parcial borrado". El botón se oculta al pasar a `step=applying` (mkvpropedit es ms, no aporta cancelar).
- **Timeout largo**: `apiFetch` para el POST usa `MKV_APPLY_LONG_TIMEOUT_MS = 4h` cuando hay copia. Sin esto el fetch abortaba a los 30s y el modal mostraba "timeout" mientras la copia seguía en background — confusión total. Polling es la fuente de verdad del progreso visible.

### Endpoints
- `GET /api/mkv/files` — lista MKVs en `/mnt/output`
- `POST /api/mkv/analyze` — identifica pistas + capítulos + enriquece con MediaInfo
- `POST /api/mkv/quality-audit` — **análisis extendido**: combos L8/L2 + perfil de luminancia, con una sola extracción del RPU. Cachea los dos. (La URL conserva el nombre viejo; `POST /api/mkv/light-profile` ya no existe.)
- `POST /api/mkv/apply` — aplica ediciones (mkvpropedit). Soporta `copy_to_output: true` para MKVs de Library.
- `GET /api/mkv/light-profile-cached` — perfil de luminancia YA cacheado de otro MKV, para el comparador A/B. No analiza.
- `GET /api/mkv/apply/progress` — polling del progreso de la copia + edición.
- `POST /api/mkv/apply/cancel` — solicita la cancelación cooperativa de la copia. Solo efectiva durante `step=copying`.

### Editable
- Pistas audio: nombre, flag default
- Pistas subtítulos: nombre, flags default/forced
- Capítulos: timeline interactiva, añadir/eliminar, editar nombres/timestamps
- Botón "Nombres genéricos": reemplaza nombres custom por "Capítulo XX"
- Botón "📑 Generar cada 10 min": visible cuando el MKV no tiene capítulos (banner amarillo) — replica el algoritmo `generate_auto_chapters` de Tab 1

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
   - **Timeout del demux ADAPTATIVO, no fijo** (`_adaptive_timeout`): el tope fijo de 900s no escalaba con la duración de la peli. Caso real Proyecto Salvación (2h36m UHD): demux estimado ~990s (ffmpeg 761s × `RATIO_DEMUX` 1.30) > 900s → timeout garantizado → `BL.hevc` truncado (171679 de 225177 frames). El timeout ahora es `max(1800, est×3)`, proporcional al tamaño real (la estimación ya se adapta a la carga del NAS vía `ffmpeg_wall_seconds`). Mismo cambio en los `extract-rpu` de Fase A/B (floor 1200s).
   - **Reutilización de BL/EL solo si el demux se completó** (`_demux_output_reusable` + marcador `.demux_done`): antes la rama de reutilización comprobaba solo que BL/EL **existieran**, así que un demux muerto a medias (timeout/kill/reinicio) dejaba ficheros truncados que el reintento reutilizaba → MKV corrupto (lo cazaba Fase H, pero desperdiciaba todo el pipeline). El marcador se escribe SOLO tras `rc=0`; su ausencia fuerza re-demux. Los parciales se borran al fallar/timeout (`_cleanup_partial_demux`). Cubierto por `test_cmv40_pipeline.py`.

**Regla general de timeouts (todas las pipelines)**: cualquier operación cuyo coste **escala con la duración de la peli** (`dovi_tool demux` / `extract-rpu` sobre el HEVC completo, `dovi_tool export -d all`, extract de HEVC con ffmpeg, quality/light-profile de Tab 2, extract-rpu del target en pre-flight/Fase B) **NUNCA** debe llevar un timeout fijo pequeño: usa `_adaptive_timeout(estimación, floor)` (anclado a `ffmpeg_wall_seconds`, se adapta a la carga del NAS) o, si no hay ancla, un fijo generoso ≥ 1500s. Operaciones acotadas (sniff de 30s, `info --summary` sobre un RPU ya extraído, `mkvmerge -J`/`--identify` de metadata) pueden usar fijos pequeños. El guard `test_pipeline_timeouts.py` escanea el fuente y falla si reaparece un timeout fijo pequeño sobre una op pesada (previene la clase de bug del demux, que estaba a 900s cuando necesitaba ~990s).
4. **Fase D — Verificar sincronización** (UX clave): gráfico Canvas custom con dos curvas superpuestas (origen rojo, target azul). Incluye:
   - **Zoom**: presets 30s / 1min / 5min / 30min / Todo + inputs manuales
   - **Detección automática de offset** por cross-correlation
   - **Correcciones acumulativas** (cada "Aplicar" suma a las previas)
   - **Botón "Resetear al original"** que descarta todas las correcciones
   - **Panel de confianza** basado en correlación de Pearson sobre MaxCLL (insensible a diferencias absolutas, sensible a desalineación temporal)
   - **Criterio para avanzar**: `Δ frames = 0` Y `confianza ≥ 85%`
5. **Fase E — Aplicar corrección** (parte de D): `dovi_tool editor -j editor_config.json` con `remove`/`duplicate`. NO avanza de fase — el usuario sigue iterando hasta pulsar "Confirmar sync".
6. **Fase F — Inyectar RPU**: `dovi_tool inject-rpu -i EL.hevc --rpu-in RPU_final.bin`.
   - **Workflows single-layer (`p7_mel`, `p8`): el RPU se convierte a Profile 8.1 antes de inyectar** (`_ensure_profile8_rpu` → `dovi_tool editor` con `{"mode": 2}`). El merge conserva el profile del source, así que en `p7_mel` el RPU salía como Profile 7 aunque el EL se hubiera descartado: el MKV final se anunciaba como `dvhe.07 / BL+EL+RPU` — dual-layer **sin capa de mejora** — y un reproductor DV espera una EL que no existe. Caso real: "Te van a matar" (2026-08-15), donde el track name decía "P8.1 CMv4.0" y MediaInfo del fichero decía `dvhe.07`. La conversión preserva CM version, frame count y scene cuts (verificado: P7 MEL → P8, 136033 frames y 1101 escenas). Si falla, se avisa y se inyecta el RPU original — el vídeo es correcto igualmente. Cubierto por `test_cmv40_profile8.py` (el helper) y por `test_cmv40_fase_f_matriz.py::TestProfile8EnSingleLayer`, que ejecuta la Fase F y comprueba el Profile del RPU que acaba en el HEVC. La decisión de convertir la toma `plan.inject.needs_profile8` (ver la matriz en `cmv40_strategy.py`).
7. **Fase G — Remux final**: `dovi_tool mux --bl BL.hevc --el EL_injected.hevc` + `mkvmerge -o output.mkv --no-video source.mkv` (preserva audio/subs/capítulos del origen).
8. **Fase H — Validación**: dos rutas según el modo:
   - **Drop-in FEL** (`is_drop_in_fel == True`): fast path. La cadena upstream ya garantiza Profile 7 FEL CMv4.0 (pre-flight + Fase B con trust_ok + inject-rpu determinista que copia el bin íntegro). Solo verifica integridad del MKV con `mkvmerge -J` y frame count con `ffprobe`. ~segundos.
   - **Merge CMv4.0** (rama merge sobre P7/P8 source): `dovi_tool extract-rpu` COMPLETO del HEVC pre-mux (BL_injected/EL_injected/source_injected/DV_dual según workflow) + `dovi_tool info --summary` → valida frame count del RPU vs expected (±2), `cm_version == v4.0`, `el_type` correcto, `L8 presente`. Después `mkvmerge -J`. ~5-8 min en UHD. NO usa muestreo HEAD+TAIL aunque sería más rápido: el merge frame-a-frame es la operación más sensible del pipeline y un bug que cortara el RPU a la mitad pasaría desapercibido con muestreo. Si falla, el `.mkv.tmp` se preserva para inspección.
   Si OK, mueve el MKV a `/mnt/output/` (rename atómico .tmp → .mkv).

### La matriz de workflows vive en `cmv40_strategy.py`, no en las fases

Qué hace cada fase depende de `(source_workflow, target_type, trust)`. Esa combinación estaba desplegada como cascadas `if/elif` en ~30 puntos de las fases C, F, G y H, con dos consecuencias: añadir un workflow eran ~30 sitios sin lista de verificación, y **cada fase ramificaba dos veces sobre las mismas entradas** — una para emitir su `📋 Plan` y otra decenas de líneas más abajo para decidir de verdad. Nada obligaba a que coincidieran, y fue exactamente el fallo de "Te van a matar" (2026-08-15): la pista decía "P8.1 CMv4.0" y MediaInfo leía `dvhe.07`.

`phases/cmv40_strategy.py` resuelve la matriz completa en un objeto:

```python
plan = resolve_plan(session)          # WorkflowPlan
plan.drop_in                          # ¿ruta drop-in?
plan.extract.needs_demux              # Fase C
plan.inject.hevc_input / hevc_output  # Fase F: qué lee y qué escribe
plan.inject.needs_merge / needs_profile8
plan.remux.needs_dovi_mux / video_track_name   # Fase G
plan.validate.fast_path               # Fase H
plan.inject.plan_text                 # el 📋 Plan, del MISMO objeto
```

**Regla**: cualquier decisión que dependa de workflow/target_type/trust va en la tabla, no en la fase. El grep `workflow ==`/`target_type ==` dentro de un `run_phase_*` debe seguir dando **cero**. Y el texto que se le cuenta al usuario sale del mismo `plan`, así que no puede describir algo distinto de lo que la fase hace.

El módulo es puro (sin IO ni subprocess), así que `test_cmv40_strategy.py` recorre las **48 combinaciones** en 0,2 s fijando invariantes: todo stream single-layer exige RPU Profile 8, la Fase G lee lo que la Fase F escribe, el fast path de validación es exclusivo del drop-in, etc.

**Lo que deliberadamente NO decide la tabla**: los `rpu_levels` del merge (dependen del `el_type` que `dovi_tool info` lee del RPU real, no del `source_workflow` que arrastra la sesión — los elige `_merge_cmv40_into_p7`) y si un RPU ya es Profile 8 (eso lo comprueba `_ensure_profile8_rpu` con el fichero delante).

### Las fases se ejecutan en los tests: `tests/cmv40_harness.py`

Las fases C/F/G/H no se podían invocar en un test —lanzan `ffmpeg`/`dovi_tool`/`mkvmerge` sobre ficheros de decenas de GB— así que lo poco que se comprobaba de ellas se comprobaba **leyendo el fuente** (`src.find("async def run_phase_f_inject")` + `assertIn`), que pasa en verde aunque el comportamiento esté roto. La validación real ocurría en el NAS, a 40 minutos por iteración.

`FakeToolbox` instala en el `PATH` cuatro binarios falsos que el pipeline resuelve por nombre, así que **no hace falta tocar el código de producción**. No son mudos: modelan la semántica de `dovi_tool` lo justo para que las validaciones internas del pipeline tengan sentido. Cada artefacto producido lleva un sidecar `.meta.json` con las propiedades del RPU que contiene, y se propagan por la cadena igual que en el NAS (`editor + allow_cmv4_transfer` → CM v4.0 con L8; `editor {"mode": 2}` → Profile 8; `editor` con `remove`/`duplicate` → cambia el frame count, sin lo cual no se puede comprobar que las correcciones de sync se acumulen; `inject-rpu` → el HEVC hereda las del RPU; `mux` → hereda las de la EL; `extract-rpu` → el bin hereda las del HEVC). Sin esa fidelidad las verificaciones post-merge fallarían siempre.

**Cómo se provoca un fallo**: `fail(bin, sub)` para un código de salida, `fail_when_json` para distinguir por el `-j`, y dos que nacieron con los tests de Tab 1:
- **`fail_when_arg(bin, subcadena)`** — falla solo las invocaciones cuyo argv contenga esa subcadena. Modela un fallo que depende del FICHERO: mkvmerge aborta sobre el `.mpls` y funciona sobre el `.m2ts`, que es lo que hace falta para comprobar el reintento; con un `fail` permanente no se distingue el primer intento del segundo.
- **`tras_producir=True`** — el binario hace su trabajo y DESPUÉS falla, dejando la salida a medias. Sin esto no se puede comprobar que alguien la limpia.
- **`senal=...`** — el binario muere por señal, para que el `returncode` llegue **negativo** (lo que el chequeo `>= 2` antiguo no capturaba). **SIGTERM, no SIGABRT ni SIGSEGV**: en macOS esas dos disparan el informador de fallos del sistema y cada test dejaba un `.ips` en `~/Library/Logs/DiagnosticReports` más una notificación de "Python se ha cerrado inesperadamente".

**El `mkvmerge` falso entiende `-J` y `--identify`**, que en mkvmerge son equivalentes y el pipeline usa las dos (Tab 2 y Fase A van con `-J`, la Fase E de Tab 1 con `--identify`). Cuando solo entendía `-J`, una llamada con `--identify` caía a la rama del mux y devolvía líneas de progreso donde se esperaba JSON: el mapa de pistas salía **vacío** y el test del orden de pistas pasaba en verde sin ordenar nada.

**Las props viajan en la cabecera del CONTENIDO, no en la ruta.** El sidecar está indexado por ruta absoluta, así que en cuanto el pipeline **mueve** un artefacto la metadata se queda apuntando al nombre viejo y el fichero movido vuelve a los valores por defecto — sin fallar, simplemente contestando otra cosa. Lo destapó el `os.replace` del RPU corregido de la Fase E, pero afecta igual al `.mkv.tmp` → `.mkv` de la Fase H. El `dovi_tool` real lleva la metadata dentro del fichero: `produce` escribe la cabecera que ya existía para el pipe y `read_props`/`props_of` la prefieren al sidecar.

Con eso un test afirma **qué RPU se inyecta en qué HEVC** en cada rama, y el criterio de aceptación es de mutación: reintroducir el bug (quitar la llamada a `_ensure_profile8_rpu`) tiene que hacer fallar un test. Verificado también para el demux truncado de Proyecto Salvación, el adelanto del RPU en drop-in y la validación de L8.

```python
tb.define_rpu("RPU_source.bin", profile=7, el_type="MEL", cm_version="v2.9", frames=1000)
await run_phase_f_inject(session, log)
self.assertEqual(tb.one("dovi_tool", "inject-rpu").opt_name("-i"), "BL.hevc")
self.assertEqual(tb.props_of(hevc_out).profile, 8)   # ← el bug de "Te van a matar"
```

Cubierto en `test_cmv40_fase_f_matriz.py` (29 tests) y `test_cmv40_fases_cgh.py` (35). **Regla: un test de fase ejecuta la fase.** Si hace `read_text()` del pipeline para buscar una cadena, no es un test — es un recordatorio frágil.

### Fase A en una sola pasada: ffmpeg | extract-rpu por un pipe

Los dos pasos grandes de Fase A recorren el vídeo entero y se estorbaban: medido sobre John Wick 4 (243.552 frames, MKV de 88 GB), ffmpeg tarda 574s limitado por DISCO con la CPU ociosa, y `extract-rpu` 372s limitado por CPU (100% de un core) con el disco medio ocioso — ~946s en serie usando media máquina cada vez. `_ffmpeg_extract_rpu_piped` los conecta por un `os.pipe()` (ensanchado a 4 MB con `F_SETPIPE_SZ`) usando el muxer `tee` de ffmpeg para escribir `source.hevc` y alimentar el pipe a la vez. Verificado bit a bit: mismo MD5 que por el camino clásico. Ante cualquier problema devuelve `False` **sin lanzar**, para que el caller recurra al camino en dos pasos.

Consecuencia contraintuitiva pero estructural: **ffmpeg termina bastante antes que el pipeline**. Medido en 5 jobs, la "cola" en la que `extract-rpu` sigue solo es el 23-38% de la fase (71s/263s · 119s/495s · 198s/519s · 201s/733s · 197s/863s). `source.hevc` queda con su mtime final mientras `RPU_source.bin` se escribe minutos después.

**Para testear este camino, los binarios falsos tienen que entender que `pipe:1` es stdout.** El arnés lo trataba como una ruta: creaba un fichero llamado literalmente `pipe:1` en el cwd de la suite (llegó a commitearse uno), el pipe quedaba vacío, la función devolvía `False` y todos los tests de Fase A verificaban el camino en dos pasos — el que **no** corre en el NAS. Hay dos formas reales que el fake debe cubrir: el muxer `tee` (`[f=hevc]fichero|[f=hevc]pipe:1`) y `-f hevc pipe:1`. Y `dovi_tool extract-rpu -` lee de stdin: `-` no es una opción aunque empiece por guion. Las props del RPU viajan en la cabecera del stream, porque por un pipe no hay sidecar que consultar — sin eso el RPU extraído sale con los valores por defecto y el pipe *parece* funcionar mientras pierde el contenido. Cubierto por `test_pipe_fase_a.py`.

### Progreso: medirlo, no estimarlo — y saber qué NO se puede medir

Regla del proyecto tras la auditoría de progreso: la barra y el ETA salen de **evidencia** (bytes leídos/escritos, frames, `Progress:` de mkvmerge), no de `elapsed / constante`. Las constantes envejecen con cada cambio del pipeline — el pipe de Fase A y el adelanto de la validación dejaron las suyas desfasadas en horas, y un job de 26 min llegó a anunciar 49.

`_ReadProgress` (en `phases/cmv40_pipeline.py`) da el porcentaje real de un subproceso con tres señales, por orden de preferencia:

| señal | de dónde | cuándo usarla |
|---|---|---|
| `expected_read` | `rchar` de `/proc/<pid>/io` | procesos de **varias pasadas**: el contador acumula y atraviesa el cambio de pasada sin saltos |
| `expected_out` | tamaño del fichero de salida | el proceso escribe algo cuyo tamaño final se conoce |
| posición de lectura | `pos:` de `/proc/<pid>/fdinfo/<fd>` | una sola pasada sobre un fichero |

Trampas verificadas en el NAS, todas ellas causa de un bug real:
- **Un pipe no tiene posición de lectura**: `fdinfo` se queda a `pos: 0` pasen los GB que pasen. Para el consumidor de un pipe hay que usar `rchar`.
- **`rchar` no puede medir la cola del pipe de Fase A**: el consumidor tiene que haber sacado del pipe TODO lo que ffmpeg escribió para que ffmpeg pueda terminar (el buffer son 4 MB), así que el contador llega al total justo cuando ffmpeg acaba y ahí se queda. La barra tiene tope en `TOPE_LECTURA` (95%) y esa cola solo se cuenta en tiempo transcurrido.
- **Sin fichero de salida sigue habiendo progreso**: el denominador (`_total_del_stream`) sale del fichero cuando se pide con `tee`, y del `size=` que ffmpeg reporta en su stderr cuando no (`hevc_out=None`) — mide lo mismo, los bytes metidos en el pipe. Antes la única fuente era el fichero, así que sin él **no se emitía nada**; estaba comentado y era razonable mientras el único caller sin fichero era el pre-flight (un sniff de 30 s), pero el perfil de luminancia de Tab 2 recorre la peli entera: medido en el NAS, **300 s con la barra en 0 %** sobre un MKV de 72 GB. Y a ffmpeg no se le puede preguntar cuando escribe con `tee`: con dos salidas emite `Lsize=N/A`, así que ahí manda el fichero.
- **`dovi_tool extract-rpu` escribe el RPU de golpe al cerrar** (comprobado: el tamaño aparece en el mismo segundo en que la fase termina), así que el fichero de salida tampoco sirve para esa cola. Ese tramo NO es medible; no fingir un porcentaje.
- **`inject-rpu` hace dos pasadas** (`Processing input video for frame order info` → `Rewriting file with interleaved RPU NALs`). Medido: pasada 1 = 15% del tiempo, pasada 2 = 84%. Mezclar señales distintas con el tope monotónico dejaba la barra clavada al 100% durante la segunda. Se usa `expected_read = 2 × tamaño(entrada)`.
- **El tope monotónico es obligatorio** (`_anotar`): los totales extrapolados fluctúan y sin él la barra retrocede.
- **El respaldo por reloj del ETA es un `if` propio**, no va dentro del `if step_pct is None`: con porcentaje real pero sin dos muestras de avance, el ETA quedaba en `None` y desaparecía del log.
- El ETA sale de una **ventana** de muestras (~60s). Con 18s saltaba ±2 min entre líneas porque el ritmo del NAS fluctúa.
- El porcentaje solo se anuncia en el log **cuando es medido**; con la estimación por reloj sería una cifra inventada con pinta de dato.

**Para probar esto, un test de integración con binarios falsos no sirve**: el pipe termina en milisegundos y el ticker del consumidor comprueba cada segundo, así que `fin` se pone antes del primer tick y el bucle NO se ejecuta. Un test así no prueba nada y encima señala al código bueno (pasó: los dos primeros intentos fallaron en CI, incluido el del camino que ya funcionaba). La decisión se prueba en su helper puro, sin `/proc` y sin reloj.

Y el **binario falso de ffmpeg emite el formato real** —`frame=… size=…KiB time=…  bitrate=… speed=…x`, con `Lsize=` en la última—, copiado de un job del NAS. Escribía una sola línea sin `size=` ni `time=`, o sea justo los dos campos de los que sale el porcentaje: un fake mudo hace pasar en verde una barra muerta.

`GET /api/cmv40/eta-model` sustituye a las constantes del frontend: ratios reales de cada fase respecto a Fase A, **segmentados por ruta** (drop-in vs merge, que no se parecen en nada) sobre los 10 jobs más recientes de cada una, más `share_dropin` para repartir cuando la ruta aún no se sabe. La señal de "ruta aún desconocida" es `target_dv_info` vacío, **no** `target_type` (que nace a `'generic'`, así que `!target_type` es siempre falso — bug real que dejó muertos dos consumidores).

### El auto-pipeline tiene DOS disparadores

La siguiente fase la lanzan el orquestador del backend (`_cmv40_dispatch_next_phase`) **y** `_cmv40MaybeAutoAdvance` en el frontend. El frontend se protege mirando `error_message`, pero sobre el snapshot de su último poll: si ese poll cayó en el hueco entre "la fase anterior acabó" y "la siguiente arrancó", ve la fase avanzada, sin error y sin `running_phase`, y dispara igual. Comprobado que el disparo duplicado es **sistemático**, unos 10s después del cierre de fase.

Dos guards en el servidor, que es el único con el estado real:
- fase ya en `done` → se omite (ya existía).
- `_cmv40_guard_no_pending_error` → **409** en los nueve endpoints que arrancan fase si la sesión arrastra un `error_message` sin resolver. Para reintentar, `POST /clear-error` (lo llama el frontend al descartar el banner). Sin esto, Fase H se ejecutó dos veces con 1,2s de diferencia; en la rama merge son 5-8 min de `extract-rpu` repetidos.

### Abortar antes de gastar, no después

`check_disk_space_preflight` y `check_output_name_free` corren juntas al empezar Fase A: las dos responden "¿podrá terminar esto?". El nombre de salida se comprueba porque Fase H se niega a sobrescribir —y hace bien— pero se enteraba al final: un job real gastó 826s de Fase A + 851s de inject + 755s de remux para morir con "Ya existe un MKV con ese nombre". No borra ni renombra el destino: puede ser una versión que el usuario quiere conservar.

### El análisis de MKV (Tab 2) se ejecuta en tests

`analyze_mkv` no tenía ninguna prueba que la ejecutara. Ahora `test_mkv_analyze.py` la corre con el arnés (`cmv40_harness`, extendido con `mediainfo`, `mkvextract` y `mkvpropedit` falsos, `mkvmerge -J` configurable y `ffprobe -show_packets`): metadata HDR, pistas, detección de la Enhancement Layer, enriquecimiento con MediaInfo, capítulos, conteo PGS y los pasos del modal de progreso.

**El fake devuelve el formato REAL, con unidad.** MediaInfo da `MaxCLL` como `'300 cd/m2'`, y el código hacía `int(rt["MaxCLL"])`: ValueError, `except: pass`, campo a None — **siempre**, en los 20 MKVs cacheados del NAS. MaxCLL/MaxFALL nunca llegaron a la radiografía DV+HDR ni a las líneas de referencia del perfil de luminancia. Lo parsea `_nits_de_mediainfo`.

Demostrado por mutación que la fidelidad del fake es lo que hace útil el test: con la unidad, reintroducir el bug **falla**; con el número pelado, el mismo bug **pasa en verde**. Antes de escribir un binario falso, comprobar el formato real con `docker exec ... mediainfo --Output=JSON`.

**El conteo PGS muestrea y escala**: lee los primeros 1.200 s y multiplica por `duración / 1200`; por debajo de 1.800 s cuenta entero. Un test que espere el valor crudo falla — y con razón.

**Los niveles L8/L9/L11 tampoco llegaban.** `_enrich_dovi_from_json_export` recorría `vdr_dm_data.ext_metadata_blocks`, y en el volcado real de `dovi_tool export` los bloques viven un nivel más abajo —separados en `cmv29_metadata` y `cmv40_metadata`— y el nivel es la **clave** del bloque (`{"Level8": {…}}`), no un campo. Con las dos incompatibilidades la función retornaba sin tocar nada, y sin lanzar: el `except: pass` del caller no tenía qué registrar. Los chips de trim targets y el tipo de contenido L11 de la «Cadena de mastering» nunca aparecieron.

Reescrita sobre **`rpu_analyze.export_levels`** (198 líneas y CC 82 → 63 y 13). **Regla: un solo parser de ese JSON en el repo.** Tener dos permitió que este divergiera del formato real, y el que funcionaba era justo el que tenía tests.

Dos datos verificados sobre RPUs reales que condicionan el parseo:
- **`source_primary_index` es 0** (BT.709) en los RPUs reales, así que hay que distinguir *ausente* de *cero*: un `or` lo descarta por falsy. Mismo cuidado con `content_type` de L11.
- **L10 sale vacío** en los RPUs probados aunque `--levels` lo acepte, así que no se pide.


### `CMv40Session` se queda plana — y por qué

El modelo tiene **78 campos planos** con grupos evidentes por prefijo (`target_l8_*`, `pending_target_*`…). Anidarlos en sub-modelos **está descartado**, con dos motivos medidos:

- `session.model_dump()` va tal cual a la UI, y `app.js` lee **61 de los 78 campos**. Anidarlos no produce un error: produce `undefined` en 61 puntos.
- **Pydantic ignora en silencio las claves que no reconoce.** Verificado ejecutándolo: un JSON actual cargado con un modelo anidado devuelve los sub-modelos vacíos, y el primer `save_cmv40_session()` reescribe el fichero **sin** el análisis L8 ni los combos L2 — horas de `dovi_tool export` perdidas sin un mensaje. Con proyectos en el `/config` de usuarios que no podemos inspeccionar, no hay vuelta atrás: volver a la imagen anterior no recupera un fichero ya reescrito.

Ningún bug conocido viene de que el modelo sea plano.

**Lo que sí hay**, con el beneficio y sin el riesgo:

- **Vistas agrupadoras**: `session.l8`, `session.pending_target`, `session.recommendation` — properties calculadas al vuelo, con predicados (`l8.is_real`, `recommendation.says_keep`). **No entran en `model_dump()`**, así que el JSON no cambia. El código nuevo escribe `session.l8.classification`.
- **Invariantes** que **normalizan y avisan, sin lanzar**. Un validador estricto convertiría una incoherencia menor en «la app no carga tu proyecto»; con datos de usuarios, cargar gana a validar. La garantía para el código nuevo la dan los tests.

**Si alguna vez se retoma el anidado**, el camino seguro es: añadir `schema_version` a las sesiones nuevas, aceptar los dos formatos durante al menos una versión, esperar a que el parque rote, y escribir un `.bak` antes de migrar. Meses, no una sesión.

### El log del pipeline: `_log`, no `if log_callback:`

`await _log(log_callback, "…")` hace el guard dentro, así que el callback es
opcional de verdad y las fases se pueden ejecutar sin log (los tests lo
hacen). Quedan 25 `if log_callback:` a propósito: los que construyen el
texto condicionalmente o emiten varias líneas, donde sin el guard se haría
el trabajo para tirarlo.

**Los textos se quedan junto a los datos que interpolan.** Se evaluó sacarlos
a un módulo de narrativa y los números no lo sostienen: de 136 llamadas al
log, solo 33 llevan texto fijo; 95 son f-strings con 2-5 variables locales
(tamaños, frame counts, profiles del RPU). Separarlas costaría ~475 líneas de
módulo para quitar 180 del pipeline, y obligaría a saltar entre ficheros para
saber qué se emite. Los `📋 Plan` que SÍ salieron son los de las fases que
ramifican por workflow, y viven en `cmv40_strategy` porque allí la decisión y
su explicación tienen que ser el mismo objeto.

### La UI no re-deriva el plan: lo lee

`GET /api/cmv40/{id}` incluye `session.plan` con las decisiones ya resueltas por `cmv40_strategy` (`drop_in`, `trust_effective`, `target_needs_merge`, y por fase `needs_demux` / `needs_merge` / `needs_profile8` / `needs_dovi_mux` / `fast_path`…). `app.js` lo lee con tres helpers: **`_cmv40Trust`**, **`_cmv40DropIn`** y **`_cmv40TargetNeedsMerge`**.

Antes esas reglas estaban escritas a mano: el trust efectivo **once veces** y en dos variantes sintácticas distintas. Una réplica de una regla del backend se desincroniza en silencio — de esa familia era el bug del overlay.

Los helpers conservan un **fallback local** porque el plan no llega en tres situaciones: sesiones cacheadas de antes del cambio, el summary del sidebar (que vacía campos pesados) y el modal de creación (sin sesión todavía). Es UNA implementación, y `test_cmv40_plan_frontend.py` comprueba en node que coincide con la tabla de Python.

**Regla**: cualquier decisión que dependa de workflow/target_type/trust se lee del plan. `test_cmv40_plan_frontend::TestNoQuedanReplicas` falla si la expresión `force_interactive` reaparece a mano (hay 3 usos legítimos: el fallback, la predicción de drop-in previa a los gates y la regla de auto-avance, que incluye el ACK).

**Saltarse la Fase D: `skip_sync_review`, y solo ahí.** Dos vías la habilitan —gates OK, o el usuario aceptó una degradación que la Fase D no puede arreglar— y **ninguna sobrevive a `force_interactive`**. La consumen el orquestador, el endpoint de ACK y la UI (`plan.skip_sync_review`).

Antes cada parte tenía su versión: el backend hacía `trusted_auto or user_acked` (con el ACK dado se saltaba la fase aunque el usuario hubiera pedido revisión manual) y el frontend exigía además que no hubiera `force_interactive`. Manda esta última lectura: pedir revisar el sync a mano y aceptar que el grading diverge son decisiones distintas.

**Ojo con confundirla con `trust_effective`**: `mark_synced` usa esa otra a propósito, porque responde otra pregunta —si el usuario *revisó* la Fase D o solo la dio por buena— y ahí el ACK no cuenta. Confundir las dos es lo que produjo la divergencia.

**Regla**: ninguna condición de trust se escribe a mano fuera de `cmv40_strategy.py`. `is_drop_in_fel` del pipeline delega en `plan.drop_in` por lo mismo — era una segunda definición del drop-in.

### Un router por pestaña, y la dependencia en un solo sentido

`main.py` era un fichero de 6.000 líneas con las tres pestañas dentro. Hoy:

| Fichero | Qué tiene |
|---|---|
| `main.py` (1.215) | La `app`, los estáticos y **lo que es de toda la aplicación**: salud/estado/actividad, versión y chequeo de actualizaciones, ajustes, el scan + borrado de huérfanos y el arranque |
| `routers/tab1.py` (2.900) | **Tab 1** — orígenes, sesiones, análisis, series TV, la cola, el orquestador `_run_pipeline` + `_validate_final_mkv`, y los WS `/ws/{id}` y `/ws/queue` |
| `routers/tab2.py` (1.311) | **Tab 2** — `/api/mkv/*`, el file browser, la auditoría de calidad del RPU y el apply con copia desde biblioteca |
| `routers/cmv40.py` (3.829) | **Tab 3** — los 37 endpoints de `/api/cmv40/*` + el WS del log + el auto-pipeline |
| `paths.py` · `workload.py` · `analysis_progress.py` | lo que comparten |

Nótese que `main.py` **ya no tiene ninguna pestaña**: lo que queda no es "Tab 1 más cosas", es la aplicación. Un endpoint nuevo de una pestaña va a su router; uno transversal, a `main`.

**Las URLs no cambiaron con ninguno de los dos movimientos**, y eso no es una promesa: `tests/test_rutas_no_cambian.py` compara el esquema OpenAPI real contra un golden de 93 rutas (con sus parámetros y su body) más los 3 websockets. Un endpoint nuevo es legítimo, pero hay que meterlo en el golden en el mismo commit.

La dependencia es **unidireccional**: `main` incluye los routers, y ningún router importa `main` (lo fija `test_split_por_pestanas.py`, que además carga cada router en un proceso limpio para comprobar que no arrastra `main`). Se sostiene porque:
- `DEV_MODE` vive en `dev_fixtures.py` y los directorios en `paths.py`, no en `main`.
- **Cada router se recupera solo.** Lo que tiene que correr al arrancar se expone como función pública y lo llama `main`: `tab1.recuperar_sesiones_interrumpidas()` devuelve a `pending` las sesiones zombie, `tab2.recuperar_apply_interrumpido()` borra el `.mkv` a medias de una copia interrumpida, y `cmv40.recuperar_sesiones_interrumpidas()` limpia los `running_phase` fantasma. Ese sentido (`main` → router) sí está permitido; el recovery vive junto al estado que protege.
- `_dev_simulate_phase` se fue con el router de CMv4.0 y `_run_fake_pipeline` con el de Tab 1: eran helpers exclusivos suyos.

**Al añadir un endpoint de fase o un guard que escanee el fuente, mirar la lista de ficheros.** Cuatro tests recorren código fuente (`test_event_loop_limpio`, `test_pipeline_timeouts`, `test_imports_alcanzables`, `test_analisis_extendido_frontend`) y todos tenían `main.py` a secas: con el código en otro fichero habrían seguido pasando en verde vigilando el vacío. Están extendidos a los tres routers.

**Cuidado con el estado que dos pestañas comparten.** `GET /api/analyze/progress` alimenta DOS modales —"Analizando disco" de Tab 1 y "Abriendo MKV" de Tab 2— y era una global con `global _analyze_progress`. Al separar los ficheros ese `global` habría rebindeado un nombre **propio del router**: Tab 2 seguiría escribiendo su progreso, el endpoint seguiría devolviendo 200, y el modal se quedaría en blanco sin un error ni una línea de log. Vive en **`analysis_progress.py`**, que se **muta** en vez de reasignarse (`fijar(...)` / `leer()`) para que quien tenga la referencia vea los cambios. Que dos análisis simultáneos se pisen el paso mostrado es anterior al corte y sigue igual: es cosmético, y de los solapamientos de verdad se encarga `workload`.

**Al tocar tests que parcheen por módulo**: lo de CMv4.0 se parchea en `routers.cmv40` y lo de Tab 2 en `routers.tab2`. Los directorios van SIEMPRE en `paths` (`paths.OUTPUT_DIR_MKV`, nunca `from paths import ...`, para que un solo parche lo vea todo el mundo); los de CMv4.0 siguen en `storage` (`CONFIG_DIR`, `CMV40_DIR`) y `phases.cmv40_pipeline` (`OUTPUT_DIR`, `CMV40_WORK_BASE`) — ver `tests/api_harness.py`, que redirige todos.

### Los dos job singleton de Tab 2 NO se unifican — y por qué

`_mkv_quality_state` (la auditoría de calidad) y `_mkv_apply_state` (el apply con copia) tienen la misma **forma** —un dict de estado, un reset, un set_step, un flag de cancelación, un endpoint de progreso— y se evaluó sacar un `SingletonJob` común. Está descartado, con los números delante: son 432 y 235 líneas, y lo que comparten son unas **30**. Lo que NO comparten es justo lo que costó depurar:

- la calidad cancela **dirigido por `audit_id`**, porque un cancel tarda 3-13 s (kill + reap) y en ese hueco el usuario ya ha lanzado otro audit: el viejo no debe matarlo. Todo —`_mkv_quality_log`, `_state_finalize_if`, `_check_cancel`— va filtrado por ese id;
- el apply **sobrevive a un reinicio del contenedor**: persiste su estado en `/config/mkv_apply_state.json` y tiene recovery de arranque que borra el `.mkv` a medias.

Una clase común uniría el 5 % trivial, dejaría el 95 % en subclases y añadiría una indirección entre cada endpoint y su estado. El riesgo real que motivaba el punto —dos trabajos pesados a la vez, que además envenenan `ffmpeg_wall_seconds` y con él `_adaptive_timeout` y el modelo de ETA— **ya lo cubre `workload.py`**, que es control de admisión entre pestañas y no tiene nada que ver con la forma de estos dos dicts.

Los otros tres estados con pinta de job (`_disc_probe_progress`, `_series_create_progress`, `analysis_progress`) son dicts de progreso de solo lectura sin cancelación ni persistencia: meterlos en la abstracción sería inventarles maquinaria que no usan.

### Lanzar una fase: un solo sitio, y una tabla

Los nueve endpoints de fase y los cinco dispatchers del auto-pipeline eran
el mismo bloque copiado **14 veces** (el `_coro` + `_run` con su try/except
y un comentario de seis líneas). Hoy:

- `_cmv40_launch_phase(session, fase, coro_factory, nueva_fase)` — el
  `create_task` con su red de seguridad. El try/except **no es decorativo**:
  `_run_cmv40_phase` ya captura los errores DE LA FASE, así que lo que llega
  ahí es un fallo del wrapper (p. ej. el save de arranque); tragárselo mudo
  dejaba el job zombie sin una pista en el log.
- `_CMV40_RUNNERS: fase → (función del pipeline, fase destino)` +
  `_cmv40_dispatch_phase(session, fase)` — sustituyen a los cinco
  dispatchers, que solo diferían en ese par. Los endpoints de fase sin
  parámetros propios delegan aquí también.

**Regla**: una fase nueva se añade con una fila en `_CMV40_RUNNERS`, no
copiando un endpoint. El nombre de la fase y su runner viajan por separado,
así que `test_cmv40_endpoints` comprueba los dos (una fila cruzada daría un
`phase_name` correcto ejecutando otra cosa).

### Los endpoints se prueban con TestClient: `tests/api_harness.py`

`ApiTestCase` levanta la app real con `/config`, `/mnt/output` y el workdir
en un tmpdir. **No usa variables de entorno** — llegarían tarde si otro test
importó `main` antes, y solo se puede importar una vez por proceso: parchea
los directorios ya resueltos, igual que `cmv40_harness` con `OUTPUT_DIR`.

La pieza que lo hace posible es el **espía de `_run_cmv40_phase`**. Los
endpoints de fase son fire-and-forget; dejándolos correr, el `TestClient` se
cuelga al cerrar esperando la tarea (y lanza ffmpeg de verdad). Con el espía,
un test afirma qué fase se pidió arrancar sin ejecutarla, y
`mockear_runners()` — que hay que llamar **antes** de la petición, porque el
runner se resuelve al despachar — cubre qué función del pipeline se eligió.

Tres matices del contrato HTTP que no son obvios y están fijados en tests:
`cancel` **no** devuelve 404 a propósito (libera flags aunque la sesión ya no
esté: mejor eso que una UI bloqueada), `apply-sync` lanza `correct_sync`
**sin avanzar de fase** (Fase E se repite hasta que el Δ es 0), y los cuatro
endpoints con body devuelven **422** antes de llegar a los guards, porque
FastAPI valida el body primero.

### Estados de la sesión

- `phase`: última fase completada — `created → source_analyzed → target_provided → extracted → sync_verified → injected → remuxed → validated → done`
- `running_phase`: fase ejecutándose ahora mismo (bloquea la UI en modo modal overlay)
- `error_message`: error de la última acción intentada (no bloquea, se puede descartar)
- `archived`: true tras cleanup — proyecto en modo solo lectura, no se pueden rehacer fases
- `sync_config`: dict con la corrección acumulada (remove/duplicate ranges)
- `sync_delta`: diferencia de frames actual (target - source)

### Resiliencia frente a desconexiones de cliente y reinicios de servidor

La app está diseñada para que el frontend pueda cerrarse (cierre de pestaña, Mac dormido, server reinicia) sin perder estado ni log de los jobs en curso. Sistema de robustez en 5 capas:

1. **Watermark de log frontend** (`project._renderedLogCount` y `_renderedRunningLogCount`) — el log permanente de cada proyecto CMv4.0 (card "📜 Log") se hidrata desde `session.output_log` al cargar y se incrementa con cada línea WS **que el backend persiste** (las `§§PROGRESS§§` no cuentan — ver punto 6 del patrón de saves). Garantiza que tras un Mac dormido toda la noche, al reabrir el proyecto se ve TODO el log (no solo lo que llegó por WS antes del sleep). Sin esto, la card permanente quedaba vacía cuando `running_phase=null` porque el WS no se conectaba. El backend YA NO envía replay de las últimas 500 líneas del WS (era duplicación contra la hidratación REST). Los pollers piden `?include_log=false` mientras el WS está sano y `_cmv40AssignSession` restaura la copia local — así el watermark nunca ve un array recortado.
2. **Auto-resume del overlay Tab 3** — al entrar al Tab 3 con un proyecto en `running_phase != null` y sin proyectos abiertos, la app abre automáticamente el proyecto con su modal de ejecución y log en vivo. Toast informativo "🤖 Reanudando seguimiento". 1-shot por entrada al tab (`_cmv40AutoResumeAttempted`). Mismo mecanismo aplicado a Tab 2 con `_mkvCheckActiveApply` cuando hay copia desde Library en curso.
3. **Atomicidad de escritura** — `_atomic_write_json` en `storage.py` (escribe a `.tmp` + `os.replace`) usado por `save_session`, `save_cmv40_session`, `_persist_mkv_apply_state` y `queue_state`. Sobrevive a kill -9 mid-write sin dejar JSON corrupto.
4. **El log de un proyecto CMv4.0 vive en un FICHERO, no en el JSON** — `/config/cmv40/{id}.log`, al que se **añade**: escribir una línea es O(1). Antes iba dentro de `session.output_log`, así que persistir una línea reescribía la sesión entera; medido sobre el `/config` real del NAS, **el proyecto más grande son 3,89 MB con 2.068 líneas de log y 2.251 combos L8**, y con el throttle a 5 s eso son ~850 saves por job. El coste era **cuadrático en la longitud del log**: el throttle acotaba la frecuencia, no el tamaño de cada escritura.
   - Las líneas pasan por un buffer que se vuelca cada segundo o cada 100 líneas — no por ahorrar syscalls (añadir es barato) sino para no hacer una escritura por línea cuando `_run_streaming` entrega una ráfaga. Pérdida máxima ante `kill -9`: 1 s.
   - **`last_progress` va a su propio sidecar** (`{id}.progress`). Al quitar el log, ESTE se convirtió en el coste dominante: ~50 bytes reescritos con 0,86 MB cada 20 s durante todo el job.
   - **La extensión es `.progress`, no `.progress.json`**: el listado de sesiones hace `glob("*.json")` sobre ese directorio y el sidecar se colaba como si fuera una sesión. El listado tiene ya el guard de "sin `id` no es una sesión" que `list_sessions` de Tab 1 sí tenía.
   - **El contrato con el frontend no cambia**: `GET /api/cmv40/{id}` sigue devolviendo `output_log`, compuesto al servirlo como *prefijo del JSON + fichero + buffer*. Así el watermark de la UI funciona igual y una línea recién emitida nunca falta de la hidratación.
   - **Las sesiones existentes no se migran.** Un proyecto terminado no vuelve a escribir; reescribir el `/config` de un usuario para ahorrarse una concatenación no vale la pena.
   - Medido: **2,06 GB → 9 MB de escrituras en `/config` por job (219×)**. Con ello se cayó `_cmv40_maybe_persist_log` entera (marcadores que forzaban save, throttle, lock por sesión, descarte de trigger): toda esa maquinaria existía porque escribir una línea costaba megabytes.

**Tab 1 NO tiene este problema, y la medición lo corrigió.** El plan asumía que era peor porque salva cada 1 s (contra los 5 s de Tab 3), pero el throttle solo dispara cuando llega una línea y Tab 1 emite **~136 por job** (mkvmerge con `--gui-mode` da un `#GUI#progress` por punto porcentual, más las líneas del pipeline). Medido sobre el `/config` real: **40 sesiones, 3,4 MB en total, la mayor 0,15 MB** — unos 11 MB de escrituras por job. No compensa la cirugía, que ahí es más invasiva porque `execution_history[].output_log` guarda una copia del log por ejecución.
5. **Recovery startup** — al arrancar el server:
   - `_recover_interrupted_sessions` resetea Tab 1 sesiones running/queued a pending con error.
   - `_recover_interrupted_cmv40_sessions` limpia `running_phase` fantasma y marca el último `phase_history` running como error.
   - `_recover_interrupted_mkv_apply` borra el `.mkv` parcial en /mnt/output si una copia desde Library quedó interrumpida, y marca `_mkv_apply_state` como error.

Indicadores visuales:
- **Aviso al terminar un trabajo largo** (`core.js`, controles en `settings.js`). Un rip son 20-40 min y una fase CMv4.0 puede pasar de la hora; esto evita tener que volver a mirar. Dos restricciones que definen el diseño:
  - **El NAS va por HTTP, así que la Notification API NO está disponible ahí** (exige contexto seguro; en `localhost`/dev sí). El aviso base es el **título de la pestaña parpadeando**, que funciona siempre; la notificación de escritorio y el pitido (WebAudio, sin fichero que servir) son extras. `avisoNotificacionDisponible()` lo decide y el texto de ⚙︎ Configuración lo explica en vez de ofrecer un botón que no haría nada.
  - **Cero tráfico cuando no hay nada que vigilar.** El poller de los puntos verdes se salta las vueltas con `document.hidden` a propósito y eso NO se toca. En su lugar, al ocultarse la pestaña se toma una foto de qué tabs tenían trabajo: si no había ninguno **no se pollea nada**, y si lo había se pregunta cada 20 s hasta que termine — 3 peticiones/min sólo mientras estás fuera. Tab 1 sale de `queueState` (ya lo llena el WS), así que un tick son **dos** peticiones, y a `/api/cmv40-active`, que se responde desde memoria.
  - `_arrancarParpadeo` **no re-guarda el título original** si ya hay un parpadeo en curso; sin ese guard, dos avisos seguidos dejan el título alterado para siempre. Cubierto por `test_aviso_fin_trabajo.py`, que evalúa las funciones reales de `core.js` en node sobre un DOM mínimo.
- **Punto verde animado** en cada tab principal (`tab-running-dot-{1,2,3}`) cuando ese tab tiene jobs activos. Polling cada 5s (`/api/mkv/apply/progress`, `/api/cmv40-active`, `queueState` en memoria). Visible aunque el usuario esté en otro tab, pero **el poller se salta las vueltas con `document.hidden`** — eran dos peticiones cada 5 s durante horas contra un NAS que está procesando vídeo; al volver, `visibilitychange` dispara `_runRecoveryTasks`, que los refresca.
  - **`/api/cmv40-active` se responde desde memoria** (`_cmv40_activas`), sin tocar el disco. Empezó pidiendo `GET /api/cmv40` (569 KB y 193 ms con 88 proyectos), pasó al summary cacheado —que aún hace un `glob` más un `stat` por sesión: ~88 syscalls cada 5 s, 1,5 millones al día— y ahora cuesta cero. Se puede porque **este proceso es el único que arranca fases** y `_recover_interrupted_cmv40_sessions` limpia al arrancar los `running_phase` que quedaran en disco: la memoria es la fuente de verdad, y un fantasma del JSON tras un `kill -9` no enciende el punto.
  - **Regla**: `session.running_phase` solo se toca desde **`_cmv40_marcar_activa` / `_cmv40_marcar_libre`**. Una asignación cruda deja el registro con un fantasma y el punto no se apaga nunca; `test_cmv40_activas_en_memoria::TestNadieAsignaRunningPhaseAMano` lo guarda recorriendo el AST (no el texto: los docstrings del módulo mencionan `running_phase="preflight"` describiendo el flujo).
- **Spinner animado en cards del sidebar CMv4.0** cuando `running_phase != null` — sustituye al icono estático de fase. Card con `border-left` verde + halo pulsante.

### Control de admisión: un solo trabajo pesado a la vez — `workload.py`

Cada pestaña serializaba lo suyo y ninguna sabía de las otras: Tab 1 con su cola FIFO de uno, Tab 2 con un análisis y una copia, y Tab 3 bloqueando **por `session_id`**, así que N proyectos podían correr fases a la vez. Sumado: tres o más `mkvmerge`/`ffmpeg`/`dovi_tool` peleándose por 4 núcleos y un solo pool ZFS.

Lo evidente es que todo va más lento. Lo que no se ve es peor: **`_adaptive_timeout` y el modelo de ETA se anclan en `ffmpeg_wall_seconds`**, así que una medición tomada con contención envenena en silencio las dos calibraciones de las que depende todo el progreso medido, y un timeout calculado a partir de ella puede quedarse corto en el job siguiente.

`workload.py` es un registro **en memoria** de lo que está corriendo (este proceso es el único que arranca trabajo, igual que con `_cmv40_activas`). Política: **409 diciendo qué bloquea, en qué pestaña y desde cuándo**. Frentes cubiertos: `POST /api/sessions/{id}/execute` (antes de encolar), el análisis extendido y la copia desde Library de Tab 2, los nueve endpoints de fase de Tab 3 y los dos pre-flight. `GET /api/activity` lo expone para que la UI diga *qué* bloquea.

Tres matices que son el contrato:

- **Nadie se bloquea a sí mismo.** El hueco se ocupa con la clave de la sesión, así que un proyecto de Tab 3 que avanza a su fase siguiente pasa — es el mismo job. Sin eso el auto-pipeline se detendría solo tras la primera fase.
- **La cola de Tab 1 espera, no falla.** El 409 al encolar deja una ventana estrecha (el trabajo N libera el hueco, el usuario lanza algo en Tab 2, la cola va a arrancar el N+1). Fallar un trabajo ya encolado por algo que el usuario hizo después sería gratuito, y la cola es justo donde esperar es lo natural.
- **Abrir un MKV en Tab 2 NO se bloquea** (`/api/mkv/analyze`). Es cómo se navega, está acotado, y bloquearlo dejaría la pestaña inservible mientras corre un rip. Igual con `mkvpropedit`, que es O(1).

**Regla**: el hueco se libera SIEMPRE en un `finally` y por la clave propia. Si eso se escapa, la app queda bloqueada para todo lo demás hasta reiniciar.

### Patrón crítico: NO bloquear el event loop con `model_dump_json` síncrono

**Regla absoluta** para cualquier código async que se ejecute en hot paths (log callbacks de subprocess, broadcasts WS, polling intensivo): **NUNCA llamar `save_session()` o `save_cmv40_session()` síncronos**. Usar las versiones async (`save_session_async`, `save_cmv40_session_async`) que mueven la serialización JSON + write atómico al thread pool.

**Por qué importa**: `session.model_dump_json(indent=2)` es CPU-bound y se ejecuta DENTRO del event loop si se llama desde un coroutine. Para sesiones con miles de líneas en `output_log` (CMv4.0 ffmpeg extract genera 5K-10K líneas, JSON ~500KB-1MB), cada serialización tarda 100-500ms y bloquea el event loop completo durante ese tiempo. Síntomas:
- Reader del subprocess (`_run_streaming` / Tab 1 log loop) deja de leer del pipe → el pipe del subprocess se llena → ffmpeg/mkvmerge se bloquea en write → procesamiento del frame se detiene.
- Cuando el event loop se desbloquea, el reader procesa todas las líneas del pipe en burst. El throttle de 500ms en `_run_streaming` descarta TODAS excepto la primera (porque tienen `now` ≈ mismo).
- Resultado: gaps de varios segundos en el log visible al usuario aunque el subprocess esté funcionando normal.

**Solución estándar**:
1. **Helpers en `storage.py`**: `save_session_async` y `save_cmv40_session_async` toman un snapshot en async (rápido, µs) y mueven `serialize + write` a `asyncio.to_thread` para no bloquear.
2. **Snapshot barato**: `_log_safe_snapshot` (storage.py) sustituye al `model_copy(deep=True)` — copia solo las listas que crecen durante un job (`output_log`, `phase_history`/`execution_history`) y comparte el resto. El deep copy clonaba también campos estables y enormes (3.914 `L2Combo` en una sesión real) en cada save.
3. **Throttle + lock por sesión**: `_maybe_save_session_throttled` (`routers/tab1.py`, >1s o >=20 líneas) y `_cmv40_maybe_persist_log` (`routers/cmv40.py`, >5s o >=50 líneas). Si lock libre → fire-and-forget task; si lock ocupado → descartar trigger (la línea sigue en `output_log` RAM y se persistirá con el siguiente). Garantía: el callback `await` retorna en <1ms siempre.
4. **Flush al terminar fase**: `_flush_session_save` (Tab 1) y `_cmv40_flush_log` (Tab 3) esperan al lock para garantizar durabilidad antes de marcar la sesión como done.
5. **Broadcasts WS**: `_send_ws_with_timeout` con `asyncio.wait_for(timeout=2s)` en `asyncio.create_task` — un cliente zombie nunca bloquea el log loop.
6. **El progreso no se persiste**: las líneas `§§PROGRESS§§` viajan SOLO por WS — no entran en `output_log` ni disparan save (`_cmv40_progress_should_emit`, dedup por (pct,label) con heartbeat de 5s). El ticker las emite cada 2s aunque el valor no cambie; con el pct saturado al 95% en la recta final de una fase larga eran cientos de reescrituras del JSON entero. El watermark del frontend (`_appendCMv40Log`) NO las cuenta, justamente porque ya no existen en `output_log`. Cubierto por `test_cmv40_log_throughput.py`.

**Cuándo usar la versión síncrona**: solo en endpoints REST one-shot donde el bloqueo de 100-500ms es aceptable (la respuesta tarda eso pero no afecta a otras corutinas en hot loops).

**El guard: `test_event_loop_limpio.py`.** Recorre el AST de `main.py` y `routers/cmv40.py` y falla si una llamada cuyo coste escala con el DATO (`json.load`, `shutil.rmtree`/`copytree`/`copyfileobj`, `Path.rglob`, `os.walk`) aparece en el cuerpo de una corrutina. No cuenta lo que vive dentro de una `def` síncrona anidada, que es el patrón de `asyncio.to_thread(_helper)`. La lista de excepciones está **vacía a propósito**: si hace falta una, que cueste escribir el motivo. Y un segundo test falla si una excepción deja de corresponder a código real, para que no se quede vigilando nada.

Lo que cazó al escribirlo (y no era ninguno de los que iba buscando): `/api/isos`, el endpoint legacy, hacía `rglob` sobre `/mnt/isos` en el bucle — miles de `stat` contra el NAS — mientras su reemplazo `/api/sources` ya lo hacía en un thread.

Sitios que se cerraron con él, todos medidos: el volcado del perfil de luminancia (`json.load` de 1,12 GB + recorrer 243.552 frames), las series del chart de sincronización (24,1 MB por petición), el recuento de `cleanup-preview` (las 88 sesiones completas + `rglob` de cada workdir) y los `rmtree` de 250-400 GB de `cmv40_cleanup`/`cmv40_delete`, que sobre ZFS son segundos con la app entera congelada, healthcheck del contenedor incluido.

**Tests de validación**: con session de 10K líneas (JSON ~1MB), el event loop debe quedarse bloqueado <50ms p95 incluso bajo cascadas de saves. (Los tests `test_eventloop.py` / `test_tab1_loop.py` que cubrían esto vivieron en commits históricos; ver la suite actual en `app/tests/` — comando abajo.)

### Auto-rewind y forward-roll en GET /api/cmv40/{id}

Al abrir un proyecto, el endpoint reconcilia el estado persistido con la realidad del filesystem:

- **Auto-rewind** — si `phase ∈ {remuxed, validated}` pero el MKV esperado (`.mkv.tmp` o `.mkv`) no existe en /mnt/output, retrocede a `phase=injected` para que la UI muestre Fase G como siguiente. Sin esto, el usuario era llevado a Fase H y la ejecución fallaba con "MKV final no existe — ejecuta Fase G primero". **No se aplica a `phase=done`**: una vez el job terminó, el MKV es responsabilidad del usuario (workflow normal: mover a biblioteca externa). Para regenerar, el usuario tiene "Rehacer Fase G" en la card done del panel.
- **Forward-roll** — complementario: si `phase ≤ injected` pero hay un `.mkv.tmp` en /mnt/output Y `phase_history` tiene un `remux` con `status='done'`, adelanta a `remuxed`. Recupera proyectos atascados sin re-mux cuando el auto-rewind disparó por error.

Ambos saltan en sesiones archivadas y en DEV_MODE.

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
GET    /api/cmv40/{id}                      (incluye campo artifacts con sizes;
                                             ?include_log=false omite output_log
                                             — lo usan los pollers mientras el WS
                                             entrega el log en vivo)
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
- **Umbral de confianza**: 85% (`SYNC_CONFIDENCE_THRESHOLD`, >= 0.85 Pearson). Protege contra RPUs de películas incompatibles

**El recorte de la ventana usa un origen COMÚN a las dos series.** `detect_sync_offset` buscaba el primer frame con brillo *en cada serie por separado* — y el desfase típico es un logo de estudio al principio del BD que la versión de streaming no trae. Un logo es oscuro, así que el recorte alineaba las dos ventanas antes de correlacionarlas y devolvía **offset 0 con 100 % de confianza**: la señal más tranquilizadora posible justo cuando se equivoca. El contraste A/B lo delata y es el test de referencia: logo oscuro de 72 frames → 0; el MISMO desfase con logo brillante → −72. El algoritmo estaba bien; fallaba el recorte.

Dos trampas más de la misma función, ambas con test:
- **Solape mínimo.** Con `max_offset` y la ventana de comparación ambos a 200, en el extremo del barrido quedaba UNA comparación, así que una coincidencia por casualidad ganaba con RMS 0 y confianza 100 %. Reproducible: 300 muestras de dos pelis distintas con `tgt[0] = src[149]` → `offset=-149, 100 %`.
- **Varianza cero.** Con una serie plana todos los offsets empatan y gana el primero. `compute_sync_confidence` ya trataba ese caso aparte (`no_variance`); aquí faltaba, y con `None` en el MaxCLL además reventaba con TypeError.

Es una **función pura**: los tres casos se fijan con series sintéticas, sin disco ni RPU real (`test_cmv40_sync_offset.py`).

**Las correcciones se ENCADENAN, no se recomponen.** `dovi_tool editor` no edita in situ, y la Fase E partía siempre de `RPU_target.bin`; para no perder los pasos anteriores el endpoint sumaba sus frames y los colapsaba en un único rango en la cabecera. Con pasos del mismo signo sale lo mismo, pero alternando (quitar 10 y luego duplicar 5) el **orden** decide qué frames se tocan y el Δ final coincide en ambos casos — que es justo lo que hace que el criterio de avance no lo note. Ahora cada paso se aplica sobre el resultado del anterior, que es el sistema de coordenadas en el que el usuario lo dibujó, y `sync_config` guarda `{"steps": [...], "total_removed", "total_duplicated"}` (acepta el formato aplanado de las sesiones ya existentes). La escritura pasa por `RPU_synced.tmp.bin` + `os.replace`: una pasada que falle deja intacta la corrección anterior. `run_phase_e_correct_sync` **no** toca `session.sync_config` — lo escribe el endpoint con todos los pasos, y sobreescribirlo desde la fase borraba los anteriores.

**El chart pide una VENTANA, no la película.** `per_frame_data.json` de un UHD son 24,1 MB (243.552 frames) y el endpoint los devolvía todos: 97 ms de parseo + 79 ms de Pearson + 92 ms de re-serializar en cada petición, para pintar un canvas de ~1500 px — 160 puntos por píxel. Ahora `GET /sync-data?desde&hasta&cubos`:

- Las series se cachean por **stat** del fichero como `array('i')` — 3,9 MB en RAM para un UHD, contra los ~250 MB que ocuparían los dicts. La Fase E regenera el volcado tras cada corrección, y el stat lo detecta.
- Las **métricas se calculan una vez** y van sobre la serie COMPLETA, nunca sobre la ventana: un desfase fuera del rango visible sigue siendo real.
- Por cubo se emiten el **mínimo y el máximo**, no la media: un promedio sobre 160 frames se come los picos que delatan un corte de escena desplazado. El canvas dibuja la banda min–max cuando la respuesta viene reducida (`downsampled`), porque si solo se pintara el máximo las dos curvas quedarían convertidas en envolventes parecidas y se podría **perder la desalineación que esta pantalla existe para ver**.
- Si la ventana cabe en los cubos no se reduce nada: el preset de 30 s son ~720 frames y el usuario ve el dato exacto.

Medido: 24,1 MB → **0,38 MB** la película entera y 0,07 MB un zoom; la primera carga son 303 ms **en un thread** y los cambios de zoom, 0 ms.

**El criterio de avance (Δ=0 ∧ confianza ≥ 85 %) lo aplica el backend.** `evaluate_sync_gate` lo resuelve, `sync-data` lo expone como `sync_gate` y `POST /mark-synced` devuelve **409** si no se cumple. Vivía SOLO en `app.js` (`canConfirm = delta === 0 && confOk`, que deshabilitaba el botón) mientras el endpoint aceptaba cualquier cosa: no había copia en el servidor con la que desincronizarse porque no había servidor. `force=true` es la salida explícita para el caso legítimo (grading que diverge de verdad, validado a ojo en el gráfico), que antes no tenía ninguna. **No** se comprueba cuando `plan.inputs.skip_sync_review` (la Fase D se omite: nadie ha mirado el gráfico porque no había que mirarlo, y el volcado puede no existir) ni cuando falta `per_frame_data.json`.

### Reglas y principios

- Cada transición de fase es explícita (click del usuario) — no hay cascadas automáticas
- Los artefactos de fases anteriores se preservan para permitir reentrar tras crash/reinicio
- Cleanup es destructivo e irreversible → archived=true → proyecto en modo solo lectura. `cleanup` y `delete?clean_artifacts=true` devuelven **409 con una fase en curso** (`_cmv40_guard_not_running`): hacían `rmtree` del workdir por debajo del `dovi_tool` que lo estaba escribiendo, y lo que veía el usuario era un fallo incomprensible en el log. `cleanup-bulk` ya saltaba los proyectos en ejecución
- **Cancelar es cooperativo, no solo un SIGTERM.** `raise_if_cancelled()` se llama **antes de cada subproceso** (`_run`, `_run_streaming`), que es el punto natural: entre un comando y el siguiente es donde una fase puede parar sin dejar un artefacto a medias. Antes la cancelación dependía solo de matar el proceso en curso, así que cancelar **entre** dos pasos no tenía efecto y la fase seguía adelante con el comando siguiente. El predicado lo instala el orquestador en un **ContextVar** (`set_cancel_check`) y no como parámetro: las nueve `run_phase_*` reciben `(session, log_callback, proc_callback)` y un cuarto argumento obligaría a tocar las firmas, sus catorce call-sites y todos los tests; las tasks de asyncio heredan el contexto, así que llega solo a las corrutinas hijas. Lanza `CMv40Cancelled`, que **no es un error del pipeline**: se registra la fase como `cancelled`, sin manchar `error_message`
- Errores NO bloquean el proyecto: mantienen la fase previa, solo escriben `error_message` descartable
- Validación de CM version target = v4.0 (aviso, no bloqueante)

---

## Integraciones externas

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
- Matching fuzzy compuesto: max(SequenceMatcher, token-set Jaccard, containment) sobre acentos strippeados. El `containment` opera sobre **tokens** (no subcadena de caracteres) con gate de cobertura ≥60%: evita que un título corto matchee uno largo que lo contiene como subcadena (caso real: "The Ring" ⊂ "The Lord of the Rings: The Fellowship of the **Ring**" daba 0.875). Cubierto por `test_cmv40_recommend_match.py`. La misma `_similarity` la consume `rec999_drive_match.rank_candidates`.
- Normalización de romanos (II→2, III→3…) y stop-words (the, la, de…)
- Multi-candidato TMDb (top-5) — prueba cada uno contra el sheet
- Umbrales adaptativos: **0.72 año exacto · 0.82 año ±1 · 0.88 sin año**
- Dedup por (slug, año) — permite matches distintos con mismo título (El Rey León 1994 vs 2019)

### Sheet DoviTools (R3S3T_9999)
- El sheet es un **XLSX importado en Drive** (no Sheet nativo) — Sheets API v4 falla con "not supported"
- Prioridad de fetch: **Sheets API v4 (Google key)** → **XLSX + openpyxl (rich-text hyperlinks)** → gviz HTML → CSV export → disk cache
- XLSX + openpyxl es la única vía que preserva los rich-text hyperlinks incrustados manualmente en celdas
- Extracción de URLs: primero `cell.hyperlink.target`, fallback a regex sobre texto plano
- Parseo de 3 secciones: cols 0-4 (no factible), 6-11 (factible), 13-18 ("Not Sure! / probably ok"). Cada fila lleva `section` (`infeasible`/`feasible`/`probably_ok`) — el consumidor NECESITA saber de qué bloque viene
- Sheet ID + GID hardcodeados a la hoja de DoviTools (variables de entorno para override)

#### Semántica del veredicto — el sheet NO responde nuestra pregunta

La columna izquierda evalúa si el disco se puede **convertir a P8.1 single-layer** (objetivo mayoritario de la comunidad), NO si se le puede añadir CMv4.0. Medido sobre la hoja real (828 filas, 792 títulos): **219 de las 326 filas no-factibles** tienen el motivo `can only be played on a FEL device and can't be converted to P8 without baking FEL into BL`, que **no aplica a esta app** — el workflow `p7_fel` es "demux + merge CMv4.0 + preserva FEL" y el EL solo se descarta en `p7_mel` (donde está vacío). A nivel de título: 190 de 291.

Además **32 títulos están en dos secciones a la vez** con veredictos opuestos (izquierda: "no convertible a P8"; derecha: "cmv4.0 bloc can be restored to the P7 RPU (workflow 2-3)" ← literalmente lo que hacemos). Caso de referencia: **Obsession 2026**.

Consecuencias en el código (`services/cmv40_recommend.py`):
- `classify_blockers(notes)` → lista de motivos (`p8_only`, `static_dv`, `grading`, `no_bd`, `other`, `unspecified`). Devuelve TODAS las categorías que matcheen, no la primera: una nota puede mezclar el tema P8 con un problema real. `blockers_apply_to_fel_workflow()` descarta solo `p8_only`.
- `_matching_rows()` recoge **todas** las filas del título (factibles primero) y `recommend()` las devuelve en `rows[]`. `_best_match` prefiere la factible **en empate** — antes ganaba la primera emitida por el parser, que es siempre la izquierda: 31 de los 32 duplicados mostraban ❌ falso.
- `_build_verdict()` produce 5 estados: `recommended` (verde) · `caveats` (ámbar: avisos relevantes o sección "Not Sure!") · `p8_only_note` (azul informativo, NO rechazo) · `not_feasible` (rojo: `static_dv`/`grading`/…) · `unknown`.
- Los campos planos del result (`dv_source`, `sync_offset`, `notes`…) vienen de la **fila primaria** = la factible si existe (la izquierda no trae `sync_offset`).
- **Persistencia**: `CMv40Session.sheet_recommendation` se rellena al crear el proyecto (`_cmv40_hydrate_sheet_recommendation`, background best-effort) y con `POST /api/cmv40/{id}/refresh-sheet` para proyectos previos. Antes el veredicto solo existía en el modal de creación y se perdía justo antes de Fase D.
- **Fase D contrasta offsets**: `sheet_sync_hint()` (en `phases/cmv40_pipeline.py`, junto a `detect_sync_offset`) compara el desfase documentado con el detectado por cross-correlation → `agrees` (±2 frames), `sign_flipped` (misma magnitud, signo contrario) o divergencia con aviso. Se expone en `GET /api/cmv40/{id}/sync-data` como `sheet_sync`.
- Cubierto por `test_cmv40_sheet_verdict.py` (32 tests, incluye end-to-end con el parser CSV real).

**Sheet y bins del Drive son rutas independientes**: `/api/cmv40/recommend` (sheet) y `/api/cmv40/repo-rpus` (Drive) no se consultan entre sí y usan matchers con umbrales distintos (sheet 0.72/0.82/0.88 · drive 0.65/0.78). El veredicto no filtra bins ni el bin cambia el veredicto — se pintan juntos en el modal. Composición medida del repo: 1857 `.bin` → 473 `trusted_p7_fel_final`, 360 `trusted_p8_source`, 114 `trusted_p7_mel_final`, 567 `unknown`, 343 artefactos; 254 retail / 688 generated.

### Drive DoviTools
- Listado recursivo de `.bin` con Drive API v3 + paginación (profundidad máx 5)
- Caché en memoria + disco (`/config/rec999_drive_cache.json`, TTL 24h)
- Match fuzzy de filename → candidatos rankeados por score (reusa lógica de `cmv40_recommend`)
- Descarga streaming vía `files.get?alt=media&key=...` al workdir del proyecto
- Endpoint `target-rpu-from-drive` (hermana de B1-path / B2-mkv)

### Modal "Nuevo proyecto CMv4.0"
- Ancho 980px, responsive hasta viewport-48
- 3 tabs de target: **📦 Repo DoviTools** (default) · **🎬 Extraer de MKV** · **📁 Carpeta local** (residual)
- Banner de recomendación con status-badge pill + chips en fila (Fuente · Sync · Verificación) + note con botón "Abrir ↗"
- Footer compacto en 1 línea: toggle auto-pipeline + Cancelar/Crear

---

## Sistema de Trust para bins pre-validados

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

`_evaluate_trust_gates` compara source BD vs target. Cada gate lleva un campo `severity` que decide cómo se ramifica el flujo si falla:

| severity | Comportamiento |
|---|---|
| `ok` | Gate pasa — no requiere acción |
| `warn` | Mensaje informativo, no bloquea |
| `sync_review` | Fase D (revisión visual) puede arreglarlo o dar pistas — no se salta D |
| `ack_required` | Técnicamente inyectable pero el resultado se degrada — Fase B setea `session.awaiting_critical_ack=True` y guarda `critical_gate_failures`. La UI muestra banner de confirmación. `POST /api/cmv40/{id}/acknowledge-critical-gates` desbloquea + añade `sync_verification_pause` a `phases_skipped` (Fase D no puede arreglar el problema) |
| `hard_abort` | Bin rechazado — `_run_phase_b` raise RuntimeError, el usuario debe cambiar de target o cancelar |

Escalas por gate:
- `frames` → exacto OK · cualquier diff = `sync_review`
- `cm_version` → v4.0 OK · resto = `hard_abort`
- `has_l8` → presente OK · ausente = `hard_abort`
- `l5_div` → ≤5 px OK · 5-30 = `sync_review` · >30 = `ack_required` (edición distinta, D no la arregla)
- `l6_div` → ≤50 nits OK · 50-200 = `warn` · >200 = `ack_required` (peak muy distinto)
- `l1_div` → ≤5% OK · 5-20 = `warn` · >20 = `ack_required`

#### El gate L5 se mide frame a frame, y la ausencia de bloque es el neutro

El summary de `dovi_tool` resume el L5 de toda la película en una línea, así que
en un máster con escenas expandidas (IMAX / open matte) el gate estático dispara
`ack_required` por un falso positivo. El refinamiento compara los dos RPUs con
`export --levels level5` y **zonifica** el resultado: intro (primer 5%), cuerpo
(90% central) y outro (último 5%); solo el cuerpo decide, porque una divergencia
en un logo o en los créditos no afecta a nada.

Dos cosas que costaron una tarde de medición y que no son opcionales:

- **Un frame sin bloque L5 vale `(0,0,0,0)`, no "sin dato".** Así codifica el
  Blu-ray las escenas expandidas: en The Mandalorian and Grogu son **76.774 de
  190.021 frames** sin bloque, frente a un bin que sí escribe el `(0,0)`
  explícito. Sin esa regla el disco pasa de un 0,29 % de divergencia a un 40 % y
  un título correcto acaba pidiendo confirmación al usuario.
- **El emparejamiento va por `row["frame"]`, nunca por el índice de la fila.**
  Lo anterior muestreaba 24 frames y usaba el índice como si fuera el número de
  frame; con 113.247 filas en el BD contra 190.021 en el bin la intersección de
  los dos muestreos era **un único par** (el índice 0, que además comparaba el
  frame 24 del source contra el frame 0 del target). Ese par cayó en 'intro', el
  cuerpo se quedó con cero muestras y `body_coverage` valía `1.0` por el `else`
  del denominador: **el gate aprobó sin haber mirado nada**, y acertó de
  casualidad. Hoy `body_total == 0` mantiene `ack_required` — sin evidencia no se
  aprueba. Muestrear tampoco hacía falta: el export ya devuelve todas las filas
  y comparar los 190.021 frames son ~90 ms (en `asyncio.to_thread`, que escala
  con la duración de la peli).

**El criterio no es solo el porcentaje: también el tramo contiguo.** Veinte
transiciones de medio segundo suman lo mismo que ocho minutos de película con
otro encuadre, y no son lo mismo. Calibrado sobre los proyectos reales del NAS
(112 sesiones; el refinamiento se ejecuta en el 5,4 % de los jobs): el mayor
tramo divergente de un título cuyos dos másters SÍ coinciden son **278 frames
(11,6 s)**, frente a **8.566 y 12.001** (5,9 y 8,3 min) en los que el bin trae
las escenas IMAX abiertas y el BD no. Factor 30 entre los dos grupos, así que
`TRAMO_L5_MAX_SEGUNDOS = 40` no está en zona de duda. Los umbrales porcentuales
(90 % / 70 %) **no se tocaron**: con la medición corregida ningún proyecto del
corpus pasa de aprobado a ACK por esa vía; los tres que cambian de veredicto lo
hacen por el tramo, y son los tres bins «Variable L5» sobre un BD plano.

**El detalle NO se persiste.** La comparación recorre ~190.000 frames y en la
sesión solo entran los agregados, el histograma de valores (tope 8) y los 20
tramos mayores. El proyecto más grande del `/config` real son 3,89 MB y no se va
a multiplicar por un volcado.

#### El nombre del bin como contra-chequeo — avisa, no decide

Los bins de la comunidad suelen declarar el caso en el nombre
(`…P7 MEL Variable L5 (retail cmv4.0 restored).bin`, `…2025 IMAX BD_P7 FEL…`).
Medido sobre los 99 proyectos con nombre y perfil L5 conocido: **TP=6, FP=0,
FN=7, TN=86 → precisión 100 %, recall 46 %**. Esa asimetría fija el uso:

- el nombre declara variable **y** medimos constante → la contradicción apunta
  **siempre a nuestra medición**, y se avisa. Este chequeo solo habría cazado el
  fallo del muestreo: el gate escribió `tgt_variable_l5=False` sobre un bin
  llamado literalmente «…VARIABLE L5.bin»;
- el nombre calla → no concluye nada, y **no relaja ninguna severidad**.

`pistas_de_procedencia` (en `rpu_analyze.py`, pura) normaliza `._-` a espacios
—si no, `variable_L5` se escapa— y busca con frontera de palabra, porque `imax`
es subcadena de `Climax`. La nota del sheet se muestra como contexto pero **no**
se cruza: solo 1 de 24 proyectos con notas menciona L5, así que como detector no
da. Cubierto por `test_cmv40_gate_l5.py` (24 tests, con los cuatro escenarios
reales del corpus y el contraste A/B que justifica el umbral de tramo) y
`test_cmv40_procedencia.py`.

Campos relacionados en `CMv40Session`: `awaiting_critical_ack`, `critical_gate_failures`, `user_acknowledged_degradation`, `pipeline_aborted`. La flag `target_trust_ok` se mantiene como resumen booleano (true sólo si todos los gates son `ok`/`warn`).

### Skip automático de fases

Si `target_trust_ok=True` y `trust_override == "auto"`:
- **Fase D** (revisión visual) se salta — `_cmv40AutoMarkSynced` avanza directo a `sync_verified`
- **Fase F merge** se salta solo en `trusted_p7_fel_final` — inject directo del bin sin `_merge_cmv40_into_p7`

Se registra en `session.phases_skipped` para UI (phase-strip muestra las fases omitidas con opacity + borde punteado).

### Override manual

`session.trust_override = "force_interactive"` fuerza rama A completa incluso con target trusted — todas las validaciones manuales vuelven a ejecutarse.

---

## Versión de la app + chequeo de actualizaciones

La aplicación expone su versión y comprueba si hay una más reciente publicada en GitHub.

### Resolución de versión

`GET /api/version` devuelve la versión actual. La resuelve con prioridad:
1. **Docker (build con args)**: lee `APP_VERSION` + `APP_COMMIT` del entorno (los pasa el workflow de CI al construir la imagen GHCR)
2. **Dev local**: ejecuta `git describe --tags --always --dirty` desde el working tree

Cachea el resultado en memoria (`_VERSION_CACHE`) — la versión no cambia en runtime. Devuelve también `is_tagged`, `is_dirty`, `is_dev` para que la UI sepa si está en un release puro (`vX.Y.Z`), en un commit posterior a un tag (`vX.Y.Z-N-gHASH`) o en `dev`.

### Chequeo de actualizaciones

`GET /api/version/check-updates?force=&simulate_current=` consulta la API pública de GitHub:
1. Intenta `/repos/Aldicarus/hdo-iso-converter/releases/latest` (preferido — trae `release_notes` markdown)
2. Si vacío → fallback a `/tags?per_page=30`, filtra a semver tags y elige el mayor (común si el repo solo tagea via `git tag` sin formalizar Releases)

Cachea en `/config/update_check_cache.json` con TTL **1h** (la API pública limita a 60 req/h por IP sin auth). `force=true` ignora cache.

`simulate_current=v2.0.0` (modo dev): override de la versión actual para probar la UI del banner sin publicar release nueva. Nunca persiste cache cuando se simula.

Respuesta:
```
{
  current, latest, update_available, release_url, release_notes,
  published_at, checked_at, cached, simulated, ignored_version
}
```

`update_available` se calcula como `_semver_gt(latest, current)` Y `latest != ignored_version`. La UI permite al usuario marcar una versión como "ignorar" — se guarda en `app_settings.json` como `update_ignored_version` y silencia el banner para ese tag concreto.

### UI

Pill de aviso de nueva versión aparece en el header global (al lado de Help/Lookup/Settings) cuando `update_available=true`. La pill enlaza al `release_url` en GitHub. Clic en "ignorar" persiste `update_ignored_version` y oculta la pill hasta que haya un tag posterior.

---

## Reglas de desarrollo

### Idiomas
- El código (variables, funciones, clases) en **inglés**
- Strings de UI, mensajes de lógica y comentarios en **español**
- Los literales de pistas siguen exactamente la spec: "Castellano TrueHD Atmos 7.1", "Inglés DTS-HD MA 5.1", etc.

### Análisis del disco (Fase A — pipeline extendido)

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

3. **PGS packet counting** (opcional, no bloquea si falla) — proxy fiable forzado/completo/AD
   - `parse_mpls_pg_streams()`: parser MPLS binario en Python puro (~150 líneas, sin deps). Lee la STN_table del primer PlayItem y devuelve PIDs PGS + language code en orden mkvmerge. Crítico: `UO_mask_table` son **8 bytes**, no 12.
   - `count_pgs_packets_ts_parse()`: parser TS directo del m2ts. Lee ~4 GB (presupuesto `sample_bytes`) y cuenta paquetes por PID. NO usa ffprobe (incompatible con UDF: `-read_intervals` devuelve `nb_read_packets=N/A` o stdout vacío en m2ts montado por loop). **Muestreo distribuido** (no HEAD-only): el presupuesto de bytes se reparte en `sample_segments` tramos (default 4) equiespaciados a lo largo de TODO el fichero (inicio/33%/66%/final), realineando a frontera de paquete tras cada seek (`_find_alignment`). Leer solo los primeros 4 GB desde el inicio bastaba en BD de ~30-50 GB, pero en UHD de ~100 GB son ~5 min del arranque: una completa con poca actividad inicial (típico: subs ES) quedaba con cuenta parecida a un forzado y phase_b los confundía por ratio (caso real La Momia 2026: ES completo 1096 vs forzado 988 = 1.1×). Cubierto por `test_pgs_sampling.py`. mediainfo NO sirve como alternativa: con parse rápido no da bitrate de PGS (haría falta full-scan de los 99 GB).
   - **Robustez**: bytearray buffer con leftover entre reads → inmune a short reads (NAS bajo contención). Auto-detect 192-byte BDAV vs 188-byte standard TS por sync bytes consecutivos.
   - **Rangos PIDs**: spec BDA-016 dice 0x1200-0x121F pero discos modernos usan hasta 0x12FF (visto 0x12A0-0x12AA). El parser MPLS da los PIDs reales; fallback al rango extendido 0x1200-0x12FF.
   - **Inicialización a 0**: si MPLS aporta lista de PIDs, todos se inicializan a 0 antes del scan. Forzados con eventos tardíos (no en sample) siguen apareciendo en el resultado posicional → phase_b clasifica por ratio relativo.
   - **El emparejamiento conteo ↔ pista lo hace `asignar_packet_counts`, y si no puede garantizarlo NO asigna.** Eran tres `zip(bdinfo.subtitle_tracks, sorted_counts)` copiados (uno por punto de entrada del análisis) y `zip` **trunca en silencio por la lista corta**. La lista puede venir corta de verdad: la inicialización a 0 de todos los PIDs solo ocurre **cuando el MPLS aporta la lista**, y `run_full_analysis_for_m2ts` va SIEMPRE sin MPLS (`pid_list=None`), así que un PGS que el muestreo no vio no sale con cero — **no sale**, y todo lo que va detrás se desplaza un puesto. El resultado no es un error: es cada pista con el conteo de su vecina, y sobre eso corre la clasificación forzado/completo con sus umbrales de 3.0×/500/3000. Ahora: longitudes distintas → no se asigna nada y se avisa; phase_b cae al patrón estructural, que es lo que ya hace cuando el conteo falla entero. **Un conteo desplazado es peor que ningún conteo, porque parece un dato.** El orden ascendente de PID sigue siendo preferente (es lo ejecutado hasta hoy y coincide con mkvmerge en los 41 discos del corpus); el orden del MPLS —la STN_table, que es el de presentación— solo entra si el ascendente no cuadra por idioma y el del MPLS sí. Cubierto por `test_pgs_asignacion.py`. Medido sobre el corpus: 39 sesiones con conteo PGS, **0 con la firma de la truncación** (ceros en la cola), así que era preventivo — pero los 21 BDMV y 14 ISO llevan MPLS y **de M2TS no hay ni uno**, o sea que la vía sin red no la ha ejercitado ningún disco.
   - **Progreso**: monitor task asyncio cada 0.5s lee `progress_state["bytes_read"]` (actualizado por el thread de parsing) y emite pct + ETA al modal.

4. **dovi_tool** (opcional, solo si hay EL, no bloquea si falla) — análisis RPU Dolby Vision
   - ffmpeg extrae 30s del EL: `-map 0:v:1 -c:v copy -bsf:v hevc_mp4toannexb -t 30`
   - `dovi_tool extract-rpu` → RPU.bin
   - `dovi_tool info --summary` → parseo regex
   - **Resultado**: Profile (4/5/7/8), FEL/MEL (definitivo), CM version (v2.9/v4.0), L1/L2/L5/L6 metadata
   - Ficheros temporales limpiados en `finally`

5. **Enriquecimiento** — los datos de MediaInfo y dovi_tool se inyectan en BDInfoResult
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
- **Clasificación forced/complete por ratio** ([phase_b.py](app/phases/phase_b.py) `_classify_lang`):
  - Por idioma, con packet_count disponible:
    - 1 sola pista: `<500 paq → forced`, `≥500 paq → complete` (no hay con qué comparar).
    - 2+ pistas: la de más paquetes (`biggest`) es la REFERENCIA para medir ratios. Cada otra pista
      se clasifica contra ella: `ratio ≥ 3.0 → forced` · `ratio < 3.0 → alternativa ambigua` (España/Latam, normal/SDH, comentarios).
    - **Pista con `packet_count == 0` (el muestreo la perdió) — fallback al patrón estructural**: un forzado muy disperso
      puede caer entero fuera de los 4 tramos del muestreo y salir con 0 paquetes. En ese caso NO se manda a "ambigua"
      (eso disparaba el falso aviso de "otra pista parecida" y no la auto-incluía); se usa la señal estructural del patrón
      de bloques/adyacencia (bitrate sintético de phase_a): si el patrón también dice forzado (`bitrate_kbps < 3.0`) → **forced**;
      si no hay patrón forzado → conservador → ambigua. El modelo ya define `packet_count=0` como "usar la heurística de patrones".
      Caso real: Obsession 2025, ES forzado `0x12A3` = 0 paq. (frente a completo 4516) — con la corrección se clasifica forzado.
      Cubierto por `test_subtitle_classification.py::test_obsession_forced_missed_by_sample_uses_pattern`.
    - **Completa elegida — NO siempre `biggest`** (Opción A): se incluye la **primera del disco** entre las completas
      "elegibles" — la de más paquetes suele ser la versión SDH (texto extra en momentos sin diálogo) y la primera la normal.
      Son **elegibles**: (a) las casi-idénticas a `biggest` (`ratio < 2.0`, ninguna puede ser un forzado disfrazado); **y**
      (b) las de banda `2.0–3.0` con **`≥ 3000` paquetes absolutos** (`COMPLETE_ABSOLUTE_THRESHOLD`) — tantos paquetes no caben
      en un forzado (ni en los "grandes" ~2000-2200), así que son completas normales. Las de banda `2.0–3.0` **por debajo de
      3000** siguen ambiguas y NUNCA se eligen (podrían ser un forzado grande — el bug que arregló 4b985d2).
      Casos reales: Scream 7 ES 12569(1ª)+12817(2ª) y EN 13410(1ª)+17308(2ª) (banda <2× → 1ª) · Imaginary 2024 EN 4187(1ª, normal)+9634(2ª, SDH),
      ratio 2.30× pero 4187≥3000 → se elige la 1ª. Cubierto por `test_subtitle_classification.py`.
    - Caso edge (`biggest` <500): todas son forced; el idioma no tiene complete en este disco.
    - **Un `packet_count ≥ COMPLETE_ABSOLUTE_THRESHOLD` (3000) NO puede ser forzado, tenga el ratio que tenga.**
      El ratio solo compara con `biggest`, y eso asume que el idioma tiene DOS magnitudes (una completa y sus
      forzados). Con **TRES** —normal + SDH + forzado— la del medio queda 4× por debajo de la SDH, cruza el
      umbral de 3× y salía etiquetada como forzada. Caso real **The Mandalorian and Grogu** (15 subs = 3 bloques
      × 5 idiomas): ES 5591 (normal) · 23022 (SDH) · 248 (forzado) → el MKV llevaba "Castellano Forzados" con
      la completa normal, "Castellano Completos" con la SDH, y **el forzado de verdad descartado**. El desempate
      es el volumen absoluto, la MISMA constante que ya rescataba la banda 2-3×: por encima de 3000 no cabe un
      forzado (los "grandes" rondan 2000-2200). Cubierto por `test_subtitle_classification.py::TestTresBloquesPorIdioma`.
  - La detección de audiodescripción por packet_count se RETIRÓ — era estructuralmente incorrecta
    (cualquier completo+forzado cruzaba el threshold ×1.3 mal calibrado). Única señal fiable de AD: código ISO 639 `qad`.
- **`DiscardedTrack.inferred_subtitle_type`** ([models.py](app/models.py)): cada sub descartado lleva su tipo inferido
  (forced/complete) según la heurística. Lo usa `recoverTrack` para nombrar correctamente los recuperados
  de cualquier idioma (Tailandés Forzados, Checo Forzados, etc.) — antes todos los recuperados se etiquetaban como Completos.
- **Orden de inclusión**:
  1. Forzados Castellano (default=True, forced=True)
  2. Completos VO
  3. Completos Castellano
  4. Forzados VO (forced=False — ver siguiente regla)
  5. Completos Inglés (si VO ≠ Inglés)
- **flag_forced de Matroska solo a Castellano**: aunque haya forzados de VO/Inglés extra en el MKV
  (subtitle_type='forced' + label "X Forzados (PGS)"), su `flag_forced` Matroska es `false`. Solo
  el forzado Castellano lleva la flag en el contenedor, así el reproductor no solapa varios al
  cambiar de audio. recoverTrack respeta la misma regla: si recuperas un forzado de idioma no-Castellano,
  el label es "X Forzados (PGS)" pero flag_forced=false.
- Labels: `{Idioma} Forzados (PGS)`, `{Idioma} Completos (PGS)`

### Tres bloques de subtítulos: hay que arreglar los DOS extremos

Un disco con `normal + SDH + forzado` por idioma rompe las **dos** señales estructurales, que asumían exactamente dos bloques:

- **`phase_a._build_subtitle_tracks`** partía en la primera repetición de idioma y marcaba forzado (bitrate sintético 1.0) *todo lo posterior* → la SDH del bloque 2 salía como forzada.
- **`phase_e._classify_sub_source_ids`** hacía lo mismo con los source ids → la categoría "forzados" de cada idioma contenía `[SDH, forzado]` y el matcher, que consume 1:1 y en orden, entregaba **la SDH** a la pista etiquetada "Forzados".

**Arreglar solo una es peor que no arreglar ninguna**: la selección sale bien y el contenido cruzado, que es el bug de Obsession 2025 otra vez y no falla nada visible. La regla del proyecto ya lo decía — las dos tienen que usar la misma base.

Generalización aplicada a las dos: **el forzado es la ÚLTIMA aparición de un idioma que se repite**. Con dos bloques da exactamente lo de antes (la última ES la del bloque 2); con tres, coloca la SDH donde toca; un idioma que solo aparece una vez sigue siendo completo. De paso desaparece el bail-out de `phase_a` («el bloque 2 tiene idiomas que no están en el 1 → no se detectan forzados»), que era una divergencia silenciosa: `phase_e` los marcaba igual. `test_tres_bloques_estructural.py` compara las dos señales con 1, 2, 3 y 4 bloques y con un idioma extra en el último.

### Capítulos (spec §5.3)
- **Capítulos reales** extraídos del MPLS en Fase A (no en Fase D). Disponibles desde la creación del proyecto.
- **Extracción en dos niveles** (`parse_mpls_chapters` en [phase_a.py](app/phases/phase_a.py)):
  1. `_extract_chapters_via_mkvtoolnix`: MKV mínimo (`mkvmerge --no-audio --no-video --no-subtitles`) + `mkvextract chapters --simple`. Preserva nombres custom del disco.
  2. **Fallback binario** `_parse_mpls_marks_binary`: parsea los **PlayListMark** del MPLS directamente. Necesario en discos UHD multi-segmento donde el nivel 1 hace **abortar a mkvmerge** (assertion `add_filelists_for_playlists`, SIGABRT) y devuelve `[]`. Calcula el tiempo absoluto de cada entry mark (tipo 0x01) sumando las duraciones de los PlayItems previos (`Σ(OUT-IN) + (mark_ts - IN)`, ticks 45 kHz). Mismo layout de PlayItem validado por `parse_mpls_pg_streams` (IN en +12, OUT en +16). Sanity check: descarta si <2 marks o si el último excede la duración total +5% (multi-ángulo no soportado). También descarta un "end mark" final degenerado (≤1s del total) que no es un capítulo navegable — mkvmerge tampoco lo cuenta (caso Avatar: 58 marks → 57 capítulos). Cubierto por `test_mpls_chapters.py`.
- Si no hay capítulos → generación automática cada 10 min desde el primer intervalo
- **Botón "🏷️ Nombres genéricos"**: visible si algún capítulo tiene nombre custom. Reemplaza todos por "Capítulo XX".
- **Botón "🔄 Restaurar del disco"**: visible en fuentes con MPLS (iso/bdmv) tanto tras ediciones manuales **como cuando los capítulos son auto-generados** (el disco puede tener capítulos reales que no se pudieron extraer la 1ª vez — caso multi-segmento). Oculto en m2ts. Re-monta ISO y re-extrae con `parse_mpls_chapters` (con confirmación; texto dinámico según auto/editado).
- **`name_custom: bool`**: `false` = auto-nombrado, `true` = editado manualmente

### Acceso al ISO — Loop mount directo (UDF 2.50)
- `mount -t udf -o ro,loop {iso} /mnt/bd/{nombre}_{pid}/` → mkvmerge lee desde el mount point → `umount` en `finally`
- Requiere `privileged: true` en Docker
- **MOUNT_BASE**: `/mnt/bd` (creado en entrypoint.sh)
- `unmount_iso()`: umount normal, fallback lazy, limpia directorio

### Abstracción Source (v2.6+) — soporte BDMV/M2TS sin ISO

Encapsula el origen en una sola interfaz async context manager
([`phases/iso_mount.py`](app/phases/iso_mount.py)). 3 tipos:

| Tipo | Path apunta a | Pre | Post |
|---|---|---|---|
| `iso` | fichero `.iso` | `mount_iso` → `/mnt/bd/...` | `unmount_iso` |
| `bdmv_folder` | carpeta con `BDMV/` | no-op (verificar estructura) | no-op |
| `m2ts` | uno o varios `.m2ts` sueltos | no-op | no-op |

Uso típico:

```python
async with await Source.open(user_path) as src:
    if src.bdmv_root:
        # iso (ya montado) o bdmv_folder
        candidates = await identify_episode_candidates(src.bdmv_root)
    elif src.m2ts_paths:
        # m2ts directo (uno o varios)
        ...
```

`Source.detect_type(path)` clasifica por extensión (`.iso`, `.m2ts`) y por presencia de `BDMV/PLAYLIST/`. Lanza `SourceError` si el path es ambiguo o no soportado.

`safe_source_path(user_path, allowed_root)` valida path-traversal estricto. Cualquier `../` o path absoluto fuera de `ISOS_DIR` se rechaza → HTTP 400. Aplicado en todos los endpoints de Tab 1 (`/api/disc-probe`, `/api/analyze`, `/api/check-duplicate`, `/api/create-series-sessions`).

`run_full_analysis_for_m2ts(m2ts_path)` ([`phases/phase_a.py`](app/phases/phase_a.py)): variante para m2ts sin BDMV. Diferencias con el flujo MPLS:
- mkvmerge -J sobre el m2ts directo (no MPLS).
- Sin `parse_mpls_chapters` → capítulos auto-generados cada 10 min en el caller.
- PGS counting con rango por defecto `0x1200-0x12FF` (sin lista autoritativa de PIDs del MPLS).
- MediaInfo + dovi_tool operan idéntico sobre el m2ts.

`identify_episode_candidates_from_m2ts_list(paths)`: análogo a `identify_episode_candidates` (que lee MPLS de una carpeta BDMV) pero opera sobre una lista explícita de `.m2ts`. Devuelve el mismo shape (`{mpls_name, mpls_path, duration_minutes, audio_track_count, data}`) — el resto del pipeline downstream no distingue.

### Cascada de duración M2TS con sanity check (v2.7+)

Los m2ts del Blu-ray a veces tienen PCR discontinuo al inicio. `mkvmerge -J` y MediaInfo pueden leer solo el primer chunk y reportar duración trunca — caso real: 50GB reportados como 168s (bitrate implícito 2380 Mbps, físicamente imposible).

Solución en cascada en [`phases/phase_a.py`](app/phases/phase_a.py):

1. **`_duration_looks_implausible(duration_seconds, file_size_bytes)`**: calcula bitrate implícito; True si >200 Mbps (Blu-ray UHD físico tope 113 Mbps con margen).

2. **`_resolve_m2ts_duration_seconds(file_path, mkvmerge_data, mediainfo_result)`**: orquesta la cascada con el sanity check entre cada paso:
   - mkvmerge `playlist_duration` o `duration` (ns) → si pasa el check, devuelve.
   - MediaInfo: probó tracks General → Video → Audio (algunos m2ts no tienen Duration en General).
   - ffprobe con `-probesize 100M -analyzeduration 100M` (default ~5MB era insuficiente).
   - Si todas dan valores sospechosos, devuelve la MÁS ALTA con un WARN al log (mejor un valor aproximado que 0).

3. **`_ffprobe_duration_seconds(file_path)`**: solo se usa sobre ficheros directos. NO dentro de UDF mount — `ffprobe` rompe el demux en m2ts montado por loop (memoria histórica del proyecto).

Aplicado en `run_full_analysis_for_m2ts` y `identify_episode_candidates_from_m2ts_list`. Si la duración inicial era el outlier, se sustituye por la corregida y se loguea el cambio.

### Disc-probe progress endpoint (v2.7+)

Para no dejar al usuario con un modal estático durante los 10-30s del scan, [`main.py`](app/main.py) mantiene un singleton `_disc_probe_progress: {running, current_label, pct, step}` y expone `GET /api/disc-probe/progress`. El frontend pollea cada 400ms y refresca el modal "Detectando contenido" con el paso real y % por candidato escaneado.

Los helpers `identify_episode_candidates(...)` y `identify_episode_candidates_from_m2ts_list(...)` aceptan ahora un `progress_callback(idx, total, item_name)` opcional que se invoca antes de procesar cada MPLS/m2ts. `disc_probe` lo conecta al estado global con etiquetas como `"Analizando candidato 3/20: 00800.mpls"`.

### Endpoints adaptados a los 3 tipos (v2.6+)

| Endpoint | Comportamiento |
|---|---|
| `POST /api/disc-probe` | acepta `source_type` (`iso`/`bdmv_folder`/`m2ts`). Para m2ts con 1 fichero → movie; con N → series sin auto-detect. |
| `POST /api/analyze` | analiza el origen completo. Para bdmv_folder/m2ts no monta. |
| `POST /api/check-duplicate` | huella según tipo (iso → del .iso; bdmv → del m2ts mayor; m2ts → del fichero directo). |
| `POST /api/create-series-sessions` | resuelve cada `ep.mpls_path` según tipo (nombre relativo en iso/bdmv; path absoluto en m2ts). |
| `GET /api/sources` | reemplazo de `/api/isos`. Escaneo recursivo (depth 3) clasificando ISOs + BDMV folders + m2ts sueltos. Cache 60s. |
| `GET /api/isos` | legacy alias, sigue funcionando. Frontend nuevo usa `/api/sources`. |

Campos persistidos en `Session`: `source_type`, `source_path` (default `iso`/`""` para compat legacy).

### Pipeline optimizado — 1 sola copia de datos (v2.7+: 3 source types)

El pipeline de ejecución (Fase D + Fase E en [phases/phase_d.py](app/phases/phase_d.py) y [phases/phase_e.py](app/phases/phase_e.py); orquestación en `_run_pipeline` de [main.py](app/main.py)) soporta los 3 tipos de origen mediante el context manager `Source`:

- **ISO**: `mount -t udf -o ro,loop` automático en `/mnt/bd`. Selecciona MPLS (preferencias en orden):
  1. `session.mpls_path` (modo serie — nombre del MPLS específico).
  2. `session.bdinfo_result.main_mpls` (modo película — detectado en Fase A).
  3. `find_main_mpls(mount_point)` (fallback general).
- **BDMV folder**: no-op de mount. Mismo proceso de selección de MPLS dentro de la carpeta directa.
- **M2TS**: no-op de mount. El "MPLS" para mkvmerge es el propio fichero (`session.mpls_path` absoluto para series, `session.iso_path` para movies con 1 m2ts).

`run_phase_d` y `run_phase_e_direct` aceptan ambos un `source_path` que puede ser un MPLS (iso/bdmv) o un m2ts directo — mkvmerge maneja ambos formatos sin distinción.

**Rutas optimizadas:**
- **Ruta directa** (con reordenación/exclusión de pistas): mkvmerge lee origen → MKV final en `/mnt/output`. Mapeo de pistas por idioma+codec (no posicional).
- **Ruta intermedio** (sin reordenación): Phase D → MKV intermedio → mkvpropedit edita cabeceras (sin recopiar datos) → `mv` al output.

**Strip de fases en el panel de cola** (v2.7+, source-aware):
- Para `iso`: 3 fases visibles — Montar ISO → mkvmerge → Desmontar ISO.
- Para `bdmv_folder` / `m2ts`: las fases mount/unmount se marcan `skipped` (clase `.skipped`: opacity .35 + icono ⊘ + label "Origen directo" / "Cierre del origen"). `updateColaMiniPipeline` ignora las transiciones de estado en fases skipped — el backend sigue emitiendo el evento por compat con el context manager Source, pero el panel ya no se enciende con ese pulso de medio segundo.
- `_configurePhaseStripForSource(sourceType)` (frontend) reconfigura títulos + estado al arrancar un job. Cache `_lastConfiguredSourceType` evita reconfigurar en cada poll si no cambió.

**Logs del pipeline** (audit v2.7+):
- Marker `[Origen]` (sustituye al antiguo `[Montando ISO]`) con texto dinámico: "Montando el ISO…" (iso) · "Origen directo — leyendo la carpeta BDMV" (bdmv) · "Origen directo — leyendo el fichero M2TS" (m2ts).
- Cierre dinámico: "✓ ISO desmontado" / "✓ Origen cerrado (carpeta BDMV)" / "✓ Origen cerrado (fichero M2TS)".
- Frontend (parser del panel cola) detecta los nuevos marcadores con fallback a los antiguos para sesiones legacy con log persistido.

**Otros:**
- **`--gui-mode`** en mkvmerge: fuerza output de progreso (`#GUI#progress XX%` → traducido a `Progress: XX%`).
- **Cancelación**: `_cancel_flags` + `_active_processes` permiten matar el subprocess activo. Limpieza de temporales y cierre del origen en finally (via `Source.__aexit__`).
- **Validación final**: tras crear el MKV, `mkvmerge -J` + `mkvextract` verifican pistas, idiomas, flags y capítulos contra lo esperado. **No comprueba duración** — por eso el fallback al M2TS (abajo) la valida él mismo.

**Fallback al M2TS ante el assertion de playlist de mkvmerge** (Avatar Fuego y Ceniza 2025):
- Algunos discos UHD multi-segmento (seamless branching / multi-ángulo) hacen que **mkvmerge aborte por SIGABRT** al muxear desde el `.mpls`: `Assertion 'file_names.size() == play_items.size()' failed` en `add_filelists_for_playlists`. Ocurre **solo en el mux real, no en `mkvmerge -J`** — por eso Fase A pasa y Fase D/E peta.
- **Dos bugs eran reales**: (1) el check `if proc.returncode >= 2` de Fase D/E **no capturaba el código negativo de la señal** (SIGABRT = -6) → el flujo seguía hasta el `stat()` del output inexistente y reventaba con un críptico `[Errno 2] No such file`. Ahora es `not in (0, 1)` + guard de existencia del output. (2) No había workaround.
- **Fix**: `run_phase_d`/`run_phase_e_direct` detectan la línea del assertion (`is_playlist_assertion_line` en [phase_d.py](app/phases/phase_d.py)) y lanzan `MkvmergePlaylistError`. El orquestador (`_run_pipeline`) la captura, resuelve el **M2TS principal** (`session.bdinfo_result.main_m2ts` → fallback `find_main_m2ts`) y **reintenta UNA vez** con el m2ts directo — el workaround estándar de la comunidad (mkvmerge soporta m2ts nativo).
- **Cada fase limpia SU salida parcial** (`_limpiar_parcial`). Un mkvmerge que aborta a mitad del mux deja el fichero a medias, y el orquestador **no puede** borrarlo: solo conoce la ruta a través de lo que la fase DEVUELVE, y una fase que falla no devuelve nada. Su limpieza (`if intermediate_mkv and ...`) era por tanto **inalcanzable**, así que un `*_intermediate.mkv` a medias —decenas de GB en un UHD— se quedaba en `/mnt/tmp` para siempre (el barrido de huérfanos tampoco cubre ese patrón), y en la ruta directa un `.mkv` parcial se quedaba en **`/mnt/output` con el nombre definitivo**, indistinguible de un rip terminado. Salvaguarda que importa: **solo se borra si el fichero no existía antes** de lanzar mkvmerge — si ya estaba, no es nuestro parcial y puede ser el resultado bueno de una ejecución anterior.
- **Gate de duración** (`m2ts_covers_title`): antes de aceptar el m2ts compara su duración con la del playlist (vía `mkvmerge -J`, que no crashea). Si el m2ts es >2% más corto → `RuntimeError` claro (seamless branching real, usar MakeMKV/dgdemux), **no** se produce un MKV truncado en silencio. Si alguna duración es desconocida (0) se asume que cubre (caso dominante: 1 m2ts = toda la peli). Cubierto por `test_playlist_fallback.py`.

### Mapeo de pistas (Phase E)
- **Por contenido, no por posición**: `_match_tracks_to_source()` busca cada pista incluida en el source por coincidencia de idioma + codec.
- Audio: compara `raw.language` (inglés) con ISO 639-2 del source + subcadenas de codec.
  - **Desambiguación por canales físicos** (`_parse_channels` + `audio_channels` en track_map):
    cuando 2+ pistas comparten idioma + codec (caso típico: Castellano DD 2.0 + Castellano DD 5.1,
    ambas AC-3 spa), el matcher prefiere la que coincide en canales con la pista incluida en lugar
    de coger la primera por orden de source_ids. Sin esto, seleccionar manualmente la DD 5.1 producía
    extracción de la DD 2.0 con el label "DD 5.1" mal puesto encima (bug histórico, arreglado v2.5.4).
  - **`_identify_tracks` excluye los cores AC-3 subordinados, con la MISMA función que Fase A**
    (`_find_subordinate_track_ids`). Cuando un TrueHD y su core AC-3 viajan multiplexados, mkvmerge
    los reporta como dos pistas y lo señala con `properties.multiplexed_tracks`. Fase A quita el core
    al construir las `RawAudioTrack`, así que la "Castellano DD 5.1" que el usuario ve **descartada**
    es la pista INDEPENDIENTE — pero Fase E los veía, y el core suele llevar el **ID menor**, así que
    ganaba el desempate. Recuperar esa pista con `recoverTrack` metía en el MKV el core lossy del
    TrueHD (audio que ya está dentro, duplicado) y la DD 5.1 real no entraba nunca.
    Es la misma clase de divergencia que los subtítulos de tres bloques: **si las dos puntas no parten
    de la misma base, la selección sale bien y el CONTENIDO sale cruzado.**
    Medido sobre los 41 discos reales del NAS (censo del 2026-09-03): 43 ocurrencias en 19 discos donde
    el core compite, **22 en las que gana**, 4 de ellas en castellano (La Momia, Obsession, Scream 7,
    Te van a matar). El camino automático **no** se veía afectado (Fase B incluye una sola pista por
    idioma): 84 decisiones, 0 fallos. Cubierto por `test_golden_discos_reales.py`.
  - **Lo que el disco NO permite desambiguar se AVISA, no se resuelve en silencio.** Varias AC-3
    independientes del mismo idioma y mismos canales (los comentarios del director) son
    indistinguibles: `mkvmerge` devuelve `track_name` vacío en todos los orígenes Blu-ray del corpus.
    Ahí gana el ID menor porque no hay con qué elegir, y `_match_tracks_to_source` lo anota en su
    parámetro opcional `avisos`, que las dos rutas de Fase E emiten al log. 26 casos en 10 discos.
  - **`_codec_matches` es laxo A PROPÓSITO**: los pares se prueban en orden y se cae al genérico, así
    que 'dolby truehd/atmos audio' encaja con 'e-ac-3 atmos' y 'dts-hd master audio' con 'dts'. Para
    hacer daño harían falta dos pistas del mismo idioma en tiers distintos, y sobre las 457 pistas de
    audio del corpus eso ocurre **cero veces**. Endurecerlo pediría una tabla de tiers en paralelo a
    `MKVMERGE_CODEC_TO_BDINFO` — otra réplica que se desincroniza. Si aparece, el aviso lo dirá.
- Subtítulos: por **idioma + identidad estructural forzado/completo**, no por posición ni por `subtitle_type`.
  - `_classify_sub_source_ids` clasifica cada source id como forzado/completo detectando el patrón del disco (bloques vs adyacentes). Cada pista incluida se empareja con un source id de la categoría correcta (forced↔forced, complete↔complete), con fallback a la otra categoría y luego a cualquier id del idioma. Consume IDs 1:1 (used_ids).
  - **La categoría de la pista incluida se decide por `raw.bitrate_kbps < 3.0` (señal sintética estructural de phase_a), NO por `track.subtitle_type`.** `subtitle_type` lo fija la clasificación por paquetes de phase_b y puede venir equivocado (un forzado que el muestreo PGS perdió → 0 paq. → marcado `complete`; o una pista recuperada a mano) — y **renombrar la etiqueta no cambia `subtitle_type`**. Al decidir por `subtitle_type` el matcher entregaba el contenido **CRUZADO**: la pista "Castellano Forzados" recibía el PGS completo y viceversa (caso real Obsession 2025 — dos castellanos ambos con `subtitle_type=complete`, consumidos en orden). `raw.bitrate_kbps` usa la MISMA base estructural que `_classify_sub_source_ids`, así que ambos extremos quedan alineados. Cubierto por `test_track_mapping.py::test_obsession_forced_subtitle_type_wrong_still_maps_by_structure`.
- Tabla de matching: `_ISO639` (ISO → nombre inglés), `_codec_matches()` (BDInfo → mkvmerge).
  - **`_ISO639` se DERIVA de `phase_a.ISO639_TO_ENGLISH`** (`{code: name.lower()}`), NO es un subset propio. Antes era un subset manual de ~24 idiomas y se desincronizó: una pista incluida llega con `raw.language` en inglés (`"Catalan"`) y el source la identifica por código ISO (`"cat"`); si el código no estaba en el subset, `_ISO639.get("cat","cat")="cat" ≠ "catalan"` → el matcher no la encontraba y la pista se **perdía en silencio** (catalán, tailandés, griego…). Bug pre-existente independiente de la ruta (mismo matcher para MPLS y M2TS); aflora cuando el job completa. Caso real: Avatar Fuego y Ceniza (catalán DTS + sub forzado). Cubierto por `test_track_mapping.py::TestLanguageMapCoverage`.
  - **`_validate_final_mkv` compara también el TIER del codec**, no solo idioma y flags. Una pista llamada "Castellano TrueHD Atmos 7.1" con un AC-3 dentro pasaba en verde sin una línea en el log — y eso es exactamente lo que producen los dos bugs que ha tenido el matcher (el P0 posicional y el core AC-3 subordinado). El tier real sale de `codec_key_de_mkvmerge(codec, canales)` (en `phase_b`), que traduce el codec de mkvmerge a nomenclatura BDInfo con la tabla de `phase_a` y delega en `_codec_key`: **una sola definición de tier en el repo**, la misma que ordena la selección por calidad. Replica la heurística de Atmos para DD+ (E-AC-3 con ≥8 canales) porque mkvmerge no lo distingue en el nombre. Un tier `unknown` (PCM, FLAC…) **nunca** marca el job como fallido — sería el falso ❌ del catalán otra vez. Los nombres que se le enseñan al usuario salen de `CODEC_TIER_NAMES`, compartido con `_codec_literal`. Cubierto por `test_validacion_final.py::TestElTierDelCodec`.
  - **Mismo patrón en `_validate_final_mkv`** (main.py): su `_iso` local era otro subset incompleto y daba un **falso ❌** ("esperado: catalan, real: cat") aunque la pista estuviera bien extraída — también deriva ahora de `ISO639_TO_ENGLISH`. Las discrepancias de idioma inline ahora se añaden a `warnings` (antes marcaban `all_ok=False` pero el resumen decía "0 discrepancias"). **Regla: cualquier normalización ISO 639 → idioma parte de `ISO639_TO_ENGLISH`, nunca de un subset local.**

### Persistencia
- Cada sesión es un fichero JSON en `/config/{session_id}.json`
- ID de sesión: `{titulo}_{año}_{timestamp_unix}`
- **`iso_fingerprint`**: SHA-256 del primer 1 MB + tamaño. Permite detectar duplicados por contenido.
- Estados: `pending` → `queued` → `running` → `done` | `error`
- **No se persiste si Fase A falla**: la sesión solo se crea tras éxito de A+B.
- **Timestamps UTC-aware**: `datetime.now(timezone.utc)` siempre.
- **Cola**: `queue_state.json`. Al arrancar, sesiones zombie → `pending`.

### Docker
- **Base**: `ubuntu:22.04` + MKVToolNix (v81+ oficial) + mediainfo + ffmpeg + dovi_tool 2.3.3 + Python 3.10
- **`privileged: true`**: para loop mount
- **Puerto**: 8090 → 8080 (configurable)
- **Healthcheck**: `GET /api/health` cada 30s
- **Recuperación**: sesiones zombie → `pending` al arrancar

### Desarrollo local
- Python 3.12 en `.venv/`
- `./run_local.sh` — carga `.env.local`, crea directorios, lanza uvicorn con `--reload`
- `DEV_MODE=1` activa fixtures fake

### Tests
- Suite `unittest` en `app/tests/` (sin pytest ni config). Ejecutar **desde la raíz del repo**:
  ```bash
  python3 -m unittest discover -s app/tests -v        # toda la suite
  python3 -m unittest app.tests.test_rpu_analyze -v   # un módulo
  ```
- Cubren motor de reglas/series, match TMDb TV, clasificación L8 RPU, cache + quality audit de MKV, la abstracción Source y **las fases C/F/G/H del pipeline CMv4.0 ejecutadas de verdad** (via `cmv40_harness.py`). No requieren discos reales ni los binarios instalados.
- **El pipeline de Tab 1 se ejecuta en tests** (`test_fases_de_tab1`, `test_orquestador_tab1`, `test_validacion_final`): las fases D y E con el argv real que recibe mkvmerge, `_run_pipeline` con las dos rutas y el reintento con el M2TS —que reintenta, que reintenta **una sola vez** y que con origen m2ts no vuelve a entrar—, y `_validate_final_mkv` con su tabla ISO 639. Se usa un origen `bdmv_folder`, que en `Source` es un no-op: ejercita el orquestador entero sin montar nada.
- **Los endpoints de Tab 1 y Tab 2 se prueban por HTTP** (`test_endpoints_tab1_tab2`, sobre `api_harness`): path traversal del file browser, el whitelist del borrado, que `PUT /api/sessions/{id}` no acepte `has_fel`/`audio_dcp`, que los secretos no salgan en crudo. Los que salen a la red (TMDb, Google, GitHub) quedan fuera a propósito.
  - Al ampliar el arnés a Tab 1/2 hubo que redirigir dos cosas no obvias: **`services.settings_store` resuelve su `CONFIG_DIR` en el import**, así que un test de `/api/settings` escribía en el `/config` real; y **`queue_manager.enqueue` dispara el pipeline** (`create_task(_process())`), así que un test de `POST /execute` lanzaba mkvmerge de verdad y filtraba el estado de la cola al test siguiente.
- **`test_golden_discos_reales.py` corre el mapeo de Fase E contra los 41 discos reales del NAS.**
  Las fixtures de `tests/fixtures_discos/` son el recorte de las sesiones de `/config` (3,4 MB → 350 KB):
  las pistas del `mkvmerge -J` del origen más los `raw` de incluidas y descartadas. Nada inventado — si
  un test de ahí falla, hay un disco de verdad que se procesaría distinto. Cubre dos escenarios y la
  diferencia entre ellos es el motivo del fichero: **incluidas solas** (lo que ya corrió en el NAS; ese
  mapeo no puede cambiar nunca) e **incluidas + todas las descartadas** (simula `recoverTrack`, la única
  vía por la que entra una segunda pista del mismo idioma — y donde el core competía).
  `golden_mapeo_discos.json` guarda el codec y los canales de la pista elegida, no solo su id, para que
  el diff se lea. Si el golden falta, el test **se salta** en vez de fallar: en CI eso se ve porque el
  resumen deja de decir `OK` a secas.
- **El `mkvmerge` falso emite `id` explícito y `multiplexed_tracks`.** Los ids de un origen Blu-ray no
  empiezan en 0 ni son contiguos, y sin `multiplexed_tracks` el fake describía un disco **en el que los
  cores AC-3 no existen** — por eso ningún test podía ver que Fase E los tomaba por pistas
  independientes. Otro caso de la regla de fakes fieles, como el `MaxCLL` con unidad de mediainfo.
- Un test de una fase **ejecuta la fase**. Los `assertIn` sobre el fuente del pipeline no cuentan como cobertura: pasan en verde aunque el comportamiento esté invertido. Al añadir un test de comportamiento, verificar que falla reintroduciendo el bug.

### CI — `.github/workflows/tests.yml`

La suite corre en **push a main, PR y `workflow_dispatch`**. Hasta entonces el único workflow del repo publicaba la imagen en `release.published`, así que se podía tagear en rojo sin que nada avisara.

**Ningún entorno local ejecuta la suite entera.** Medido sobre los mismos 761 tests:

| entorno | se salta | por qué |
|---|---|---|
| Mac (`.venv`) | **2** | `_ReadProgress` mira `/proc/<pid>/io` (`rchar`) y `/proc/<pid>/fdinfo` (`pos:`), que en macOS no existen |
| Contenedor del NAS | **25** | la imagen no lleva `node` ni Chrome, y no debe llevarlos — son 4 módulos de frontend |
| **CI (`ubuntu-latest` + node 20)** | **0** | tiene `/proc`, `node` y Chrome preinstalado |

Los dos de `_ReadProgress` cubren la medición de progreso, la maquinaria que este repo documenta como la más frágil, y hasta que hubo CI **no se ejecutaban en ninguna parte**: en el Mac se saltaban y a nadie se le ocurría pasar la suite dentro del contenedor por eso. La señal de que corren es que el resumen diga `OK` a secas y no `OK (skipped=N)`. El job comprueba `/proc` **antes** de la suite, para que si el runner cambiara no volvieran a saltarse en silencio.

Detalles que no son accidentales:
- **Python 3.10**, la misma minor que el contenedor (`ubuntu:22.04`). En el Mac la suite corre sobre 3.12; fijar la del contenedor evita descubrir una incompatibilidad en producción.
- **Node 20**, porque cuatro tests (`test_cmv40_plan_frontend`, `test_cmv40_overlay_bloqueante`, `test_cmv40_timeline_layout`, `test_cmv40_eta_sufijo`) evalúan las funciones reales de `app.js` en node y sin él se auto-saltan.
- Wall time: **~56 s en CI** contra ~175 s en el Mac (los binarios falsos son scripts de Python y la suite paga un arranque de intérprete por cada llamada simulada).

`test_frontend_cache_bust.py` va en la **suite**, no en el YAML: caza el error típico del token `?v=` de `index.html` —tocar una de las dos referencias y olvidar la otra, que deja el CSS y el JS en versiones distintas— y así también salta en local, que es donde se comete.

---

## El frontend son siete scripts, y clásicos a propósito

`app.js` eran **18.687 líneas** con 475 funciones: no había forma de tocar una parte sin cargar el todo. Hoy son siete ficheros que `index.html` carga en orden, con el mismo reparto por temas que el backend:

| fichero | líneas | qué tiene |
|---|---|---|
| `core.js` | 1.084 | estado global, el `DOMContentLoaded`, tooltips, tabs y sub-tabs, toasts, confirm, helpers de modal |
| `settings.js` | 656 | ⚙︎ Configuración, el pill de versión, el panel de Limpieza |
| `cmv40_modals.js` | 1.991 | consulta rápida, manual CMv4.0, limpieza masiva (lo de Tab 3 que no toca una sesión) |
| `tab1.js` | 5.342 | nuevo proyecto, modo serie, sidebar, render de sesión, cola, consola, WS |
| `tab2.js` | 2.936 | editar MKV + radiografía DV+HDR |
| `tab3.js` | 6.385 | proyectos CMv4.0, cards por fase, overlay, gráfico de sync |
| `browser.js` | 289 | el file browser modal, que comparten Tab 2 y Tab 3 |

**Son `<script>` clásicos, NO `type="module"`, y es una decisión medida**: **154 nombres de función** se referencian desde atributos inline (`onclick="openProject(…)"`) en `index.html` y en las plantillas que el propio JS emite con `innerHTML`. En un script clásico las declaraciones `function` van a `globalThis` y siguen funcionando sin tocar nada; con módulos quedan encerradas en el ámbito del módulo y haría falta un bloque de 154 `window.foo = foo` **mantenido a mano** — cada botón nuevo moriría en silencio hasta que alguien lo pulsara. No hay build step en este proyecto, así que nadie generaría ese bloque.

Concatenar scripts clásicos en orden es equivalente al fichero único **con tres salvedades**, y las tres tienen test en `test_frontend_troceado.py`:

1. **`'use strict'` es por script.** El original lo tenía en la línea 1; una pieza sin él pasa a modo sloppy —los globals accidentales dejan de lanzar, `this` cambia— sin dar un solo error.
2. **El hoisting es por script.** Una sentencia top-level que use algo declarado en una pieza POSTERIOR ve `undefined` donde antes veía la función. Hay cuatro sentencias top-level en total (el `DOMContentLoaded`, un `keydown`, un `setInterval` y el `<style>` del spinner) y el test comprueba que ninguna cruza hacia adelante.
3. **Una declaración duplicada** que antes se sombreaba en silencio puede caer ahora en piezas distintas. Había una: `_doFilterSidebarSessions` estaba declarada **dos veces** y la primera —sin filtro de estado ni ordenación— era código muerto que la segunda tapaba. Se quitó.

**El orden de carga y los nombres salen de `index.html`, no de una lista en el test.** `tests/frontend_sources.py` lo parsea y ofrece `js_completo()`; los siete módulos de test que antes leían `app.js` van por ahí. Una lista hardcodeada se desincronizaría del HTML en el primer cambio y el test seguiría en verde midiendo otra cosa.

**El test que de verdad protege el corte abre `index.html` en Chrome headless**: carga las siete piezas con su orden y su `?v=` real, y comprueba que no hay **ni un error de JS** y que los 154 handlers son funciones en `window`. Es el único sitio donde un fallo de carga (SyntaxError, TDZ, referencia adelantada) se ve; los demás tests leen fuente y no lo verían.

**Si `tab3.js` sigue creciendo, la siguiente costura ya está marcada**: el gráfico de sincronización de la Fase D (el Canvas custom con su zoom) son 590 líneas autocontenidas al final del fichero. No se ha sacado ahora porque bajar de 6.393 a 5.802 no cambia nada práctico y sí añade un fichero.

**Cache-bust: el token es uno para los ocho assets** (siete JS + el CSS). Con el token subido en seis de siete, el navegador sirve una pieza vieja contra seis nuevas — peor que no tenerlo, porque el desajuste es parcial. Lo comprueba `test_frontend_cache_bust`.

## Reglas de UX / Diseño visual

### Estilo general — macOS moderno
- Paleta **clara** (tema macOS light: `--bg: #f5f5f7`, texto oscuro), con **islas oscuras** puntuales (el hub navy superior, la consola del pipeline, ciertos modales). Acentos azul/teal, radios generosos, transiciones fluidas
- Variables CSS centralizadas en `:root`

### Hub activo — franja superior
Todas las pestañas comparten una **franja navy** en la parte superior que unifica visualmente la "zona de acción" (tabs principales + botón primary de la herramienta).

- Variable principal `--active-hub: #22436c` — es **la interpolación lineal exacta** entre `--active-hub-top` (`#264a78`) y `--active-hub-bottom` (`#1a3557`) al 35.7% (proporción tab height / total hub height). Este valor NO debe cambiarse arbitrariamente: si se toca, hay que recalcular para mantener la continuidad de slope entre los dos tramos de gradiente.
- **Dos tramos continuos** (hackeado como un único gradiente virtual):
  - `.tab.active` usa `--active-hub-tab-grad` = `top → --active-hub`
  - `#subtab-bar`, `#cmv40-subtab-bar`, `.sidebar-new-project-area`, `#mkv-action-bar` usan `--active-hub-bar-grad` = `--active-hub → bottom`
- **Altura hub**: `min-height: 63px` en los bars + 14+36+12+1 en `.sidebar-new-project-area`. Este 63px se referencia en varias reglas — mantenerlo sincronizado.
- **Hairline entre tab-bar y subtab-bar**: resuelto con `border-bottom: 1px solid var(--active-hub)` en `#tab-bar` (se funde con el top del subtab-bar de Tab 1/3; en Tab 2 queda como acento fino).
- **Botón primary en franja navy**: `.sidebar-new-project-btn` lleva `box-shadow: 0 1px 6px rgba(0,0,0,0.25), 0 0 0 1px rgba(255,255,255,0.08) inset` para destacar sobre el azul oscuro. Tab 2 reutiliza literalmente esa clase (no crea `.mkv-action-btn` duplicada).

### Switching de tabs principales
`switchTab(n)` debe marcar el active **por ID** (`tab-btn-N`), nunca por posición DOM — el orden visual no coincide con la numeración interna.

### Scroll horizontal de sub-tabs de proyecto
Tab 1 y Tab 3 comparten el patrón. Arquitectura:
- UN SOLO scroll container: `.subtab-projects` con `overflow-x: auto`.
- Scrollbar nativa oculta (`scrollbar-width: none` + `::-webkit-scrollbar { display: none }`).
- Chevrones circulares blanco/azul (`.subtab-scroll-btn`) dentro de `.subtab-projects-area`, visibles solo cuando hay overflow (JS añade `.has-overflow` en el area).
- Wheel vertical → scroll horizontal sobre la franja (handler en `_installSubtabScrollBindings`).
- Los helpers `_updateSubtabScrollState()`, `_scrollSubtabContainer()` y la config `_SUBTAB_SCROLLERS` sirven a ambos tabs por DRY.

### Indicadores de ejecución
- **Spinner inline** (`.spinner-inline`): aparece en tab "Blu-Ray ISO → MKV", subtab del proyecto, sidebar del proyecto y sección "En curso" cuando hay job activo
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
- No hardcodear rutas de directorio en el backend. Lo que se puede borrar sale de **`_cleanup_targets()`** (`main.py`), con `bases` + `patron` por tipo de basura: la consumen el barrido de arranque, el panel de Limpieza y el whitelist del borrado. Estaba escrito por triplicado y había divergido — el panel se quedó mirando `/tmp` cuando los workdirs de Tab 2 ya se creaban en `TMP_DIR`, así que un huérfano de hasta ~90 GB era invisible, y el whitelist tampoco habría aceptado la ruta real. **La validación resuelve la ruta antes de comparar** (`_cleanup_path_allowed`): `path_str.startswith(prefix)` sobre la cadena cruda aceptaba `/mnt/tmp/cmv40/../../library` y llegaba a `rmtree`, con `/mnt/output`, `/mnt/tmp` y `/config` montados `rw` y el contenedor en privileged. Solo se admite el **primer nivel** bajo la base: ni el propio root ni un fichero de dentro de un workdir. Regla: **cualquier validación de ruta parte de `resolve()` + `relative_to()`**, como `_safe_library_path` y `_resolve_mkv_path_safe`
- No añadir abstracción para un solo uso
- **No cambiar `--active-hub` sin recalcular la interpolación** — romperá la continuidad del gradiente del hub
- **No usar `idx + 1` sobre los `.tab` principales** para activar tabs — usar IDs (`tab-btn-N`) porque el orden visual no coincide con la numeración interna

### Textos de log y UX — patrón "describir estado, no predecir futuro"

Regla establecida tras varias rondas de audit (v2.4.1) — los textos del
log de la consola CMv4.0, el sidebar de la timeline, el modal de creación
y las cards del panel del proyecto deben describir **el estado actual**
o **el artefacto generado**, NO el comportamiento futuro de una fase
posterior.

Bugs históricos eliminados que ilustran el patrón:
- Fase A cerraba con "El RPU source queda guardado para los trust gates de Fase B" → si el usuario cancela, promesa colgando.
- Fase F cerraba con "Fase G puede saltarse el dovi_tool mux…" → si el usuario aborta, predicción incumplida.
- Pre-flight emitía "Procediendo con extracción del BD (Fase A)" antes del análisis L2/L8.
- Tooltip auto-pipeline decía "Pausa obligatoria en Fase D" siempre, pero D se omite si `target_trust_ok=True`.

Reglas concretas:
1. Cierres de fase (`🎯 Resultado: …`) describen el **artefacto producido**, no qué hará la siguiente fase. Cada fase emite su propio `📋 Plan` al arrancar.
2. Anuncios prematuros — nunca decir "procediendo con Fase X" hasta que el dispatcher realmente la dispare. Si `auto_pipeline=False`, indicarlo.
3. Textos dinámicos según `workflow` + `target_type` + `target_trust_ok` cuando el comportamiento del backend depende de esa combinación. Patrón "5 ramas if/elif" para Fase F y Fase G (drop-in / p7_fel merge / p7_mel merge / p7_mel direct / p8 merge / p8 direct).
4. Markers de persistencia (`━━━`, `✓ Fase`, `✗ Fase`, `🎯 Resultado`, `📋 Plan`, `🛑 Cancelado`, `ℹ️ Auto`, `ℹ️ Forward`) son tokens de matching para `_CMV40_LOG_FORCE_PERSIST_MARKERS` (en `main.py`, buscar por símbolo). Sus prefijos no se pueden cambiar; el texto detrás sí.
5. Tokens del frontend (`§§PROGRESS§§`, `$ ` prefix, `--gui-mode` mkvmerge → "Progress: XX%") son contratos del parser — intocables.
6. `_emit_progress(pct, label)` debe ser **monotónico** dentro de una fase. Si una operación intermedia consume tiempo sin reportar progreso (típico de `_run` sync vs `_run_streaming`), reservar un % fijo (ej. Fase F merge → 15%, inject → 85%).
7. El `_emit_progress(100%, "X completado")` debe emitirse **después** del `🎯 Resultado` final, no antes (evita que la barra llegue al 100% mientras quedan logs útiles).

Las funciones afectadas (en `app/static/tab3.js`, salvo indicación):
- `_cmv40BuildPhaseSteps` (sidebar timeline)
- `_renderCMv40Info`, `_renderCMv40RecommendationCard` (cabecera + card recomendación)
- `_cmv40FaseCBody`, `_cmv40FaseDBody`, `_cmv40FaseFBody`, `_cmv40FaseGBody`, `_cmv40FaseHBody` (bodys de fase activa)
- `_cmv40NewUpdatePipelinePreview` (modal de creación)

Y en `app/phases/cmv40_pipeline.py`:
- `run_phase_a_analyze_source`, `run_phase_b_target_from_*`, `run_phase_c_extract`, `run_phase_e_correct_sync`, `run_phase_f_inject`, `run_phase_g_remux`, `run_phase_h_validate` — cada una emite `📋 Plan` al arrancar y `🎯 Resultado` al terminar (excepto Fase E que añade resumen de ops aplicadas).
- `_preflight_validate_bin` + `_cmv40_preflight_analyze_target` (en `main.py`, buscar por símbolo) — emiten clasificación L8 + tier de calidad + recomendación al cierre del pre-flight.

---

## File browser unificado multi-root

Tab 2 ("Abrir MKV") y Tab 3 ("MKV origen del proyecto CMv4.0") usan el mismo browser modal, pero con roots distintos según el caso de uso:

| Tab | Roots expuestos | Por qué |
|---|---|---|
| Tab 2 (Editar MKV) | 📚 Biblioteca (`/mnt/library`) + 📦 Output (`/mnt/output`) | Inspecciona MKVs ya consolidados o el output del propio converter |
| Tab 3 (CMv4.0) | 📚 Biblioteca (`/mnt/library`) + 📥 Downloaded (`/mnt/isos`) | El MKV origen suele venir directo de la carpeta de descargas (mismo directorio que las ISOs); no tiene sentido procesar nuestro propio output como source CMv4.0 |

- Endpoint único `GET /api/library/browse?root={library|output|downloaded}&path=...`. Los 3 roots viven en `LIBRARY_ROOTS` (backend) — qué subset expone cada tab lo decide el frontend en su llamada a `openFileBrowser({ roots: [...] })`.
- Modal con breadcrumb navegable, búsqueda incremental, root pills cuando hay 2+, selección por click + botón "Seleccionar" o doble-click.
- z-index 220 para overlay encima de otros modales (wizard CMv4.0 = 200).
- Validación path-traversal (`_safe_library_path`, `_resolve_mkv_path_safe`) — el frontend manda ruta absoluta, backend valida que cae bajo un root permitido.

## Pre-flight bloqueante del bin CMv4.0

Antes de gastar Fase A (~12 min de extracción HEVC en discos UHD), un pre-flight rápido valida que el bin target tenga CMv4.0. Si no, aborta con mensaje claro sin tocar el HEVC.

- Endpoint `POST /api/cmv40/{id}/preflight-target` (asíncrono, devuelve `{started:true}` inmediato).
- Background task setea `running_phase="preflight"` durante toda la operación → bloquea el auto-pipeline (otros endpoints respetan el lock por session_id).
- Tres kinds: `drive` (descarga del repo DoviTools), `path` (copia de carpeta local), `mkv` (extrae RPU de otro MKV).
- Si el bin no aporta CMv4.0 (caso típico: bins "P5 to P8 transfer") → setea `error_message` con mensaje legible y `target_preflight_ok=False`. Frontend lo ve via polling y detiene el auto-pipeline.
- Si pasa → `target_preflight_ok=True`, el bin queda en `RPU_target.bin` del workdir y Fase B reutiliza sin re-descargar (helper `_bin_already_cached`).
- Errores van al log de la sesión via WS (igual que cualquier fase) — sin toast prematuro.

## Modelo Mantener / Inyectar — clasificación L8 + tiers de calidad

Tras descargar el bin, el pre-flight ejecuta `dovi_tool export -d all` y analiza la riqueza real del L8 en `phases/rpu_analyze.py`. La clasificación L8 alimenta una decisión de 4 caminos visible al usuario en la card "Análisis y recomendación" del panel del proyecto.

### Clasificación L8 (`classify_l8`)

Tres categorías:

- **`real`** — L8 trabajado por colorista. Restore aporta calidad. Dos sub-ramas:
  - **CORE/CORE+/FULL estándar**: `combos_únicos >= 10` Y `frames_neutros < 95%`.
  - **FULL minimal**: `combos_únicos >= 3` Y (`mid_contrast` OR `clip_trim` poblados) Y al menos un combo con delta>50 unidades del neutro en algún trim (slope/offset/power/saturation_gain). Detecta masters con look global uniforme + brackets de toning sutiles (Black Phone 2: 3 combos slope+117 mid_c=2121; Expediente Warren: 5 combos slope=-276 sat=-410 clip=1901). Sin esta rama caerían en "indeterminate".
- **`default`** — Bin sintético: ≤2 combos únicos, O ≥95% frames neutros **con <10 combos**. Restore == conversión al vuelo del p3i/avdvplus → recomendación: Mantener MKV actual. Un % neutro alto **con muchos combos** (≥10) es típico de un master CORE real de peli oscura (escenas oscuras → trims a neutro), así que cae a `indeterminate`, no a `default`, para no descartar un bin válido. `≤2 combos` sigue siendo disparador independiente (sintético aunque el combo sea no-neutro).
- **`indeterminate`** — Caso límite (incluye master con muchos combos pero mayoría neutros), avanzar y decidir tras Fase A.

### Tiers de calidad (`classify_l8_quality`)

Solo aplica cuando la clasificación L8 es "real":

| Tier | Filename label | Criterio |
|---|---|---|
| `full` | `[CMv4 FULL]` | `target_mid_contrast` o `clip_trim` poblados **con valor no neutro (≠2048)** — un campo presente pero a 2048 no es trabajo del colorista y no cuenta. Sub-descripción "FULL minimal" cuando combos<10 (master uniforme con brackets) |
| `core_rich` | `[CMv4 CORE+]` | `combos/scene_cuts >= 0.1` o `>= 400 absolutos` (grading dinámico shot-a-shot intenso) |
| `core` | `[CMv4 CORE]` | Estándar streaming (Apple TV+/Disney+/Netflix) — trims básicos sin campos CMv4.0-only |

El label se inyecta al `output_mkv_name` al terminar el pre-flight via `_cmv40_apply_quality_label_to_output_name`.

### Decisión de 4 caminos (`recommend_action`)

Árbol de decisión:

```
¿target_preflight_ok=True Y l8="real"?
├─ NO  → Mantener MKV actual (keep)
└─ SÍ  → ¿Profile source ↔ bin match (FEL↔FEL / MEL↔MEL / P8↔P8)?
         ├─ NO  → Inyectar RPU CMv4.0 (preserva L2)  ← merge selectivo [3,8,9,11,254]
         └─ SÍ  → ¿L2 del source == L2 del bin?
                  ├─ SÍ → Inyectar RPU CMv4.0 (rápido)       ← drop-in, ~30s
                  └─ NO → Inyectar RPU CMv4.0 (preserva L2)  ← merge selectivo
```

Endpoints relacionados:
- `POST /api/cmv40/{id}/accept-keep` — cierra el proyecto como `done` con `output_workflow="keep_cmv29"`.
- `POST /api/cmv40/{id}/override-recommendation` — fuerza la inyección aunque la recomendación sea Mantener.

`output_workflow` final: `keep_cmv29` | `restore_dropin` | `restore_merge`.

### Auditoría retroactiva (`tools.audit_cmv40_bins`)

Script CLI standalone que re-clasifica TODAS las sesiones CMv4.0 históricas con el modelo actual y reporta jobs desperdiciados + renombrados sugeridos. Útil para validar calibraciones nuevas del classifier.

Modos:
- Default: análisis completo + tabla + estadísticas + lista de renombrados sugeridos.
- `--filter-keep`: solo muestra jobs que el modelo retroactivo dice deberían haber sido Mantener (procesados pero el bin era sintético).
- `--redownload`: re-descarga el bin del Drive / re-extrae del MKV origen si no queda en workdir. Necesario para auditar sesiones antiguas con workdir limpiado.
- `--detail <substring>`: short-circuit — salta el análisis general y vuelca todos los combos L8 únicos del bin con sus deltas vs neutro. Discrimina "synthetic con jitter" (todos los deltas ≤50) vs "real minimal" (al menos un combo con delta>50). Usado para confirmar manualmente los casos "indeterminate" antes de refinar los umbrales del classifier.

Las re-clasificaciones desde stats persistidas reusan los `target_l8_combos` guardados en la session JSON para que la rama "real minimal" (que mira deltas por combo) funcione retroactivamente sin tocar el bin original.

## Análisis extendido del RPU (Tab 2) — UN botón, dos análisis

**Un solo botón "🔬 Análisis extendido"** produce los combos L8/L2 clasificados **y** el perfil de luminancia L1, con **una sola extracción del RPU**. Antes eran dos botones, dos jobs singleton, dos modales y dos extracciones.

Se separaron porque cada uno era caro. Dejó de tener sentido cuando el pipe unió ffmpeg y `extract-rpu`: medido, extraer el RPU del MKV son **~650 s** y el export por niveles **~7 s**, o sea que la extracción es el **~97 %** del análisis. Mantenerlos aparte solo servía para pagar dos veces el 97 % y compartir el 3 %.

Y había una asimetría peor que la repetición: la auditoría de calidad **se cacheaba** (bloque `quality` de `/config/mkv_audits/{fingerprint}.json`; en el NAS, 23 MKVs y 396 KB) y el perfil de luminancia **no se persistía en ninguna parte** — solo vivía en el state del job. La calidad costaba 10 min una vez; la luminancia, 10 min **cada vez que se miraba**.

`analyze_rpu_quality_for_mkv(con_luminancia=True)` hace los dos y devuelve el perfil bajo `light_profile`. Puntos que no son obvios:

- **El export es uno.** Los niveles se solapaban —calidad L1/L2/L8, luminancia L1/L2/L5/L6/L8, o sea que los de calidad son un SUBCONJUNTO— así que `_run_export_levels` acepta `niveles_extra` y se piden L5/L6 en la misma pasada. Sin el flag, el camino de solo calidad no paga los niveles que no usa.
- **`DoviInfo.light_profile` tiene que existir en el modelo.** La re-inyección del cache filtra por `hasattr(result.dovi, k)`: sin el campo, el perfil se descarta **en silencio**. Verificado por mutación.
- **La re-inyección solo ocurre si hay bloque `basic` cacheado** (`if cached and cached.get("basic")`), que es el orden real de uso: el usuario abre el MKV y después lanza el análisis.
- **El render lee campos planos.** `_mkvAplicarPerfilLuminancia` mapea `dovi.light_profile` → `per_scene_max_cll` / `l1_stats` / `l1_references` en UN sitio, en vez de tocar las diez lecturas del render.
- La reserva para dovi_tool < 2.3.3 (volcado completo) da los combos pero **no** L5/L6: el perfil se omite y se dice en el log en vez de fallar.

`phases/luminance.py` contiene el análisis: `pq_code_to_nits`, `niveles_desde_volcado`, `perfil_desde_niveles` y `payload_de_luminancia`. Estaba **inline** en el endpoint — 668 líneas y complejidad ciclomática 170, la peor función del repositorio, y encima un route handler.

**Hueco de cobertura que salió al escribir los tests**: `_run_dovi_on_mkv` escribe su RPU de muestreo en `TMP_DIR`, que por defecto es `/mnt/tmp` y en el Mac no existe. El sniff falla en silencio (`except: no bloquea`) y `result.dovi` queda en `None`, así que **ningún test había poblado nunca un `DoviInfo`** — y por eso el filtro `hasattr` nunca se había ejercitado. Un test de cache de MKV que dependa de `result.dovi` tiene que parchear `mkv_analyze.TMP_DIR`.

### El perfil L1 en sí

- Extrae L1 (max_pq, avg_pq, min_pq) por frame del RPU del MKV completo.
- Pipeline: **`ffmpeg | dovi_tool extract-rpu` en una sola pasada** → `dovi_tool export` → parseo.

**El HEVC intermedio no se escribe.** Se reutiliza `_ffmpeg_extract_rpu_piped(..., hevc_out=None)`, el mismo helper de la Fase A. Antes eran dos pasadas en serie, con dos costes medidos: escribir y volver a leer ~75 % del tamaño del MKV (~45 GB en un UHD) sobre el mismo pool por el que ffmpeg lee el MKV — y aquí ese HEVC **no se usa para nada más**, tanto que el código lo borraba tres líneas después—; y que los dos pasos se estorban (ffmpeg 574 s limitado por DISCO con la CPU ociosa, `extract-rpu` 372 s limitado por CPU con el disco medio ocioso, ~946 s usando media máquina cada vez).

Lo que hubo que adaptar, y por qué:
- **El progreso.** El helper emite `§§PROGRESS§§` (contrato del frontend de Tab 3) con el pct ya escalado; un adaptador lo traduce al estado que pollea el modal de Tab 2. El reparto pasa de 55/35/10 a **90/10** (`PESO_EXTRACCION`): por el pipe la extracción es un solo tramo continuo.
- **La tira de pasos del modal tiene DOS filas.** Extraer el HEVC y extraer el RPU son el mismo trabajo cuando van conectados. El backend conserva los ids internos 1/2/3 porque el camino de reserva sí los hace en serie, y `_dvLightSetStep` mapea id → fila (`_DV_LIGHT_PASO_A_FILA`).
- **La cancelación mata TODOS los procesos vivos.** `_lp_active_proc` guardaba uno; con el pipe hay dos y matar uno deja al otro colgado en el otro extremo.
- **El pre-flight de espacio solo corre en la reserva.** Exigía ~45 GB libres para un HEVC que ya no se escribe: habría rechazado análisis que caben de sobra.
- **La duración del MKV** se resuelve con un `ffprobe` best-effort (~1-3 s): es la referencia con la que ffmpeg traduce su `time=` a porcentaje. Sin ella el tramo de ffmpeg dependería solo de `/proc`.

**El camino de reserva (dos pasadas) se conserva y se ejercita en el test**, porque el helper devuelve False *sin lanzar* ante cualquier problema y esa rama tiene que seguir funcionando el día que haga falta.

`test_mkv_quality_pipeline.py` **ejecuta el endpoint** con los binarios falsos. Una de las mutaciones destapó un test flojo que conviene recordar: *"no queda ningún `.hevc` en disco"* pasaba **también** con las dos pasadas, porque la reserva lo borra al terminar. La aserción útil es sobre el **argv** — que a ffmpeg no se le pida escribir ningún HEVC.
- **Un solo consumidor del export**: `_perfil_desde_niveles` come el formato PLANO de `rpu_analyze.export_levels` (L1 → las tres series, L5 → zonas de active area, L8 → max_pq por target, L2 → trim targets, L6 → mastering display). El camino legacy (volcado completo, para dovi_tool anterior a 2.3.3) se **adapta** a plano con `_niveles_desde_volcado` en vez de parsearse aparte.
  - Antes había un segundo parser aquí dentro —tres formatos, paths conocidos y fallback recursivo— más `_rpus_from_levels`, que **reconstruía la forma anidada** desde el export por niveles solo para alimentarlo. Es la duplicación que la regla de "un solo parser de ese JSON" prohíbe desde `305689a`: dos parsers permitieron que uno divergiera del formato real, y el que funcionaba era el que tenía tests. Medido con 243.552 frames: reconstruir + navegar eran 3,50 s y un pico de **450 MB** de RAM (con el recorrido en el event loop); ahora 1,47 s y 26 MB, todo en un thread.
  - El sanity check `min_pq <= avg_pq <= max_pq` se conserva: venía del bug de BR2049 (la búsqueda a ciegas encontraba bloques hermanos con campos homónimos y daba un peak de ~176 nits donde el real era ~1000). Con el nivel ya etiquetado no puede pasar eso, pero un registro incoherente sigue sin servir.
- Conversión PQ→nits via SMPTE ST 2084 EOTF inverse (`_pq_code_to_nits`).
- Polling resiliente (`/api/mkv/light-profile/progress`) — chained-await en frontend (no setInterval, evita races out-of-order), guard monotónico anti-rollback.
- Resultado guardado en `_light_profile_state["result"]` para fallback si el POST aborta por timeout (frontend 60min timeout, backend hasta 35min).
- **Cada serie se reduce con el agregador que le toca**: máximo para los picos, media para el avg, mínimo para el suelo. Reducir las tres con el máximo aplanaría la banda y el gráfico mentiría.
- Sparkline SVG con 3 curvas superpuestas (peak/avg/min) + líneas de referencia (HDR10 MaxCLL/MaxFALL del SEI, L2 trim targets ámbar, L6 master display) + tooltip hover crosshair + chips de refs out-of-range.
- Mini-card de stats: percentiles (peak/p99/p95/p50/avg) + clasificación de escenas por brillo (SDR-like <100n / midtone 100-300n / highlight ≥300n).

### Comparador A/B del perfil de luminancia (Tab 2)

Superpone la curva L1 de **otro** MKV sobre la del que está abierto — el caso para el que existe es el mismo título antes y después del upgrade a CMv4.0, para responder *«¿mereció la pena?»* con el grading delante en vez de con `classify_l8`, que es un proxy. Botón **«⚖️ Comparar con…»** en la card del perfil; abre el file browser (Biblioteca + Output) y pinta la curva en rojo discontinuo más una tabla de deltas (peak, p99, p95, mediana, media de los picos).

- **`GET /api/mkv/light-profile-cached` NO lanza análisis.** Devuelve sólo lo que ya está en `/config/mkv_audits/`. Extraer el RPU de un UHD son ~10 min y eso no puede dispararse por elegir un fichero en un navegador; si falta, el mensaje distingue *«sin análisis previo»* de *«analizado pero sin perfil»* (cache de la auditoría vieja) para llevar al botón correcto. Es un GET barato —fingerprint = SHA del primer 1 MB— y no toca `workload`.
- **El eje X va normalizado a 0-100 % del metraje**, así que dos montajes con distinto número de frames se superponen ocupando el mismo ancho. Es deseado (así es como se ve un desfase) pero obliga a **avisar de la diferencia de duración**: por encima del 2 % la tabla lo dice, porque si no el usuario compara escenas que no se corresponden creyendo que sí.
- Dos detalles del SVG que un cambio descuidado rompe en silencio, los dos con test: el **eje Y abarca las DOS curvas** (si no, una comparación más brillante se sale del chart sin decirlo) y la curva de comparación **se reparte por su propia longitud**, no con el `xOf` de la serie propia (que la aplastaría contra el margen izquierdo si trae otro número de cubos). Cubierto por `test_comparador_luminancia.py`.

> **Distinción clave para usuarios**: L1 max_pq es la metadata DV codificada por el colorista, NO la luminancia real en pantalla tras tone-mapping. BR2049 etiqueta conservadoramente: peak L1 ~176 nits aunque medidas reales muestren ~600 nits. Confirmado: nuestro parser coincide al 100% con `dovi_tool info --summary`.

## Cadena de mastering (Tab 2 — radiografía DV+HDR)

El panel "Radiografía DV+HDR" del tab "Editar MKV" tiene una sección "Cadena de mastering" que centraliza toda la info de primaries y trim targets sin repetir datos en otros bloques.

- 3 cards: Master display (primaries de L9 o HDR10 SEI + peak/min nits) · Container HEVC (primaries + transfer + bit depth) · DV target display (L10 si presente).
- Chip ámbar "P3 ↑ BT.2020" cuando hay expansión de gamut master→container (caso muy común en UHD BD).
- Filas auxiliares: trim targets DV (chips ámbar de L2 target_max_pq) · HDR10 metadata (MaxCLL/MaxFALL SEI) · L11 content type.
- Campo backend `mastering_display_primaries` en `HdrMetadata` extraído de mediainfo `MasteringDisplay_ColorPrimaries`.

## Polling resiliente (Tab 2 light-profile)

- `setInterval` evitado: en su lugar chained-await en `_pollLoop()` con flag `polling`. Solo 1 fetch en vuelo a la vez → orden monotónico garantizado a nivel de transporte.
- `_dvLightSetStep` y `_dvLightSetProgress` ignoran updates con valor inferior al actual (guard monotónico, doble red).
- En error: modal NO se cierra automáticamente. Inyecta el error al log del modal + botón "Cerrar" explícito. Toast de 8s en vez de 3.5s.
- Fallback: si POST falla (red/abort) y `state.result` está poblado → recuperar el dato sin volver a procesar.

---

## Comandos de despliegue al NAS — REGLA FIJA

**Antes de nada: no desplegar si hay un job activo.** `compose up -d --build` reinicia el contenedor, así que mata el job en curso y encima el deploy se eterniza esperando a que el contenedor pare (un ffmpeg/dovi_tool en marcha lo bloquea: caso real 2026-08-17, >10 min contra los ~2-3 habituales). Comprobar los tres frentes — `/api/cmv40-active`, sesiones de Tab 1 en `running`/`queued`, `/api/mkv/apply/progress` — y si hay algo, avisar al usuario y esperar. El push a GitHub sí se puede hacer; lo que se retrasa es el paso del NAS.

Estos son los 3 comandos exactos que se usan en el NAS. **No inventar variantes** (no añadir `--no-cache`, no usar `pull` sobre `:latest`, no sugerir `force-recreate`, etc.). Si algo falla, revisar el log antes de proponer otro comando.

```bash
# 1. (Solo cuando hace falta) Limpiar cache de Docker — corrige el bug ZFS
#    de "no such file or directory" al construir.
sudo docker system prune -f

# 2. Pull del repo (alpine/git container — el repo fue clonado como root y git
#    rechaza la operación sin safe.directory).
docker run --rm -v /share/Container/hdo-iso-converter:/repo -w /repo alpine/git -c safe.directory=/repo pull

# 3. Rebuild + up del contenedor. REQUIERE `cd` al subdirectorio `docker/`
#    porque docker-compose.yml vive ahí, no en la raíz del repo. TMPDIR a ZFS
#    porque /tmp en QNAP es tmpfs pequeño y el build agota espacio.
cd /share/Container/hdo-iso-converter/docker
sudo TMPDIR=/share/ZFS20_DATA/Container/tmp docker compose up -d --build
```

Cuando el usuario pida "comandos del NAS" o "deploy al NAS", responder con los comandos 2 y 3 en bloque, y el `cd` SIEMPRE va incluido (no asumir que el usuario lo hace por su cuenta). El 1 solo si pide explícitamente limpieza o si hay evidencia de bug ZFS en el log.
