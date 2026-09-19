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

---

## La auditoría del 2026-09-16: los bugs que dejó la migración, y no son de traducción

La auditoría completa —pedida porque «el resultado dista mucho de ser ni una
primera release»— encontró cuatro cosas que **no** son de traducción: son
marcado roto que llevaba así desde la migración y que nadie veía porque el
HTML seguía siendo válido.

### El tooltip del gráfico de luminancia no existía

`tab2.js` tenía, literalmente:

```js
<div class="dv-<span data-i18n="tab2.sparkline_tooltip_s"></span>
     tyle="display:none"></div>
```

El original —verificado contra `pre-i18n`— era
`<div class="dv-sparkline-tooltip" style="display:none">`. Una sustitución a
máquina cogió el trozo `sparkline-tooltip" s`, que para un regex parece texto,
y lo reemplazó **dentro del valor del atributo**: partió el `class` y se comió
la `s` de `style`.

Lo que hace este caso instructivo es que **no falla nada**. `node --check`
pasa, el navegador acepta el marcado y no hay ni un error en consola; lo único
que ocurre es que `host.querySelector('.dv-sparkline-tooltip')` no encuentra
nada y el tooltip del hover **nunca aparece**. Es el error de los regex otra
vez: reescribir dentro de un hueco no rompe, contesta otra cosa.

Guard: ningún valor de atributo puede contener una etiqueta (con la excepción
de un `data:` URI, que es el favicon).

### Diez atributos escribían el nombre de una función

`data-tooltip=tr('workbar.detener_este_trabajo')`, sin `${…}` y sin comillas.
Dentro de una plantilla, `tr()` solo se evalúa si va en un hueco; sin él es
texto, y el navegador se queda con `tr('workbar.detener_este_trabajo')` como
valor del atributo. En los diez sitios la forma correcta no era interpolar
sino `data-i18n-tip="clave"`, que es declarativa y la resuelve el observador.

### Y un espacio que se perdió en 21 sitios

Cosechar los literales normalizando el espacio (`" ".join(v.split())`)
convierte `'Lleva '` en `'Lleva'`, y la sustitución dejó «Lleva7 s». El
espacio va **fuera** del `tr()`: en el catálogo un espacio en el borde es
invisible y hay un guard que lo prohíbe justamente porque se pierde.

### Y seis `<span>` con una clave que no existe

La misma pasada que partió el `class` del sparkline pegó
`<span data-i18n="tab3.x"></span>` detrás del `</div>` final de cinco
plantillas —dos en `core.js` (la tarjeta de proyecto de las tres columnas),
una en `tab2.js` (el propio sparkline) y tres en `tab3.js` (la tabla de los
dos RPU lado a lado y el log del veredicto)—.

**No se veían, y el motivo es lo interesante**: la cosecha había metido
`tab3.x` en los tres catálogos **con el valor vacío**, así que `pintarTextos`
escribía nada y el HTML seguía siendo válido. Un defecto tapado por otro. Al
quitar esa clave —que no es una cadena de interfaz y no tiene nada que
traducir— `pintarTextos` pasó a hacer lo que hace con una clave ausente,
escribir la clave, y en pantalla se leía `tab3.xtab3.x` en los tres idiomas.

El guard en vivo no lo vio, y el motivo importa: lee `clavesAusentes()` tras
abrir `index.html` en Chrome, así que solo conoce las claves de lo que está
pintado — e `index.html` a secas no renderiza ninguna plantilla del JS. Lo
cazó `test_las_tres_lenguas_en_pantalla`, que sí abre los paneles con datos.
El guard que las habría cazado sin depender de que alguien renderice ese panel
es estático y ya está puesto: `TestNingunaClavePedidaFaltaDelCatalogo`.

---

## Defectos DEL CASTELLANO que la lectura por pantalla destapó

Van aquí y **no se arreglan en `es.json`**: el castellano es el original y lo
vigila un golden. Se anotan para hacerlos aparte, en el código, cuando el
usuario lo decida — lo que se corrige de paso es la traducción, que reproduce
el defecto fielmente porque para eso está.

