# ESPECIFICACIÓN TÉCNICA — PASO 1: ISO UHD → MKV
*Versión 1.3 — Arquitectura QTS File Station + mkvmerge directo*

---

## 1. VISIÓN GENERAL

El programa analiza una ISO de UHD Blu-ray, aplica un conjunto de reglas automáticas para seleccionar, clasificar y nombrar todas las pistas, presenta una pantalla de revisión editable, y ejecuta el proceso completo de extracción y remux de forma automática. Cada ejecución genera una configuración persistente identificada automáticamente y relanzable en cualquier momento.

El programa se ejecuta como contenedor Docker sobre un NAS QNAP x86 (amd64) mediante Docker Station. Todos los ficheros de entrada (ISOs) y salida (MKVs) residen en volúmenes del NAS montados directamente en el contenedor — ningún fichero se copia fuera de su ubicación original.

---

## 2. ENTORNO DE EJECUCIÓN

### 2.1 Arquitectura

```
QNAP NAS x86 (amd64)
└── Docker Station
    └── Contenedor único (todas las herramientas)
        ├── /mnt/isos    → volumen NAS con ISOs (read-only)
        ├── /mnt/output  → volumen NAS de salida MKVs (read-write)
        └── /mnt/tmp     → volumen temporal para intermedios (read-write)
                           Preferiblemente en SSD si disponible en el NAS
```

### 2.2 Herramientas — todas en un único contenedor Docker

| Herramienta | Versión mínima | Imagen / Fuente | Uso |
|------------|---------------|-----------------|-----|
| **BDInfoCLI** (tetrahydroc) | última | `tetrahydroc/BDInfoCLI` — binario .NET 8 nativo Linux x64 | Análisis del disco — lee BDMV montado por QTS |
| **mkvmerge** | 84+ | Repositorios Linux estándar | Extracción desde MPLS (Fase D) + Remux con reordenación (Fase E) |
| **mkvpropedit** | 84+ | Repositorios Linux estándar (incluido en MKVToolNix) | Edición metadata in-place sin copia (Fase E) |
| **mkvextract** | 84+ | Repositorios Linux estándar (incluido en MKVToolNix) | Extracción de capítulos del MKV intermedio |
| **ffmpeg** | 6.0+ | Repositorios Linux estándar | Extracción HEVC para dovi_tool (Tab 3) |
| **dovi_tool** | 2.1+ | Binario Linux x64 — GitHub releases | Manipulación RPU Dolby Vision (Tab 3) |

> **Nota:** `makemkvcon` fue eliminado en v1.3. Los ISOs del usuario están desencriptados (sin AACS2), por lo que mkvmerge puede leer directamente el MPLS del disco montado por QTS, sin necesidad de la capa de descifrado de MakeMKV.

> **Nota sobre BDInfo:** la versión GUI de BDInfo no es compatible con entornos headless. Se usa `BDInfoCLI` (fork de tetrahydroc), que está compilado en .NET 8 nativo sin dependencia de Mono y tiene soporte completo para ISOs UHD y Docker.
> Repositorio: `https://github.com/tetrahydroc/BDInfoCLI`

---

## 3. FLUJO DE EJECUCIÓN

```
ISO en NAS (read-only, nunca se mueve)
     │
     ├──► API QTS File Station: mount_iso → share BDMV
     │
     ├──► [FASE A] BDInfoCLI lee BDMV montado → report
     │                                           unmount_iso (siempre, en finally)
     ↓
[FASE B] Aplicación de reglas automáticas + capítulos provisionales
     ↓
[FASE C] Pantalla de revisión (edición opcional)
     ↓
[Confirmar] → Cola FIFO (QueueManager)
     ↓
mount_iso (loop mount UDF)
     ↓
find_main_mpls → needs_reordering?
     │
     ├── SÍ → mkvmerge MPLS → MKV final directamente (1 copia)
     │        ISO montado durante toda la escritura
     │
     └── NO → mkvmerge MPLS → intermedio (1 copia)
              + mkvextract capítulos reales
              + mkvpropedit in-place (0 copias)
              + mv → MKV final
     ↓
unmount_iso (siempre, en finally)
     ↓
MKV final en /mnt/output
```

### 3.1 Principios de optimización de I/O (v1.4)

Los ficheros ISO de 70-100 GB implican tiempos de lectura/escritura significativos. Las reglas que minimizan las operaciones de I/O son:

1. **El ISO nunca se copia.** Se monta via loop mount (UDF 2.50); mkvmerge lee directamente del mount point.
2. **Siempre 1 sola copia de datos.** La decisión de ruta se toma ANTES de extraer, no después.
3. **Ruta directa (reordenación):** mkvmerge lee del MPLS y escribe el MKV final directamente. Sin intermedio.
4. **Ruta intermedio (sin reordenación):** mkvmerge extrae al intermedio + mkvpropedit edita metadatos in-place + mv al output. Sin segunda copia.

---

### 3.2 Acceso al ISO — Loop mount directo (UDF 2.50)

Los ISOs se montan directamente dentro del contenedor Docker usando `mount -t udf -o ro,loop`. Esto requiere `privileged: true` en Docker pero elimina toda dependencia de APIs externas.

**¿Por qué loop mount?** Los ISOs UHD Blu-ray usan UDF 2.50/2.60. Solo el driver del kernel (`udf.ko`) lo soporta. Herramientas userspace (fuseiso, 7z, pycdlib) no leen UDF 2.50.

