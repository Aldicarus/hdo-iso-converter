# HDO ISO Converter — Reglas del proyecto (v1.5)

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
- **Tab 3 — CMv4.0 BD:** (futuro) Pipeline para añadir Dolby Vision CMv4.0 a discos Blu-ray UHD via dovi_tool.

---

## Stack técnico
- **Backend:** Python 3.10+ (ubuntu:22.04), FastAPI, uvicorn
- **Frontend:** Vanilla JS ES6+, Sortable.js (CDN), sin framework ni build step
- **Contenedor:** Docker sobre QNAP NAS x86 (amd64), puerto 8090 (configurable)
- **Herramientas:** mkvmerge (análisis + extracción), mkvpropedit (edición in-place), mkvextract (capítulos), ffmpeg, dovi_tool
- **Acceso al ISO:** Loop mount directo (`mount -t udf -o ro,loop`) — requiere `privileged: true` en Docker
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
    │   ├── iso_mount.py     ← Loop mount/umount de ISOs UDF 2.50
    │   ├── phase_a.py       ← mkvmerge -J + adapter BDInfoResult + capítulos MPLS
    │   ├── phase_b.py       ← Motor de reglas automáticas (audio, subs, capítulos, nombre)
    │   ├── phase_d.py       ← mkvmerge desde MPLS (extracción, ruta intermedio)
    │   ├── phase_e.py       ← mkvmerge directo / mkvpropedit (ruta directa/intermedio)
    │   └── mkv_analyze.py   ← Tab 2: análisis + edición de MKVs existentes
    ├── dev_fixtures.py      ← ⚠️ TEMPORAL (DEV_MODE=1): ISOs fake + sesiones fake
    └── static/
        ├── index.html       ← SPA completa (Tab 1 + Tab 2 + modales)
        ├── app.js           ← Toda la lógica UI (~3800 líneas)
        └── style.css
```

## Volúmenes Docker
| Ruta en contenedor | Tipo | Descripción |
|---|---|---|
| `/mnt/isos` | read-only | ISOs de origen en el NAS |
| `/mnt/output` | read-write | MKVs finales (Tab 1 output + Tab 2 input) |
| `/mnt/tmp` | read-write | MKV intermedios (preferiblemente SSD) |
| `/config` | read-write | Sesiones persistentes (JSON) + cola |

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
- **Sin persistencia**: estado ephemeral en el frontend (`openMkvProjects[]`).
- **Backend stateless**: 3 endpoints bajo `/api/mkv/`.
- **Edición in-place**: `mkvpropedit` para metadata (O(1), instantáneo). `mkvmerge -o` solo si hay reorden de pistas.

### Flujo
1. "Abrir MKV" → modal picker lista MKVs de `/mnt/output`
2. `mkvmerge -J` + `mkvextract chapters` → panel con pistas y capítulos editables
3. "Aplicar cambios" → `mkvpropedit` o `mkvmerge` (con confirmación si remux)

### Endpoints
- `GET /api/mkv/files` — lista MKVs en `/mnt/output`
- `POST /api/mkv/analyze` — identifica pistas + capítulos
- `POST /api/mkv/apply` — aplica ediciones (mkvpropedit o remux)

### Editable
- Nombre del fichero (renombrado en disco)
- Pistas audio: nombre, flag default, reorden (drag & drop)
- Pistas subtítulos: nombre, flags default/forced, reorden
- Capítulos: misma timeline interactiva que Tab 1

---

## Reglas de desarrollo

### Idiomas
- El código (variables, funciones, clases) en **inglés**
- Strings de UI, mensajes de lógica y comentarios en **español**
- Los literales de pistas siguen exactamente la spec: "Castellano TrueHD Atmos 7.1", "Inglés DTS-HD MA 5.1", etc.

### Análisis del disco (Fase A — mkvmerge -J)
- **Fuente de datos**: `mkvmerge -J <mpls_path>` sobre el MPLS principal del disco montado
- **Selección inteligente del MPLS**: ejecuta `mkvmerge -J` sobre los 10 MPLS más grandes, elige el que tiene más pistas de audio (el título principal siempre tiene más que menús o playlists de navegación)
- **Adaptador BDInfoResult**: el JSON se convierte a los mismos modelos que consumía phase_b con BDInfoCLI
- **Codecs**: mkvmerge → estilo BDInfo (ej: "TrueHD Atmos" → "Dolby TrueHD/Atmos Audio")
- **Idiomas**: ISO 639-2 → nombres en inglés via `ISO639_TO_ENGLISH`
- **DD+ Atmos**: heurística: E-AC-3 con ≥8 canales = Atmos
- **TrueHD + AC-3 core**: se filtra el core subordinado via `multiplexed_tracks`
- **FEL**: presencia de segundo track HEVC a 1080p
- **Subtítulos forzados**: detección por estructura de bloques Blu-ray. Bloque 1 (completos, todos los idiomas) → Bloque 2 (forzados, subconjunto de idiomas). El corte se detecta cuando un idioma aparece por segunda vez. Validación: idiomas del bloque 2 deben ser subconjunto del bloque 1.
- **Capítulos**: extracción en Fase A via mini MKV (`mkvmerge --no-audio --no-video --no-subtitles`) + `mkvextract chapters --simple`. Timestamps precisos de mkvmerge. Nombres genéricos ("Chapter XX") traducidos a "Capítulo XX". Nombres custom del disco preservados con `name_custom=True`.
- **Datos raw**: el JSON completo de mkvmerge -J se guarda en `bdinfo_result.mkvmerge_raw` para diagnóstico.
- **Duración**: `playlist_duration / 1_000_000_000` (nanosegundos → segundos)

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
- **Base**: `ubuntu:22.04` + `.NET 8 runtime` (para BDInfo legacy) + MKVToolNix + ffmpeg + Python 3.10
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
