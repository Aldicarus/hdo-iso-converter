# Correcciones del castellano — hechas, y lo que se decidió no hacer

Durante la traducción a inglés y catalán se anotó aquí todo lo que era un
arreglo del **castellano** y no de la traducción, para hacerlo al final en su
propio commit. El motivo: `test_castellano_intacto.py` vigila el original byte
a byte contra el golden de `pre-i18n`, y tocarlo a la vez que se traduce haría
indistinguible un arreglo deliberado de un fallo del refactor — que es justo
lo que el golden existe para distinguir.

Están todas hechas. El fichero se queda como registro de **por qué** cada una
era un problema y de las cosas que se decidió dejar como estaban, que son las
que un lector futuro va a querer entender.

---

## 1. El manual citaba una pestaña con su nombre caducado

Decía `Editar Propiedades MKV`; esa pestaña se llama **`Consultar / Editar
MKV`** desde que Tab 2 dejó de ser solo edición y pasó a incluir la
radiografía DV+HDR.

Se detectó al traducir la sección, contrastando los nombres que el manual cita
contra los de `index.html`. El nombre sale ahora del mismo sitio que la
interfaz (`ui.consultar_editar_mkv`), en las tres lenguas.

---

## 2. Frases partidas por marcado en línea

El extractor ve un nodo de texto por trozo, así que
`El repositorio <strong>DoviTools</strong> lo mantiene <strong>R3S3T_9999</strong>…`
salía como **tres claves**. Eso traduce por fragmentos por la puerta de atrás,
que `REGISTRO.md` prohíbe de frente: quien traduce un trozo sin ver los otros
no puede mover el orden de las palabras —y en inglés el orden cambia justo
alrededor del énfasis— y quien edita el castellano de uno deja los demás
descolgados.

**Medido**: 41 bloques y 105 claves, buscando las cuyo valor no puede
ENCABEZAR una frase (empieza en minúscula o en signo de puntuación) y tienen
otra clave cerca.

**Hechos 24**, cada uno como UNA clave con `data-i18n-html` y el marcado en
línea dentro del valor. El caso que motivó la nota lo demuestra: en inglés los
dos `<strong>` se intercambian, porque el castellano topicaliza el repositorio
con un clítico (`lo mantiene`) que el inglés no tiene. Con tres claves eso no
se podía escribir.

Se purgaron las claves-fragmento que quedaron huérfanas.

### Los cinco que se quedan partidos, y por qué

- **La leyenda de confianza del modal de series** intercala puntos de color
  **con su propio `data-i18n-tip`** entre los trozos de prosa. Fusionarla
  metería clases de presentación y **otra clave de i18n** dentro de una cadena
  traducible: el traductor tendría que no tocar un `data-i18n-tip` incrustado,
  y `pintarTextos` escribiría el `innerHTML` de un nodo que el observador
  volvería a mirar. Es peor que el fragmento.
- **`ui.opcional_2`** (×2) no es la cola de una frase: es el chip «opcional»
  junto a un título, y se reutiliza en dos secciones de ⚙︎ Configuración.
- **`tab2.peak` / `tab2.avg` / `tab2.nits`** son rótulos del gráfico de
  luminancia. Van en minúscula porque son nombres de campo del RPU, no prosa.

Lo fija `TestNingunaFraseSePartePorMarcado`, con la lista y el motivo de cada
uno, más un test que falla si una entrada de la lista deja de existir y otro
que prohíbe que un valor traducible lleve otra clave de i18n dentro.

---

## 3. Mensajes con una plantilla dentro de un `${…}`

Una plantilla anidada no se delimita con un regex —se corta en el primer
backtick de dentro— así que el extractor los intentó, `node --check` los
rechazó y se revirtieron solos: quedaron a medio extraer, con un trozo
traducido y el resto en castellano.

Los tres arreglados. Dos títulos de tarjeta pasan a `data-i18n`, y el aviso
del desfase del sheet se reescribe sacando la ternaria a un `const`: hoy es
UNA clave con tres parámetros.

**Cómo se detectó**: aplicando cada mensaje de uno en uno y preguntándole a
`node --check` si el fichero seguía siendo válido. Sin ese bucle, dos ficheros
se habrían commiteado roto.

---

## 4. Parámetros con castellano cableado

El extractor traduce la PLANTILLA, no lo que se le interpola, así que un valor
construido con un ternario de literales castellanos se cuela por el hueco y
sale **en castellano dentro de una frase inglesa**.

Seis sitios: el descriptor del filtro del browser, el adjetivo de
«Corrección», el «ninguna fase», la fila del sheet, y en el servidor la acción
que nombra el 409 y el ETA de la línea de progreso.