**Flujo de montaje:**
1. `mount -t udf -o ro,loop {iso} /mnt/bd/{nombre}_{pid}/`
2. BDInfoCLI / mkvmerge leen desde el mount point (`BDMV/`, `BDMV/PLAYLIST/`, `BDMV/STREAM/`)
3. `umount` en el bloque `finally` (siempre, éxito o error) + limpieza del directorio

**Mount point:** `/mnt/bd/{iso_stem}_{pid}/` — incluye PID para evitar colisiones.
**Variable de entorno:** `MOUNT_BASE` (default: `/mnt/bd`).
**Fallback:** si `umount` falla, se intenta `umount -l` (lazy unmount).
**Sin dependencias externas:** no requiere API QTS, SSH, ni credenciales.

### 3.3 Cola de ejecución

Las Fases D+E se ejecutan en una cola FIFO (`QueueManager`) con ejecución secuencial (un trabajo a la vez). La cola se persiste a disco (`queue_state.json`) para sobrevivir reinicios. Al arrancar, las sesiones con `status in ("running", "queued")` se resetean a `"pending"`.

---

## 4. FASE A — ANÁLISIS CON BDINFO

### 4.1 Formato del report de BDInfo — referencia completa

```
VIDEO:
Codec                  Bitrate          Description
-----                  -------          -----------
MPEG-H HEVC Video      66795 kbps       2160p / 23.976 fps / 16:9 / Main 10 @ Level 5.1 @ High / 4:2:0 / 10 bits / 1000nits / HDR10 / BT.2020
* MPEG-H HEVC Video    2093 kbps (3.04%)  1080p / 23.976 fps / 16:9 / Main 10 @ Level 5.1 @ High / 4:2:0 / 10 bits / 1000nits / Dolby Vision FEL / BT.2020

AUDIO:
Codec                         Language   Bitrate     Description
-----                         --------   -------     -----------
Dolby TrueHD/Atmos Audio      English    4944 kbps   7.1+11 objects / 48 kHz / 4304 kbps / 24-bit (AC3 Core: 5.1 / 48 kHz / 640 kbps)
DTS-HD Master Audio           Spanish    2228 kbps   5.1 / 48 kHz / 2228 kbps / 16-bit (DTS Core: 5.1 / 48 kHz / 1509 kbps)
Dolby Digital Plus Audio      French     768 kbps    7.1-Atmos / 48 kHz / 768 kbps (DD+ Embedded: 5.1 / 48 kHz / 448 kbps)
Dolby Digital Audio           English    640 kbps    5.1 / 48 kHz / 640 kbps

SUBTITLES:
Codec                  Language   Bitrate      Description
-----                  --------   -------      -----------
Presentation Graphics  English    35.498 kbps
Presentation Graphics  Spanish    40.006 kbps
Presentation Graphics  Spanish     1.115 kbps
Presentation Graphics  qad         0.842 kbps
```

### 4.1.1 Ejemplos reales de secciones AUDIO de BDInfo

**Ejemplo A — DTS-HD MA como VO, TrueHD Atmos en Spanish:**
```
DTS-HD Master Audio      English    3260 kbps    5.1 / 48 kHz / 3260 kbps / 24-bit (DTS Core: 5.1 / 48 kHz / 1509 kbps / 24-bit)
Dolby TrueHD/Atmos Audio Spanish    4831 kbps    7.1 / 48 kHz / 4191 kbps / 24-bit (AC3 Embedded: 5.1 / 48 kHz / 640 kbps / DN -31dB)
DTS-HD Master Audio      English    1997 kbps    2.0 / 48 kHz / 1997 kbps / 24-bit (DTS Core: 2.0 / 48 kHz / 1509 kbps / 24-bit)
```
→ VO = English. Incluidas: `Castellano TrueHD Atmos 7.1` (default=yes) + `Inglés DTS-HD MA 5.1` (default=no, mayor bitrate entre las dos English). DTS-HD MA 2.0 descartada por menor bitrate.

**Ejemplo B — Múltiples idiomas, DD+ sin Atmos en Description:**
```
Dolby TrueHD/Atmos Audio  English    5386 kbps    7.1 / 48 kHz / 4746 kbps / 24-bit (AC3 Embedded: 5.1 / 48 kHz / 640 kbps / DN -27dB)
Dolby Digital Audio       English     320 kbps    2.0 / 48 kHz / 320 kbps / DN -27dB
Dolby Digital Plus Audio  French     1024 kbps    7.1 / 48 kHz / 1024 kbps / DN -27dB (AC3 Embedded: 5.1-EX / 48 kHz / 576 kbps / DN -27dB)
Dolby TrueHD/Atmos Audio  Spanish    5511 kbps    7.1 / 48 kHz / 4871 kbps / 24-bit (AC3 Embedded: 5.1 / 48 kHz / 640 kbps / DN -31dB)
Dolby Digital Plus Audio  Japanese   1024 kbps    7.1 / 48 kHz / 1024 kbps / DN -27dB (AC3 Embedded: 5.1-EX / 48 kHz / 576 kbps / DN -27dB)
Dolby Digital Audio       Japanese    320 kbps    2.0 / 48 kHz / 320 kbps / DN -27dB
```
→ VO = English. Incluidas: `Castellano TrueHD Atmos 7.1` + `Inglés TrueHD Atmos 7.1`. French y Japanese descartados por idioma. DD inglés descartado por menor calidad que TrueHD Atmos. DD+ French sin "Atmos" en Description → sería `DD+ 7.1`, pero se descarta por idioma.

