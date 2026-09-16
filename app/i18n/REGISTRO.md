# Tono, registro y glosario de la traducción

Esto no es documentación: lo aplica `test_registro_de_la_traduccion.py`. Si una
regla de aquí se incumple, la suite falla.

## El castellano es el original y no se toca

`es.json` contiene **exactamente** las frases que ya estaban en el código, byte
a byte. Lo vigila `test_castellano_intacto.py` contra un golden capturado sobre
la etiqueta `pre-i18n`. Traducir es producir `en` y `ca`; nunca "mejorar" el
castellano de paso.

## El tono del castellano, que es el que hay que reproducir

La app habla como un colega que sabe del tema, no como un manual y no como un
asistente. En concreto:

- **Segunda persona del singular, directa**: «Pega aquí tu clave», no «Se debe
  introducir la clave» ni «Por favor, introduzca».
- **Nada de cortesía ornamental**: no hay «por favor», «lo sentimos» ni
  «¡Atención!». Un error dice qué pasó y qué hacer.
- **Se explica el por qué cuando importa**, en la misma frase y sin
  condescender: «No se puede editar en la biblioteca porque está montada en
  solo lectura — se copiará a Output».
- **Frases cortas y afirmativas.** Donde el castellano usa raya (—) para
  añadir la consecuencia, la traducción también.
- **Sin mayúsculas de título**: en castellano «Nuevo proyecto», no «Nuevo
  Proyecto». En inglés **sí** se usa sentence case, no Title Case: «New
  project», no «New Project». El catalán sigue al castellano.
- **El log describe estado, no promete futuro** (regla ya establecida del
  proyecto). Al traducir se mantiene: `Extracting the enhancement layer`, no
  `Will extract…`.

## Inglés

- Sentence case en botones y títulos, como se dice arriba.
- Se conservan los nombres propios y comerciales tal cual: `Dolby Vision`,
  `TrueHD Atmos`, `DTS-HD MA`, `Blu-ray`, `mkvmerge`, `dovi_tool`.
- `Castellano` → **`Spanish`** (es el nombre del idioma, y ahí sí se traduce:
  decisión 2).
- `Forzados` → `Forced`; `Completos` → `Full`. Son los dos tipos de subtítulo
  y aparecen dentro del MKV.

## Catalán — la regla que pidió el usuario

**El término técnico que en castellano está en castellano se queda igual en
catalán; el que está en inglés, también.** El registro de esta app es el de
alguien que habla de vídeo, y en ese registro nadie cataluñiza el vocabulario
técnico: se dice *fer un remux*, no *remesclar*.

Lo que **no** se traduce al catalán (ni al inglés) está en `GLOSARIO` de
`test_registro_de_la_traduccion.py`, que comprueba que cada término aparezca
igual en las tres lenguas. Lo que **sí** se traduce son las palabras de la
lengua común: `pista`→`pista`, `carpeta`→`carpeta`, `fichero`→`fitxer`,
`nombre`→`nom`, `tamaño`→`mida`, `idioma`→`idioma`, `aviso`→`avís`.

**La forma verbal sigue al castellano: donde el original va en infinitivo, el
catalán va en infinitivo.** Decidido el 2026-09-16. Softcatalà prescribe el
imperativo para los botones —«Obre», «Desa»— y el castellano usa el
infinitivo, así que las dos son defendibles; el catálogo tenía **las dos**, y
de los 189 rótulos cuyo castellano empieza por infinitivo, 101 iban en
infinitivo y 88 en imperativo. Lo que decidió no fue la estética sino los
cruces que producía la mezcla: `Buscar película` era «Cerca la pel·lícula» y
`Buscar la película en TMDb` era «Buscar…», o sea el mismo verbo con dos
lexemas.

**Se espeja también el artículo**: «Limpiar artefactos» → «Netejar
artefactes», no «Netejar els artefactes». El imperativo pedía artículo para
sonar natural como orden; el infinitivo no lo necesita, y sin esta parte la
decisión no cierra nada. Lo aplica
`test_calidad_de_la_traduccion::test_donde_el_castellano_va_en_infinitivo_el_catalan_tambien`.

Casos decididos, para que no se resuelvan dos veces:

| castellano | catalán | por qué |
|---|---|---|
| ripeo / ripear | **ripeig / ripar** | está en el registro hablado; *extracció* suena a manual |
| remux | **remux** | término del oficio, invariable |
| bin | **bin** | el fichero `.bin` del RPU, invariable |
| playlist | **playlist** | invariable; *llista de reproducció* es otra cosa |
| forzados | **forçats** | lengua común, se traduce |
| completos | **complets** | lengua común, se traduce |
| Castellano | **Castellà** | nombre de idioma |
| capítulos | **capítols** | lengua común |
| huérfano | **orfe** | lengua común |
| cola | **cua** | lengua común |
| pista | **pista** | igual en las dos lenguas |

## Interpolación: mensajes con parámetros con nombre

Prohibido partir una frase en trozos. Mal:

```js
`Máximo ${MAX} proyectos abiertos. Cierra uno antes de abrir otro.`
```

Bien:

```js
t('tab1.max_proyectos', { max: MAX })
// es: "Máximo {max} proyectos abiertos. Cierra uno antes de abrir otro."
```

El orden de los parámetros puede cambiar entre lenguas, y por eso van **con
nombre y nunca por posición**. Los plurales se resuelven con dos claves
(`…_uno` / `…_varios`), no con `${n !== 1 ? 's' : ''}`.

## Lo que no se traduce nunca, en ninguna lengua

Lo fija `test_registro_de_la_traduccion.py`:

- **`Season NN`** del nombre de carpeta de serie: es convención de
  Plex/Jellyfin y traducirlo rompe el scraper.
- **Los tags del nombre del fichero**: `[DV FEL]`, `[Audio DCP]`, `[CMv4 FULL]`,
  `[CMv4 CORE+]`, `[CMv4 CORE]`, `[CMv4.0]`.
- **Los markers del log**: `━━━`, `✓ Fase`, `✗ Fase`, `📋 Plan`, `🎯 Resultado`,
  `🛑 Cancelado`, `ℹ️ Auto`, `ℹ️ Forward`, `§§PROGRESS§§`, `Progress:`, `$ `.
  Son claves de parser y de persistencia. Se traduce lo que va **detrás**.
- **Los nombres comerciales de codec** (`CODEC_TIER_NAMES`): `TrueHD Atmos`,
  `DD+ Atmos`, `DTS-HD MA`, `DTS`, `DD`, `PCM`, `FLAC`.
- **El sufijo `(DCP 9.1.6)`**, que además va siempre en la pista castellana
  (decisión 5): describe esa pista, no el idioma de la interfaz.
- Nombres de herramienta, endpoint, fichero y variable.