**Dos de los seis que esta nota listaba ya estaban resueltos** —el puesto en
la cola y la nota del L2—: la entrada se había quedado vieja.

Lo encontró el traductor de los mensajes con parámetros, mirando de dónde
venía cada `{…}`. No traduciendo.

---

## 5. Plurales resueltos con un sufijo de una letra

`{p2}` = `'s'`/`''` y `{p3}` = `'n'`/`''` pluralizan en castellano por
coincidencia ortográfica, y eso no se traduce:

- **el inglés no tiene ninguna palabra que pluralice con una `n`** — escribía
  «not foundn»;
- y el catalán tampoco cuando el plural es irregular (`dia` → **dies**, no
  `dia+s`).

Los tres —y son los tres únicos del repo— pasan a dos claves `_uno`/`_varios`.
El castellano renderizado no cambia: sigue saliendo «1 saltado (ya existía)» y
«3 saltados (ya existían)».

El sufijo **sí** se conserva donde el plural es regular en las tres lenguas
(fichero/files/fitxers, proyecto/projects/projectes, episodio/episodes/episodis):
ahí no es una coincidencia, es la regla.

---

## 6. Cadenas del servidor sin extraer

- el separador `' o '.join(...)` de una lista, que salía en castellano dentro
  de una frase inglesa;
- el fallback `'la serie'` de un parámetro;
- y una **tercera que salió al arreglarlas**: `_que` de
  `create-series-sessions` era una segunda copia de la misma frase, sin
  extraer, así que la descripción del trabajo en la columna salía siempre en
  castellano aunque la clave existiera.

---

## 7. La cola de fragmentos cortos, y los locales

Esta no estaba en la lista: salió al aplicar las otras.

`captura.es_frase` exige seis caracteres, **dos palabras** y un acento o una
palabra función. Eso deja fuera justo los rótulos cortos pegados a un dato:
`hace ${mins} min`, `${n} escenas`, `Crear ${n} proyecto${s}`,
`Temporada ${n}`, `Movido a: ${ruta}`. Eran **40 claves** que salían en
castellano con la app en inglés, y **no las veía ningún guard**: el de
castellano suelto porque el umbral las descarta, y el golden porque se capturó
con el mismo umbral.

Y **16 `toLocaleDateString('es-ES')` cableados**, así que las fechas y los
miles seguían en formato español en las tres lenguas. No es texto, así que
ningún guard de traducción lo miraba. Hoy salen de `localeActual()`, un solo
sitio, con `en-GB` para el inglés —el día antes del mes, como en las otras
dos— y no `en-US`.

### Los volcados de diagnóstico se quedan en castellano

Cuatro funciones producen texto para depurar, no interfaz: el modal
**🔬 Datos ISO** de Tab 1, su equivalente de Tab 2, el Markdown que la
radiografía DV+HDR copia al portapapeles, y los cinco bloques de la card
**🛡️ Validaciones** con su cabecera.

Son etiquetas como `raw: lang=`, `── Pistas descartadas ──`, `cuerpo 97,4%` o
`· sync +16`, que se leen contra el log y contra la hoja de DoviTools —las dos
en inglés— y se pegan en un informe. Traducirlas añadiría ~38 claves que nadie
mira salvo cuando algo va mal, y cambiaría el texto que el usuario comparte.
Es el mismo criterio que con los markers del log.

Lo fija `TestNoQuedaNingunFragmentoCortoSuelto`, que exime **por función** y no
por número de línea, con un segundo test que falla si una exención deja de
apuntar a código real. Y distingue un id de la prosa sin mantener listas: un
id no lleva ningún espacio.

---

## Lo que aprendió el golden

Tocar el castellano obligó a distinguir un arreglo de un cambio, y eso se
resolvió con **equivalencias** —reglas mecánicas que el test aplica a los dos
lados— y no con listas de excepciones, que habrían escondido justo lo que
vigila. Son cuatro:

| equivalencia | por qué |
|---|---|
| `{max}` ≡ `⟦⟧` | el mismo hueco escrito de dos formas |
| el prefijo `[Fase C]` fuera | el prefijo es del parser, la prosa del catálogo |
| `\n` ≡ el escape del fuente | en la plantilla eran dos caracteres; en JSON, un salto |
| un valor con marcado ≡ sus nodos de texto | las frases fusionadas se capturaron por trozos |

`EXCEPCIONES` solo tiene **seis** entradas, y las seis son cambios de forma
deliberados (los plurales partidos y dos mensajes absorbidos), cada una
diciendo en qué clave vive ahora la frase.

Comprobado por mutación en las cuatro: cambiar una palabra del castellano
—también dentro del marcado— sigue haciendo fallar el test.