**Ejemplo C — DD+ sin Atmos, DTS simple, múltiples idiomas europeos:**
```
Dolby TrueHD/Atmos Audio  English    5295 kbps    7.1 / 48 kHz / 4655 kbps / 24-bit (AC3 Embedded: 5.1 / 48 kHz / 640 kbps / DN -4dB)
Dolby Digital Audio       English     320 kbps    2.0 / 48 kHz / 320 kbps / DN -4dB
Dolby Digital Plus Audio  French     1024 kbps    7.1 / 48 kHz / 1024 kbps (DD+ Embedded: 5.1 / 48 kHz / 576 kbps)
Dolby Digital Plus Audio  German     1024 kbps    7.1 / 48 kHz / 1024 kbps (DD+ Embedded: 5.1 / 48 kHz / 576 kbps)
Dolby Digital Plus Audio  Italian    1024 kbps    7.1 / 48 kHz / 1024 kbps (DD+ Embedded: 5.1 / 48 kHz / 576 kbps)
Dolby TrueHD/Atmos Audio  Spanish    6259 kbps    7.1 / 48 kHz / 5619 kbps / 24-bit (AC3 Embedded: 5.1 / 48 kHz / 640 kbps)
Dolby Digital Audio       English     192 kbps    2.0 / 48 kHz / 192 kbps
DTS Audio                 Spanish    1509 kbps    5.1 / 48 kHz / 1509 kbps / 24-bit / DN -4dB
```
→ VO = English. Incluidas: `Castellano TrueHD Atmos 7.1` (TrueHD Atmos > DTS) + `Inglés TrueHD Atmos 7.1`. French, German, Italian descartados por idioma. DD inglés descartado por menor calidad. DTS Spanish descartado por menor calidad que TrueHD Atmos.

---

### 4.2 Detección de FEL/MEL

La EL (Enhancement Layer) aparece en BDInfo como segunda fila de vídeo HEVC, prefijada siempre con `*` (hidden stream). El bitrate puede incluir un porcentaje entre paréntesis después del número.

**Regla de identificación de la EL:**
La fila de la EL cumple todas estas condiciones:
- Empieza con `*`
- El campo Codec contiene `HEVC Video` (como subcadena — puede aparecer como `HEVC Video` o `MPEG-H HEVC Video` según la versión de BDInfo)
- La resolución en Description es `1080p`

**Regla de parseo del bitrate de la EL:**
Extraer el número antes del primer espacio de la columna Bitrate.
Ejemplos: `2093 kbps (3.04%)` → `2093` / `3957 kbps` → `3957`

**Regla de detección FEL (combinada):**

| Condición | Resultado |
|-----------|-----------|
| EL bitrate > 1000 kbps | `FEL = true` |
| EL bitrate ≤ 1000 kbps | `FEL = false` (MEL) |
| Description contiene `"Dolby Vision FEL"` | `FEL = true` (confirmación adicional, no exclusiva) |
| Description contiene `"Dolby Vision MEL"` | `FEL = false` (confirmación adicional, no exclusiva) |
| Description contiene solo `"Dolby Vision"` sin FEL/MEL | Aplicar solo la regla de bitrate |

El bitrate es el criterio primario. El texto es confirmación secundaria cuando está presente.

**Ejemplos reales verificados:**

| EL bitrate | Description | Resultado | Verificado |
|-----------|-------------|-----------|-----------|
| `2093 kbps (3.04%)` | `Dolby Vision FEL` | FEL ✅ | Texto + bitrate |
| `3957 kbps` | `Dolby Vision` | FEL ✅ | Solo bitrate |

### 4.3 Detección del tag Audio DCP

**Regla:** buscar la cadena `Audio DCP` (case-insensitive) en el nombre del fichero ISO. Si se encuentra → `AUDIO_DCP = true`.

### 4.4 Identificación de la VO

**Regla:** la VO es la primera pista de audio que aparece en la sección AUDIO del report de BDInfo.

---

## 5. FASE B — REGLAS AUTOMÁTICAS

### 5.1 Reglas de audio

#### 5.1.1 Tabla de conversión de idioma BDInfo → literal

| Idioma en BDInfo | Literal |
|-----------------|---------|
| `Spanish` | `Castellano` |
| `English` | `Inglés` |
| Resto | Nombre del idioma traducido al español |

#### 5.1.2 Tabla de conversión de codec BDInfo → literal base

La detección del codec se realiza sobre el campo **Codec** del report. Dado que el string exacto varía entre versiones de BDInfo, se usan reglas de coincidencia por contenido de subcadenas, no por igualdad exacta.

| Regla de detección sobre campo Codec | Literal base |
|--------------------------------------|-------------|
| Contiene `TrueHD` **Y** contiene `Atmos` | `TrueHD Atmos {canales}` |
| Contiene `TrueHD` **Y NO** contiene `Atmos` | `TrueHD {canales}` |
| Contiene `Digital Plus` **Y** Description contiene `Atmos` | `DD+ Atmos {canales}` |
| Contiene `Digital Plus` **Y** Description NO contiene `Atmos` | `DD+ {canales}` |
| Contiene `DTS-HD Master` | `DTS-HD MA {canales}` |
| Contiene `DTS Audio` (y NO contiene `HD`) | `DTS {canales}` |
| Contiene `Dolby Digital` (y NO contiene `Plus`) | `DD {canales}` |

**Ejemplos de strings reales de BDInfo y su detección:**