| clave | qué dice el castellano | qué pasa |
|---|---|---|
| `core.se_recalcula_automaticamente_al_cambiar_los` | «Se recalcula automáticamente al cambiar los **toggles**» | **los toggles ya no existen.** Los sustituyeron las dos tarjetas informativas del disco (Dolby Vision y Vídeo · HDR), y con ellos se fue `recalcMkvNameLocal`: el nombre lo construye solo el backend. El tooltip describe una interfaz que se retiró. |
| `core.mkvpropedit_in_place_solo_ruta_sin` | «mkvpropedit in-place (solo ruta sin reordenación, **— en ruta directa**)» | frase **colgando**: falta lo que iba después de la raya. Se lee como si el paréntesis se hubiera cortado a medias, y las tres lenguas lo reproducen igual porque es lo fiel. |

| `tab1.paso_2_elige_el_origen_un` · `…_varios` | «y **púlsa** Analizar» | **falta de ortografía**: `pulsa` es llana, no lleva tilde. Está en las dos variantes del mismo paso, así que se escribió una vez y se copió. |
| `cmv40_modals.borrar_artefactos_de_proyecto` | «**Borrar** artefactos de {p1} proyecto{p2}**?**» | le falta el **«¿» de apertura**. Sus once hermanos de diálogo lo llevan, y sin él la frase parece un rótulo y no una pregunta — hasta el punto de que el guard de la forma verbal catalana la tomaba por un botón. |

| `tab1.iniciando_extraccion_sigue_el_progreso_en` · `tab1.anadido_a_la_cola_en_posicion` · `tab1.monitoriza_el_progreso_en_el_panel` | «Sigue el progreso en **"Trabajos en Curso"**» | **el panel ya no existe.** Se movió al modal de detalle cuando llegó la columna de trabajo, y los comentarios de `workbar.js` lo dicen: «Al retirar el panel «Trabajos en Curso» de Tab 1 se fue con él…». Tres mensajes mandan al usuario a un sitio que no va a encontrar. El destino de hoy es la columna de trabajo. |
| `tab1.trabajos_en_curso` | «Trabajos en Curso» | **clave huérfana**: la única referencia que queda está dentro de un comentario (`tab1.js:4501`). Y de paso lleva **mayúsculas de título**, que el REGISTRO prohíbe («Nuevo proyecto», no «Nuevo Proyecto»). |
| `tab2.error_en_analisis` | «Error en **analisis**: {e}» | **falta la tilde** de «análisis». Es la única del catálogo: un barrido de las palabras que siempre la llevan da este caso y nada más — los otros dieciocho candidatos eran nombres de parámetro (`{titulo}`, `{posicion}`) o el término inglés «CM version». |
| `ajustes.idioma.nota_pistas` | «El idioma **también decide** con qué perfil de pistas nacen los proyectos nuevos y en qué idioma se escriben los nombres de pista dentro del MKV.» | **RESUELTA el 2026-09-17**: ya es exacta. El perfil por idioma está implementado (`phase_b.idiomas_preferidos`) y los nombres de pista siguen el idioma. Se queda aquí como registro de que el texto llegó antes que el código. Decía: `phase_b` sigue con `filtered` clavado a Castellano y `LANGUAGE_MAP` sin traducir — está decidido y medido, pero no implementado (ver el final de la sección de i18n en CLAUDE.md). Es la regla de «describir estado, no predecir futuro» aplicada a la interfaz, y el texto se queda corto de tiempo: al leerlo hoy, el usuario espera un comportamiento que no va a ver. |
| `tab1.subtitulos_adaptado_pistas` | «── Subtítulos **adaptado** ({p1} pistas) ──» | **falta de concordancia**: «adaptados». Las dos traducciones la arreglan sin decir nada, porque en su lengua la concordancia es obligatoria y no hay forma de reproducir el error. |

