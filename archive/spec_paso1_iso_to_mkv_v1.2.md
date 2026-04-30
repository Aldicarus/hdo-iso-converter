# ESPECIFICACIÓN TÉCNICA — PASO 1: ISO UHD → MKV
*Versión 1.2 — Docker/QNAP + optimización de I/O*

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
| **BDInfoCLI** (tetrahydroc) | última | `tetrahydroc/BDInfoCLI` — binario .NET 8 nativo Linux x64 | Análisis del disco — lee ISO directamente |
| **makemkvcon** | 1.17+ | Linux x64 binario oficial | Extracción — lee ISO directamente |
| **mkvmerge** | 84+ | Repositorios Linux estándar | Remux final (solo si hay reordenación) |
| **mkvpropedit** | 84+ | Repositorios Linux estándar (incluido en MKVToolNix) | Edición metadata in-place sin copia |
| **ffmpeg** | 6.0+ | Repositorios Linux estándar | Extracción HEVC para dovi_tool |
| **dovi_tool** | 2.1+ | Binario Linux x64 — GitHub releases | Manipulación RPU Dolby Vision |

> **Nota sobre BDInfo:** la versión GUI de BDInfo no es compatible con entornos headless. Se usa `BDInfoCLI` (fork de tetrahydroc), que está compilado en .NET 8 nativo sin dependencia de Mono y tiene soporte completo para ISOs UHD y Docker.
> Repositorio: `https://github.com/tetrahydroc/BDInfoCLI`

---

## 3. FLUJO DE EJECUCIÓN

```
ISO en NAS (read-only, nunca se mueve)
     │
     ├──► [FASE A] BDInfoCLI lee ISO directamente → report
     │
     ↓
[FASE B] Aplicación de reglas automáticas
     ↓
[FASE C] Pantalla de revisión (edición opcional)
     ↓
[Confirmar]
     ↓
[FASE D] makemkvcon lee ISO directamente
         + selección de pistas via --profile
         → MKV intermedio en /mnt/tmp
     ↓
[FASE E] Decisión de método de escritura final:
         ┌─ Sin reordenación → mkvpropedit in-place (0 copia adicional)
         └─ Con reordenación → mkvmerge → MKV final → borrar intermedio
     ↓
MKV final en /mnt/output
```

### 3.1 Principios de optimización de I/O

Los ficheros ISO de 70-100 GB implican tiempos de lectura/escritura significativos en discos spinning. Las reglas que minimizan las operaciones de I/O son:

1. **El ISO nunca se copia.** BDInfoCLI y makemkvcon leen el ISO directamente mediante el prefijo `iso:` y volume mount en Docker.
2. **makemkvcon filtra pistas en origen.** Mediante `--profile` solo extrae las pistas necesarias, reduciendo el tamaño del MKV intermedio.
3. **mkvpropedit cuando no hay reordenación.** Edita metadata, flags y capítulos directamente sobre el MKV intermedio sin crear una segunda copia de 90 GB.
4. **mkvmerge solo cuando es imprescindible.** Únicamente si la Fase C determina que el orden de pistas difiere del orden en que las extrae MakeMKV.

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

Si `AUDIO_DCP = true` y el codec detectado es `TrueHD Atmos` → añadir el sufijo ` (DCP 9.1.6)` al final del literal. Solo aplica a este codec.

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
| TrueHD Atmos 7.1 en English + Audio DCP | `Inglés TrueHD Atmos 7.1 (DCP 9.1.6)` |
| TrueHD Atmos 5.1 en English + Audio DCP | `Inglés TrueHD Atmos 5.1 (DCP 9.1.6)` |
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

**Regla de capítulos ausentes:** si el disco no contiene ningún capítulo → generar capítulos estándar automáticamente cada 10 minutos a partir de `00:00:00.000`, hasta cubrir la duración total de la película. Numeración `Capítulo 01`, `Capítulo 02`... Los capítulos estándar se marcan con la indicación `"Capítulos estándar generados automáticamente (no presentes en el disco)"` en la pantalla de revisión.

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

Botón **"Confirmar y ejecutar"** → lanza automáticamente Fase D y Fase E sin más intervención.

---

## 7. FASE D — EXTRACCIÓN CON MAKEMKVCON (automático)

### 7.1 Lectura directa del ISO