| String real en BDInfo | Regla aplicada | Literal base |
|----------------------|----------------|-------------|
| `Dolby TrueHD/Atmos Audio` | TrueHD + Atmos | `TrueHD Atmos {canales}` |
| `Dolby TrueHD + Atmos` | TrueHD + Atmos | `TrueHD Atmos {canales}` |
| `Dolby Atmos/TrueHD Audio` | TrueHD + Atmos | `TrueHD Atmos {canales}` |
| `Dolby Digital Plus Audio` + Description con `7.1-Atmos` | Digital Plus + Atmos en Description | `DD+ Atmos {canales}` |
| `Dolby Digital Plus Audio` + Description sin Atmos | Digital Plus sin Atmos | `DD+ {canales}` |
| `DTS-HD Master Audio` | DTS-HD Master | `DTS-HD MA {canales}` |
| `DTS Audio` | DTS sin HD | `DTS {canales}` |
| `Dolby Digital Audio` | Dolby Digital simple | `DD {canales}` |

#### 5.1.3 Extracción de canales desde el campo Description

El campo Description tiene el formato:
```
{canales} / {frecuencia} / {bitrate} / ...
```

**Regla de extracción:**
1. Tomar el fragmento antes del primer `/`, hacer trim.
2. Aplicar en orden:
   - Si contiene `+` → quedarse con la parte antes del `+`
   - Si contiene `-Atmos` → quedarse con la parte antes de `-Atmos`
   - En cualquier otro caso → usar el fragmento directamente

| Description | Fragmento extraído | Resultado |
|------------|-------------------|-----------|
| `7.1 / 48 kHz / ...` | `7.1` | `7.1` |
| `5.1 / 48 kHz / ...` | `5.1` | `5.1` |
| `2.0 / 48 kHz / ...` | `2.0` | `2.0` |
| `7.1+11 objects / 48 kHz / ...` | `7.1+11 objects` → antes del `+` | `7.1` |
| `7.1-Atmos / 48 kHz / ...` | `7.1-Atmos` → antes de `-Atmos` | `7.1` |

#### 5.1.4 Regla adicional Audio DCP

Si `AUDIO_DCP = true` y el codec detectado es `TrueHD Atmos` **y el idioma es Castellano** → añadir el sufijo ` (DCP 9.1.6)` al final del literal. Solo aplica a la pista TrueHD Atmos en castellano. Nunca en inglés ni otros idiomas.

#### 5.1.5 Construcción del literal de audio

```
{idioma} {literal_base}
```

Con sufijo DCP si aplica:
```
{idioma} {literal_base} (DCP 9.1.6)
```

**Ejemplos completos:**

| Caso | Literal resultante |
|------|-------------------|
| TrueHD Atmos 7.1 en Spanish | `Castellano TrueHD Atmos 7.1` |
| TrueHD Atmos 7.1 en Spanish + Audio DCP | `Castellano TrueHD Atmos 7.1 (DCP 9.1.6)` |
| TrueHD Atmos 7.1 en English + Audio DCP | `Inglés TrueHD Atmos 7.1` *(sin DCP — solo aplica a Castellano)* |
| DD+ Atmos 7.1 en Spanish | `Castellano DD+ Atmos 7.1` |
| DD+ 7.1 en Spanish (sin Atmos en Description) | `Castellano DD+ 7.1` |
| DTS-HD MA 5.1 en Spanish | `Castellano DTS-HD MA 5.1` |
| DTS 5.1 en French | `Francés DTS 5.1` |
| DD 5.1 en English | `Inglés DD 5.1` |

#### 5.1.6 Criterio de calidad y selección de pistas

Orden de prioridad descendente:
```
1. TrueHD Atmos
2. DD+ Atmos
3. DTS-HD MA
4. DTS
5. DD
```

**Regla de selección por idioma:** para cada idioma, seleccionar únicamente la pista de mayor calidad según el criterio anterior. Si hay varias pistas del mismo idioma y mismo codec, seleccionar la de mayor bitrate. Las demás se descartan.

**Regla de idiomas incluidos:** solo se incluyen dos idiomas — Castellano y VO. Cualquier pista de otro idioma (French, German, Italian, Japanese, etc.) se descarta siempre, independientemente de su codec o calidad.

**Regla de detección de Atmos (asimetría TrueHD vs DD+):**
- **TrueHD Atmos:** se detecta exclusivamente sobre el campo **Codec**. Si el codec contiene `TrueHD` y `Atmos` → es TrueHD Atmos, sin necesidad de examinar Description.
- **DD+ Atmos:** el campo Codec siempre dice `Dolby Digital Plus Audio` sin mencionar Atmos. La detección de Atmos requiere examinar el campo **Description** buscando `Atmos` (ej: `7.1-Atmos`). Si no aparece Atmos en Description → es DD+ sin Atmos.

#### 5.1.7 Pistas de audio incluidas y orden

1. Mejor pista en castellano → `default=yes`
2. Mejor pista en VO → `default=no`

Si castellano = VO, solo aparece una pista.

---

### 5.2 Reglas de subtítulos

#### 5.2.1 Tabla de conversión de idioma

Misma tabla que 5.1.1.

#### 5.2.2 Detección y descarte de Audio Description (AD)

Aplicar en orden. Primera regla que coincida determina el descarte:

**Regla 1 — Código de idioma `qad`:**
Si el campo Language de BDInfo es `qad` → pista AD, descartar.
*Evidencia: "Descartada: código de idioma qad (estándar ISO 639 para Audio Description)"*

**Regla 2 — Nombre de pista en metadatos MPLS:**
Si el nombre de la pista (leído de los metadatos MPLS vía MakeMKV) contiene `Audio Description`, `Descriptive` o ` AD` (case-insensitive) → pista AD, descartar.
*Evidencia: "Descartada: nombre de pista contiene '{valor encontrado}'"*