Las dos primeras son de la misma familia que el «(audit #13)» de un mensaje de usuario:
texto que describió bien algo que después cambió, y que nadie volvió a leer
porque leer el catálogo entero por pantalla no se había hecho nunca.

## Lo que se decidió NO traducir, y dónde está escrito

La decisión del usuario fue «interfaz sí, diagnóstico no», y al aplicarla
aparecieron dos matices que conviene dejar por escrito:

- **Las cabeceras de la card 🛡️ Validaciones sí se traducen**, aunque los
  cinco bloques de contenido no. Son navegación, y estaban a medias: ①
  traducida y ②-⑤ no, que es peor que cualquiera de las dos opciones.
- **`LANGUAGE_MAP` no se toca.** `spanish: 'Castellano'` no es texto de
  interfaz: es el literal de pista de la spec y acaba en el nombre de las
  pistas del MKV. Que siga el idioma de la app es una decisión distinta y va
  con el bloque de selección de pistas, que sigue pendiente.

## 8. La preposición y el artículo no contraen: «extraídos de el MPLS»

Lo destapó el usuario el 2026-09-17 leyendo la ficha de un proyecto antiguo
de Tab 1: **«8 capítulos extraídos de el MPLS del episodio»**. No es un
defecto de la migración — el fuente de `pre-i18n` ya decía
`f"{len(chapters)} capítulos extraídos de {ep_origin_label}"` con
`ep_origin_label = "el MPLS del episodio"`.

La causa es estructural y no se arregla con una tilde: **el fragmento lleva
el artículo y la plantilla lleva la preposición**, así que se encuentran sin
contraer. Y el mismo fragmento se usa con DOS preposiciones distintas
(`de {origen}` en dos mensajes y `a {origen}` en otros dos), así que no se
puede mover la preposición al fragmento: `de` + `el` da `del` pero `a` + `el`
da `al`.

Medido con un detector sobre el AST —las 97 composiciones en las que un
`tr()` rellena un parámetro de otro `tr()`, renderizadas y buscando
`de el` / `a el` (y en catalán también `de els` / `per el`)— salen **once**:

| lengua | rendido | dónde |
|---|---|---|
| es | `{n} capítulos extraídos de el MPLS del episodio` | `tab1.py:1922` |
| es | `{n} capítulos extraídos de el fichero M2TS` | `tab1.py:1922` |
| es | `No se pudo determinar la duración de el disco` | `tab1.py:890` |
| es | `No se pudo determinar la duración de el fichero M2TS` | `tab1.py:890` |
| ca | `{n} capítols extrets de el fitxer M2TS` | `tab1.py:1922` |
| ca | `Sense capítols a el disc — generats automàticament…` | `tab1.py:885` |
| ca | `Sense capítols a el fitxer M2TS — generats automàticament…` | `tab1.py:885` |
| ca | `Sense capítols a el fitxer M2TS — generats cada 10 min` | `tab1.py:1928` |
| ca | `No s'ha pogut determinar la durada de el disc` | `tab1.py:890` |
| ca | `No s'ha pogut determinar la durada de el fitxer M2TS` | `tab1.py:890` |
| ca | `Enquadrament VARIABLE a el bin — típic d'un màster…` | `cmv40_pipeline.py:3176` |

**Ojo con el detector**: `de los` NO contrae en castellano, así que las dos
apariciones de «merge selectivo de los levels» de `cmv40_pipeline` son
falsos positivos. El patrón correcto en castellano es solo `de el` y `a el`.

**El arreglo es partir las claves**, una por (mensaje × origen) —nueve
claves en lugar de cuatro plantillas más tres fragmentos—, que es el mismo
criterio que ya se aplicó a los plurales irregulares (`_uno` / `_varios`, §5)
y por la misma razón: un hueco no puede llevar dentro algo que cambie la
palabra de al lado. Reordenar la preposición no vale, porque
`de l'MPLS` y `del fitxer` no salen de la misma plantilla — el catalán elide
ante vocal y `MPLS` empieza por una.

**APLICADO** el 2026-09-17 con el visto bueno del usuario. Las cuatro
plantillas de `tab1` y la del encuadre variable pasan a **trece claves
completas**, una por (mensaje × origen), y el código compone el sufijo
desde el id del origen (`tr(f'tab1.cap_sin_duracion_{origen_clave}')`) en
vez de interpolar un rótulo — el patrón que ya usaban `cmv40.fase_{fase}`
y `cmv40_pipeline.workflow_label_{…}`.

Lo que cambia y lo que no:

- **el castellano cambia en cuatro renders**, y solo para contraer: «de el
  disco» → «del disco», «de el fichero M2TS» → «del fichero M2TS», «de el
  MPLS del episodio» → «del MPLS del episodio». Los que iban con `en` ya
  eran correctos y se quedan **byte a byte** igual;
- **el inglés no cambia en ninguno**: las trece claves rinden exactamente lo
  que rendía la composición;
- **el catalán se arregla en siete**, con las dos contracciones que la
  lengua obliga (`de`+`el` → `del`, `a`+`el` → `al`) y respetando la elisión
  ante vocal, que es lo que hacía imposible resolverlo con una plantilla:
  `de l'MPLS` y `del fitxer` no salen de la misma.

`golden_castellano.json` solo tenía capturada **una** de las cinco —la del
encuadre variable, `[Fase B] Encuadre VARIABLE en ⟦⟧ — …`— y va con su
entrada en `EXCEPCIONES`. Las cuatro de `tab1` nunca entraron en el golden:
son de las que el detector de entonces no veía, porque se **asignaban** a
`chapters_reason` en vez de pasarse a una llamada.

**Y de paso salió una fuga viva**: el `"el BD"` de
`cmv40_pipeline.py:3173` era un literal castellano cableado —dos palabras
sin acento, así que `es_frase` no lo veía— que se colaba dentro de la frase
en las tres lenguas. Con la partición desaparece.

**Ya no puede volver**:
`test_calidad_de_la_traduccion::TestLaPreposicionYElArticuloContraen`
recorre el AST del servidor buscando un `tr()` cuyo parámetro con nombre sea
otro `tr()` —85 composiciones directas hoy—, renderiza cada par en castellano
y en catalán y busca la contracción perdida. No es una lista de sitios, así
que un mensaje nuevo con la misma forma cae igual. Verificado por mutación.


## 9. Los nueve defectos del castellano, APLICADOS

Aplicados el 2026-09-17 con el visto bueno del usuario, después de las once
contracciones de §8. Van todos con su entrada en `EXCEPCIONES` del golden,
porque cambian el castellano que se ve — que es exactamente por lo que ese
guard existe.

| clave | decía | dice |
|---|---|---|
| `core.se_recalcula_automaticamente_al_cambiar_los` | «al cambiar los **toggles**» | «Lo construye el análisis con las reglas de nombrado; si lo editas, se respeta» |
| `tab1.iniciando_extraccion_…` · `…anadido_a_la_cola…` · `…monitoriza_el_progreso…` | «Sigue el progreso en **"Trabajos en Curso"**» | «…en la **columna de trabajo**» |
| `tab1.trabajos_en_curso` | «Trabajos en Curso» | **borrada** |
| `tab2.error_en_analisis` | «Error en **analisis**» | «Error en **análisis**» |
| `core.mkvpropedit_in_place_solo_ruta_sin` | «(solo ruta sin reordenación, **— en ruta directa)**» | «solo en la ruta sin reordenación; **en la ruta directa no se ejecuta**» |
| `tab1.paso_2_elige_el_origen_un` · `_varios` | «y **púlsa** Analizar» | «y **pulsa** Analizar» |
| `cmv40_modals.borrar_artefactos_de_proyecto` | «**Borrar** artefactos…**?**» | «**¿**Borrar artefactos…?» |
| `tab1.subtitulos_adaptado_pistas` | «Subtítulos **adaptado**» | «Subtítulos **adaptados**» |

Tres cosas que salieron al aplicarlo y no estaban previstas:

- **«Monitoriza el progreso en el panel» estaba PARTIDA por el `<strong>`.**
  El golden solo había capturado el trozo de delante, así que la frase
  completa nunca se comprobó. Hoy es una clave con el marcado dentro, que es
  la regla de «una frase es UNA clave».
- **El rótulo que las tres citan no era consistente entre lenguas.**
  `tab2.columna_de_trabajo` decía «columna de trabajo» en castellano pero
  «jobs column» en inglés y «columna de treballs» (plural) en catalán. Lo
  cazó `test_una_cita_a_un_rotulo_usa_el_rotulo_traducido`, que existe para
  esto: una cita tiene que usar el rótulo tal como se traduce. Los dos se
  alinearon al singular, que es como la llama CLAUDE.md.
- **Un «ídem» no es un motivo.** `test_cada_excepcion_lleva_su_motivo` exige
  quince caracteres, y hace bien: una excepción que no se explica es una
  excepción que nadie va a poder revisar.

Con esto y con `ajustes.idioma.nota_pistas` —que dejó de prometer lo que la
app no hacía cuando el perfil por idioma se implementó— **la lista de
defectos del castellano queda vacía**.

---

## Anexo (2026-09-19) — los textos del veredicto CMv4.0

El fichero decía que la lista quedaba vacía. Se reabre con una tanda que **no
salió de traducir**: el usuario lanzó dos proyectos CMv4.0, leyó el veredicto
y pidió literalmente *«estructurado, entendible y formal para un usuario
medio»*, citando una frase suya que no se entendía — «solo ganas que el
upgrade viaje dentro del fichero».

El defecto era común a los treinta y tantos textos del veredicto y del
pre-flight: **describían el formato de la metadata en lugar de la decisión**.
Hablaban de *combos*, *trims de colorista*, *frames neutros*, *append
on-the-fly*, *brackets de toning*, *trim pass* y *bloques L8* — vocabulario de
la especificación de Dolby, no de quien tiene que elegir entre mantener el MKV
o gastar media hora de proceso.

La reescritura aplica tres reglas, en este orden:

1. **Fuera la jerga de la spec.** `combos` → `ajustes`; `trims de colorista` →
   `ajustes hechos a mano por un colorista`; `frames neutros` → `parte de la
   película sin ajuste`; `maxΔ / desviación del neutro` → `intensidad`;
   `append on-the-fly` → `convertir a CMv4.0 sobre la marcha`; `bloques L8` →
   `la capa de ajustes CMv4.0`.
2. **Se queda el vocabulario que el usuario de esta app sí usa**: `bin`,
   `RPU`, `Dolby Vision`, `CMv4.0`, `Blu-ray`, `colorista`. No es un público
   que no sepa qué es un bin; es un público que no ha leído la spec.
3. **Una idea por frase y siempre la misma estructura**: qué trae el bin → qué
   significa para el resultado → qué puedes hacer. Los números se quedan, pero
   **detrás** de la frase que se entiende, no en su lugar.

El «tú» **no se toca**: lo fija `REGISTRO.md` para toda la aplicación, y
«formal» aquí es la redacción, no el tratamiento.

Siete frases estaban en el golden y van en `EXCEPCIONES` con su motivo una a
una. Las demás son posteriores a `pre-i18n` y no estaban vigiladas.

Dos incoherencias salieron **de mirar el render, no el código**, y son las que
justifican haberlo mirado:

- el chip de calidad del tercer veredicto caía al `CMv4 ?` del final de la
  cascada — un interrogante justo donde la app sabe exactamente qué es el bin;
- el modal decía «0 % de frames neutros» al lado de «sin ajuste manual», que
  se lee como lo contrario. Hoy dice «con ajuste en el 100 % de la película»,
  que es **el mismo dato y con las mismas palabras** que la tabla de niveles
  de la ficha.

Y la reescritura destapó **un fallo de verdad**, no de redacción: los tiers
`core` y `core_rich` compartían la misma descripción, así que un máster CORE
estándar se anunciaba como «grading dinámico shot-a-shot intenso; el colorista
trabajó casi todas las escenas», que es exactamente lo que no es. El test que
lo cubría comprobaba `'CORE' in desc` y **«CORE+» contiene «CORE»**, así que
pasaba en verde. Al quedarse el texto nuevo de CORE+ sin la palabra dentro, el
test dejó de poder taparlo. Hoy cada tier tiene su clave, y el test fija **qué
clave usa cada uno** leyéndola del catálogo — comparar las dos descripciones
no servía, porque difieren igualmente por los números.

Lo que **no** se ha tocado, y queda anotado: las descripciones de las fases
(«dovi_tool mux combina BL.hevc + EL_injected.hevc…»). Son otra familia —
cuentan lo que la herramienta hace, no lo que el usuario decide— y revisarlas
es su propia tanda.