`makemkvcon` lee el ISO directamente mediante el prefijo `iso:` sin necesidad de montarlo ni copiarlo:

```bash
makemkvcon \
  --profile="{perfil_generado}" \
  mkv iso:/mnt/isos/pelicula.iso \
  0 \
  /mnt/tmp/
```

El perfil `--profile` se construye dinámicamente a partir de las pistas seleccionadas en la Fase C, de modo que solo se extraen las pistas necesarias. Esto reduce el tamaño del MKV intermedio y el tiempo de extracción.

MakeMKV convierte automáticamente la estructura DTDL (BL + EL separados) a STDL (BL+EL+RPU interleaveado en una pista), preservando la FEL intacta.

### 7.2 Salida

El resultado es un MKV intermedio en `/mnt/tmp/` que contiene únicamente las pistas seleccionadas en el orden original del disco.

---

## 8. FASE E — ESCRITURA FINAL (automático)

La Fase C determina si el orden de pistas del MKV intermedio coincide con el orden final deseado. En función de esto se elige el método de menor coste de I/O:

### 8.1 Caso A — Sin reordenación de pistas: mkvpropedit in-place

Si las pistas del MKV intermedio ya están en el orden correcto (o no hay reordenación), `mkvpropedit` aplica todos los cambios de metadata directamente sobre el fichero sin crear una copia:

```bash
mkvpropedit /mnt/tmp/pelicula_intermedio.mkv \
  --edit track:1 --set name="Castellano TrueHD Atmos 7.1" --set flag-default=1 \
  --edit track:2 --set name="Inglés TrueHD Atmos 7.1" --set flag-default=0 \
  --edit track:3 --set name="Castellano Forzados PGS" --set flag-forced=1 --set flag-default=1 \
  --edit track:4 --set name="Inglés Completos PGS" --set flag-forced=0 --set flag-default=0 \
  --chapters /mnt/tmp/chapters.xml
```

Después se mueve el fichero al destino final:
```bash
mv /mnt/tmp/pelicula_intermedio.mkv /mnt/output/Título\ \(Año\)\ \[DV\ FEL\].mkv
```

**Coste:** 0 copias adicionales. Solo operación de rename/move (instantánea si origen y destino están en el mismo volumen NAS).

### 8.2 Caso B — Con reordenación de pistas: mkvmerge

Si la Fase C establece un orden de pistas diferente al del MKV intermedio, `mkvmerge` crea el MKV final con el orden correcto:

```bash
mkvmerge \
  -o /mnt/output/Título\ \(Año\)\ \[DV\ FEL\].mkv \
  --title "Título (Año)" \
  --track-order 0:0,0:2,0:1,0:3,0:4 \
  --edit-track "0:0" --set name="..." \
  /mnt/tmp/pelicula_intermedio.mkv
```

Tras completarse, el MKV intermedio se borra:
```bash
rm /mnt/tmp/pelicula_intermedio.mkv
```

**Coste:** 1 copia completa adicional (~90 GB). Inevitable cuando hay reordenación.

### 8.3 Espacio en disco requerido durante el proceso

| Fase | Espacio necesario |
|------|------------------|
| ISO original (ya existente) | 70-100 GB |
| MKV intermedio (Fase D) | ~90 GB en /mnt/tmp |
| MKV final (Caso A — move) | 0 GB adicionales |
| MKV final (Caso B — mkvmerge) | ~90 GB en /mnt/output simultáneamente con el intermedio |
| **Pico máximo (Caso B)** | **~280 GB** (ISO + intermedio + final) |
| **Pico máximo (Caso A)** | **~190 GB** (ISO + intermedio, luego move) |

---

## 9. PERSISTENCIA DE CONFIGURACIÓN

Cada ejecución genera automáticamente una entrada persistente con:

| Campo | Valor |
|-------|-------|
| **ID** | Generado automáticamente: `{título}_{año}_{timestamp}` |
| **ISO de origen** | Ruta completa |
| **Configuración completa** | Todas las decisiones de Fase B y ediciones de Fase C |
| **Estado** | `pendiente` / `ejecutado` / `error` |
| **Fecha de creación** | Timestamp automático |
| **Fecha de última ejecución** | Timestamp automático |

La configuración puede recuperarse en cualquier momento para revisión, edición y relanzamiento del proceso completo.