**Regla 3 — Sin señal automática:**
Si ninguna regla anterior aplica y hay varias pistas del mismo idioma con bitrate similar, no descartar automáticamente. Incluir todas y dejar al usuario que decida en la pantalla de revisión.

#### 5.2.3 Clasificación: Forzados Forma A vs Completos

Para un idioma dado, BDInfo puede mostrar dos pistas `Presentation Graphics`:

```
Presentation Graphics  Spanish    40.006 kbps   ← Completos
Presentation Graphics  Spanish     1.115 kbps   ← Forzados Forma A
```

**Regla Forma A:** si para un idioma existen DOS o más pistas y una tiene bitrate < 3 kbps → es la pista de forzados separada (Forma A).
*Evidencia: "Forzados Forma A: bitrate {X} kbps < umbral 3 kbps. Pista completa del mismo idioma presente con bitrate {Y} kbps"*

**Regla Completos:** pista con bitrate ≥ 3 kbps, o la única pista del idioma.

**Nota Forma B:** si solo existe una pista para un idioma, se clasifica por defecto como Completos. El usuario puede activar manualmente el flag `forced=yes` en la pantalla de revisión si lo considera necesario.

#### 5.2.4 Construcción del literal de subtítulos

```
{idioma} Forzados PGS    ← pistas Forma A
{idioma} Completos PGS   ← pistas Completos
```

**Ejemplos:**

| Pista BDInfo | Clasificación | Literal |
|-------------|--------------|---------|
| `Presentation Graphics Spanish 40.006 kbps` | Completos | `Castellano Completos PGS` |
| `Presentation Graphics Spanish 1.115 kbps` (con pista completa presente) | Forzados Forma A | `Castellano Forzados PGS` |
| `Presentation Graphics English 35.498 kbps` | Completos | `Inglés Completos PGS` |
| `Presentation Graphics qad 0.842 kbps` | AD → descartar | — |

#### 5.2.5 Pistas de subtítulos incluidas, orden y flags

| Posición | Pista | forced | default |
|----------|-------|--------|---------|
| 1 | Forzados PGS Castellano | `yes` | `yes` |
| 2 | Forzados PGS VO | `yes` | `no` |
| 3 | Completos PGS VO | `no` | `no` |
| 4 | Completos PGS Inglés (si existe y VO ≠ Inglés) | `no` | `no` |

Todas las posiciones son condicionales: solo se incluyen si la pista existe en el disco según BDInfo.
Si castellano = VO, las posiciones 1 y 2 se fusionan en una sola pista.

---

### 5.3 Reglas de capítulos

**Fuente:** campo `Length` del playlist en el report de BDInfo para la duración total. Capítulos extraídos del MPLS del disco vía MakeMKV.

**Regla de nombre desde disco:** si el capítulo tiene nombre en el MPLS → usar el nombre del disco tal cual.

**Regla de nombre sin datos en disco:** si los capítulos no tienen nombre → generar `Capítulo 01`, `Capítulo 02`... (numeración en dos dígitos, en español).

**Regla de capítulos ausentes:** si el disco no contiene ningún capítulo → generar capítulos estándar automáticamente cada 10 minutos empezando en el **primer intervalo** (`00:10:00.000`, no desde `00:00:00.000`), hasta cubrir la duración total de la película. Numeración `Capítulo 01`, `Capítulo 02`... Los capítulos estándar se marcan con la indicación `"Capítulos estándar generados automáticamente (no presentes en el disco)"` en la pantalla de revisión.

> **Nota v1.3:** los capítulos provisionales se generan en Fase B para que el usuario pueda editar la timeline mientras espera. Los capítulos reales del disco se extraen del MKV intermedio en Fase D (vía `mkvextract --simple`) y reemplazan los provisionales.

---

### 5.4 Nombre del fichero MKV

#### 5.4.1 Extracción de título y año del nombre del ISO

El nombre del ISO sigue el patrón:
```
{Título} ({Año}) [...].iso
```

**Regla título:** extraer todo lo que hay antes del primer `(`, hacer trim.
**Regla año:** extraer el primer número de 4 dígitos entre paréntesis.

*Ejemplo: `El Rey de Reyes (2025) [FullBluRay ...].iso` → título `El Rey de Reyes`, año `2025`*

#### 5.4.2 Construcción del nombre

```
{Título} ({Año}).mkv
{Título} ({Año}) [DV FEL].mkv                     ← si FEL = true
{Título} ({Año}) [Audio DCP].mkv                   ← si AUDIO_DCP = true
{Título} ({Año}) [DV FEL] [Audio DCP].mkv          ← si ambos
```

El nombre se recalcula automáticamente cuando el usuario modifica `FEL` o `AUDIO_DCP` en la pantalla de revisión. El campo del nombre también es editable manualmente de forma independiente.

---

## 6. FASE C — PANTALLA DE REVISIÓN

### 6.1 Variables globales

En la parte superior de la pantalla, antes de las secciones de pistas:

| Variable | Valor detectado | Lógica aplicada | Editable |
|----------|----------------|-----------------|----------|
| **FEL** | `true` / `false` | Ej: `"FEL detectado: bitrate EL = 3957 kbps > 1000 kbps"` | Toggle on/off |
| **Audio DCP** | `true` / `false` | Ej: `"Tag 'Audio DCP' encontrado en nombre del ISO"` | Toggle on/off |
| **Nombre del MKV** | Nombre generado | Ej: `"Generado a partir de título + año + tags activos"` | Campo de texto |

**Comportamiento del nombre:** cuando el usuario cambia el toggle de FEL o Audio DCP, el nombre del MKV se recalcula automáticamente. Si el usuario ha editado el nombre manualmente, el recálculo no sobreescribe su edición — se le muestra un aviso indicando que el nombre ha sido editado manualmente y se ofrece la opción de revertir al nombre calculado.

---

### 6.2 Sección 1 — Pistas incluidas

Reordenables por drag & drop. Cada pista muestra:

| Campo | Descripción |
|-------|-------------|
| **#** | Posición actual en el MKV final |
| **Raw BDInfo** | Línea exacta del report de BDInfo |
| **Lógica de selección** | Regla aplicada en lenguaje legible |
| **Literal** | Campo de texto editable |
| **flag `default`** | Toggle on/off + razón de la decisión automática |
| **flag `forced`** | Toggle on/off + razón de la decisión automática |

### 6.3 Sección 2 — Pistas descartadas

Cada pista muestra:

| Campo | Descripción |
|-------|-------------|
| **Raw BDInfo** | Línea exacta del report de BDInfo |
| **Lógica de descarte** | Regla aplicada en lenguaje legible |
| **Botón "Recuperar"** | Mueve la pista a Sección 1 para edición manual |

### 6.4 Sección 3 — Capítulos

#### 6.4.1 Barra de tiempo

Representación gráfica horizontal de la duración total de la película. Cada capítulo se muestra como una marca vertical sobre la barra. El usuario puede:
- **Añadir** un capítulo: clic en cualquier punto de la barra → se inserta una marca en ese timestamp
- **Mover** un capítulo: arrastrar una marca a otra posición en la barra
- **Eliminar** un capítulo: seleccionar una marca y pulsar eliminar (o doble clic)

La barra muestra el timestamp en el punto donde se sitúa el cursor para facilitar la colocación precisa.

#### 6.4.2 Lista de capítulos

Sincronizada con la barra de tiempo. Cada capítulo muestra:

| Campo | Descripción |
|-------|-------------|
| **#** | Número de capítulo — siempre consecutivo y recalculado automáticamente |
| **Timestamp** | Tiempo de inicio — editable manualmente como campo de texto |
| **Nombre** | Campo de texto editable |

**Regla de renumeración:** cualquier operación de añadir, mover o eliminar en la barra o en la lista recalcula automáticamente la numeración de todos los capítulos para que sea consecutiva y ordenada por timestamp.

#### 6.4.3 Indicador de origen

Si los capítulos proceden del disco: sin indicación especial.
Si los capítulos son estándar generados automáticamente: aviso visible `"⚠ Capítulos estándar generados automáticamente — no presentes en el disco original"`.

### 6.5 Ejemplos de mensajes de lógica

**Variables globales:**
- `"FEL detectado: bitrate EL = 3957 kbps > umbral 1000 kbps (BDInfo no especifica FEL/MEL en texto)"`
- `"FEL detectado: bitrate EL = 2093 kbps > umbral 1000 kbps. Confirmado por texto: 'Dolby Vision FEL'"`
- `"MEL detectado: bitrate EL = 68 kbps ≤ umbral 1000 kbps"`
- `"Audio DCP: tag 'Audio DCP' encontrado en nombre del ISO"`
- `"Audio DCP: tag no encontrado en nombre del ISO"`

**Selección de audio:**
- `"Seleccionada: mejor calidad para Spanish. TrueHD Atmos > DD+ > DTS-HD MA > DTS > DD"`
- `"Seleccionada: VO (primera pista del disco)"`
- `"flag default=yes: primera pista de audio en castellano"`

**Selección de subtítulos:**
- `"Forzados Forma A: bitrate 1.115 kbps < umbral 3 kbps. Pista completa Spanish presente con bitrate 40.006 kbps"`
- `"Completos: única pista para Spanish con bitrate 40.006 kbps"`
- `"flag forced=yes: Forma A detectada por bitrate"`
- `"flag default=yes: primera pista de subtítulos forzados en castellano"`

**Descarte:**
- `"Descartada: código de idioma qad (estándar ISO 639 para Audio Description)"`
- `"Descartada: nombre de pista contiene 'Audio Description'"`
- `"Descartada: segunda pista Spanish. Menor calidad: DD 5.1 < TrueHD Atmos 7.1"`
- `"Descartada: idioma French no es Castellano ni VO (English)"`

### 6.6 Acción final

**Botón adaptativo según estado:**
- `pending` → **"▶️ Confirmar y ejecutar"**: guarda el JSON, verifica ISO, muestra confirmación, encola en QueueManager.
- `done` → **"↻ Re-ejecutar"**: permite relanzar la ejecución tras editar la configuración.
- `running`/`queued` → **"⏳ En ejecución…"** (deshabilitado).

**Validación previa a ejecución:**
1. ISO disponible en disco (`GET /api/sessions/{id}/check-iso`)
2. Estado de la sesión (`running`/`queued` → rechazado con HTTP 400)

### 6.7 Banner de ejecución activa

El banner **solo se muestra cuando hay una ejecución en curso o en cola** para este proyecto:

| Estado | Estilo | Contenido |
|--------|--------|-----------|
| `running` | Azul (`.banner.info`) | ⏳ "Ejecución en curso…" + botón "📺 Ver progreso" |
| `queued` | Azul (`.banner.info`) | ⏸ "En cola de ejecución" + botón "📺 Ver progreso" |
| `done`/`error`/`pending` | — | Sin banner (los resultados se consultan en la tabla §6.10) |

**Actualización en tiempo real:** `refreshOpenProjectState(sessionId)` recarga la sesión del backend y actualiza banner, tabla, phase strip, botón e icono de tab sin re-renderizar todo el panel (preserva ediciones del usuario). Se invoca automáticamente cuando:
- La ejecución termina (`__DONE__` / `__ERROR__` via WebSocket)
- La cola cambia de estado (nuevo running, job terminado)
- El usuario encola una ejecución

### 6.8 Phase strip dinámica

La tira de fases A→B→C→D→E refleja el estado real de la sesión:

| Estado sesión | Fases A+B | Fase C | Fases D+E |
|---------------|-----------|--------|-----------|
| `pending` | done | **active** | muted |
| `queued` | done | done | muted |
| `running` | done | done | **active** |
| `done` | done | done | done |
| `error` | done | done | **error** |

### 6.9 Icono de sub-tab dinámico

El emoji del sub-tab del proyecto cambia según `session.status`:
💿 (`pending`) → ⏸ (`queued`) → ⏳ (`running`) → ✅ (`done`) → ❌ (`error`)

### 6.10 Tabla de historial de ejecuciones

Al final del panel de proyecto, antes de los botones de acción, se muestra una tabla con el historial de todas las ejecuciones del proyecto. Cada fila corresponde a un `ExecutionRecord` en `session.execution_history`:

| Columna | Contenido |
|---------|-----------|
| **#** | `run_number` (secuencial) |
| **Fecha** | `started_at` formateado como fecha relativa, tooltip con fecha completa |
| **Estado** | ✅ (done) o ❌ (error), tooltip con `error_message` si hay error |
| **💿 Montar** | `phase_elapsed.mount` en MM:SS |
| **⬇️ Extraer** | `phase_elapsed.extract` en MM:SS |
| **🔓 Desmontar** | `phase_elapsed.unmount` en MM:SS |
| **✍️ Escribir** | `phase_elapsed.write` en MM:SS |
| **⏱ Total** | Duración total (`finished_at - started_at`) |
| **Acciones** | Botón "📄 Log" (abre modal) + "⬇" (descarga .txt) |

Si no hay ejecuciones, se muestra "Sin ejecuciones todavía". Las filas se ordenan por más reciente primero.

### 6.11 Modal visor de log

El botón "📄 Log" de cada fila del historial abre un modal (`#log-viewer-modal`) con:
- Título: "📄 Log — Ejecución #N"
- Subtítulo: estado + fecha
- Contenido: log completo con coloreado semántico (errores en rojo, fases en azul, completado en teal)
- Botón "Descargar .txt" integrado
- Cierre: Escape, clic fuera, o botón "Cerrar"

### 6.12 Cola sidebar simplificada

La cola sidebar derecha (siempre visible en Tab 1) muestra exclusivamente:
- **En curso**: trabajo activo con pipeline de fases, barra de progreso, timer
- **Pendiente de inicio**: trabajos encolados, reordenables con drag & drop (Sortable.js)

El historial de ejecuciones anteriores **no se muestra en la sidebar** — vive exclusivamente en el panel de cada proyecto (§6.10).

---

## 7. PIPELINE DE EXTRACCIÓN OPTIMIZADO (v1.4)

### 7.1 Principio: 1 sola copia de datos en todos los casos

La decisión de ruta se toma **antes** de la extracción, no después. Se analiza el MPLS con `mkvmerge --identify` y se compara con las pistas seleccionadas por el usuario para determinar si hay reordenación o exclusión.

### 7.2 Montaje del ISO

El ISO se monta via loop mount directo (ver §3.2):
```bash
mount -t udf -o ro,loop /mnt/isos/pelicula.iso /mnt/bd/pelicula_12345/
```

**Selección del MPLS principal:** `find_main_mpls()` busca el fichero de mayor tamaño (≥ 1 MB) en `BDMV/PLAYLIST/`.

### 7.3 Ruta A — Sin reordenación: intermedio + mkvpropedit

Si todas las pistas están incluidas y en el orden original del disco:

```
MPLS ──mkvmerge──▶ /mnt/tmp/intermedio.mkv (1 copia)
                         │
              mkvextract ─┤─▶ capítulos reales del disco
                         │
              mkvpropedit ─┤─▶ metadatos, flags, capítulos (O(1), sin copia)
                         │
                    mv ───▶ /mnt/output/final.mkv (rename instantáneo)
```

**Coste total: 1 copia de datos (~90 GB).** La operación mkvpropedit + mv es instantánea.

### 7.4 Ruta B — Con reordenación/exclusión: MPLS directo al final

Si el usuario excluyó pistas o reordenó su posición:

```
MPLS ──mkvmerge──▶ /mnt/output/final.mkv (1 copia)
         con --track-order, --audio-tracks,
         --subtitle-tracks, --track-name,
         --chapters, --title
```

**Coste total: 1 copia de datos (~90 GB).** Sin MKV intermedio. Sin segundo paso.

> **Mejora v1.4:** antes esta ruta hacía 2 copias (MPLS→intermedio→final = ~180 GB de I/O). Ahora es 1 copia directa.

### 7.5 Desmontaje del ISO

El ISO se desmonta **siempre** en `finally` (éxito o error). En la ruta directa, el ISO permanece montado durante toda la escritura al MKV final (read-only, sin riesgo).

### 7.6 Limpieza en caso de error

Si el pipeline falla, el MKV intermedio (si existe, solo en ruta A) se elimina de `/mnt/tmp`. El ISO se desmonta siempre.

### 7.7 Espacio en disco requerido

| Escenario | Espacio pico |
|-----------|-------------|
| ISO original (ya existente) | 70-100 GB |
| **Ruta A** (intermedio + propedit) | ~190 GB (ISO + intermedio) |
| **Ruta B** (MPLS directo) | ~190 GB (ISO + final) |

> **Mejora v1.4:** el pico máximo de la ruta con reordenación baja de ~280 GB a ~190 GB (se elimina el intermedio de ~90 GB).

---

## 9. PERSISTENCIA DE CONFIGURACIÓN

Cada sesión se almacena como un fichero JSON independiente en `/config/{session_id}.json`.

| Campo | Valor |
|-------|-------|
| **ID** | Generado automáticamente: `{título}_{año}_{timestamp_unix}` |
| **ISO de origen** | Ruta absoluta en `/mnt/isos` |
| **Configuración completa** | Todas las decisiones de Fase B y ediciones de Fase C |
| **Estado** | `pending` → `queued` → `running` → `done` \| `error` |
| **Fecha de creación** | Timestamp UTC |
| **Fecha de última ejecución** | Timestamp UTC |
| **Inicio de ejecución** | Timestamp UTC (para calcular duración) |
| **Ruta del MKV final** | Ruta absoluta del MKV generado (tras Fase E) |
| **Log de ejecución activa** | Líneas de output en tiempo real, persistidas para reconexión |
| **Historial de ejecuciones** | `execution_history: list[ExecutionRecord]` — ver §9.1 |

### 9.1 ExecutionRecord

Cada ejecución (éxito o error) se registra como un registro inmutable:

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `run_number` | int | Número secuencial (1-based) |
| `started_at` | datetime | Inicio de la ejecución (UTC) |
| `finished_at` | datetime? | Fin de la ejecución (UTC) |
| `status` | str | `"done"` o `"error"` |
| `error_message` | str? | Detalle del error |
| `output_mkv_path` | str? | Ruta del MKV generado (solo done) |
| `phase_elapsed` | dict | `{"mount": 2.3, "extract": 345.6, "unmount": 1.2, "write": 12.4}` — segundos por fase |
| `output_log` | list[str] | Copia completa del log |

Los tiempos por fase se calculan en el backend (`_run_pipeline`) con `datetime.now()` al inicio/fin de cada fase y se almacenan como float (segundos, 1 decimal).

**Timestamps:** siempre UTC-aware (`datetime.now(timezone.utc)`). Nunca `datetime.utcnow()` (genera timestamps sin zona horaria).

**Cola de ejecución:** el estado de la cola se persiste en `queue_state.json` dentro de `/config/`. Al reiniciar, las sesiones zombie (`running`/`queued`) se resetean a `pending` con mensaje explicativo.

**Log de ejecución:** cada línea lleva timestamp `[HH:MM:SS]` (UTC) excepto las líneas `Progress: XX%` que el frontend parsea para la barra de progreso. Se almacenan en `output_log[]` y se envían por WebSocket. Al reconectar, el histórico se reenvía completo.

**Logging de errores:** ficheros JSON corruptos en `/config/` se logean como warning (nunca silenciados). `settings.json` y `queue_state.json` se excluyen de `list_sessions()`.

La configuración puede recuperarse en cualquier momento para revisión, edición y relanzamiento del proceso completo.

---

## 10. ENTORNO DOCKER

### 10.1 Privileged mode

El contenedor requiere `privileged: true` para poder ejecutar `mount -t udf -o ro,loop`. Sin esta opción, los loop devices no están accesibles. Esto es aceptable para un contenedor de propósito único en un NAS no expuesto a internet.

### 10.2 Health check

- Endpoint: `GET /api/health` → `{"status": "ok"}`
- docker-compose: `healthcheck` con `curl -f http://localhost:8080/api/health` cada 30s, 3 retries, 15s start_period.

### 10.3 Entrypoint

`entrypoint.sh` valida al arrancar:
1. Volúmenes montados: `/mnt/isos`, `/mnt/output`, `/mnt/tmp`, `/config` (exit 1 si falta alguno).
2. Permisos de escritura en `/mnt/output`, `/mnt/tmp`, `/config` (exit 1 si no escribible).
3. Crea `/mnt/bd` (directorio para mount points de ISOs).
4. Ejecuta `modprobe udf` (no fatal si falla — el módulo puede estar ya cargado).

### 10.4 Recuperación al arrancar

`_recover_interrupted_sessions()` se ejecuta siempre al importar `main.py` (no solo en DEV_MODE). Resetea todas las sesiones con `status in ("running", "queued")` a `"pending"` con `error_message = "Sesión interrumpida por reinicio del servidor"`.

### 10.5 Eliminaciones respecto a v1.3 anterior

- **API QTS File Station** (`phases/qts.py`): eliminada. Reemplazada por `phases/iso_mount.py`.
- **Variables de entorno QTS**: `QNAP_HOST`, `QNAP_USER`, `QNAP_PASS`, `QNAP_PORT`, `QNAP_ISO_SHARE`, `SHARE_BASE` — ya no necesarias.
- **Volumen `/share`**: eliminado de docker-compose (ya no se necesita acceso a shares QTS).
- **Endpoints eliminados**: `GET /api/settings`, `GET /api/settings/test-connection`.
- **Frontend eliminado**: badge QTS en cabecera, modal de ajustes QTS.
