# Correcciones pendientes — se aplican al final, no durante la traducción

La regla de esta rama es **el castellano no se toca**: `test_castellano_intacto.py`
lo vigila byte a byte contra el golden de `pre-i18n`. Así que todo lo que
aparezca por el camino y sea un arreglo del original se anota aquí y se hace
después, en su propio commit, con el golden actualizado a la vez.

Tocar el castellano a la vez que se traduce haría indistinguible un arreglo
deliberado de un fallo del refactor, que es justo lo que el golden existe para
distinguir.

---

## 1. El manual cita una pestaña con su nombre caducado

**Dónde**: sección `tools` del manual (`_CMV40_HELP_SECTIONS`, en
`app/static/cmv40_modals.js`).

**Qué pasa**: dice `Editar Propiedades MKV`, y esa pestaña se llama hoy
**`Consultar / Editar MKV`**. El nombre cambió cuando Tab 2 dejó de ser solo
edición y pasó a incluir la radiografía DV+HDR.

**Cómo se detectó**: al traducir la sección, contrastando los nombres de
pestaña que el manual cita contra los de `index.html`.

**Qué hacer**: cambiar el literal en la sección `es`, y las traducciones ya
escritas (`Edit MKV properties` / `Editar Propietats MKV`) por las que
correspondan al nombre nuevo.

---

## 2. Frases partidas por marcado en línea: claves que son fragmentos

**Dónde**: el catálogo de UI, en las frases que el HTML parte con `<strong>` o
`<em>` en medio. Ejemplos reales:

- `ui.el_repositorio` + `ui.lo_mantiene` + `ui.por_su_cuenta_el_espacio_y` son
  **tres trozos de una sola frase**: «El repositorio **DoviTools** lo mantiene
  **R3S3T_9999** por su cuenta: el espacio…».
- `ui.lectura_de_la_hoja_de_recomendaciones` +
  `ui.originales_imagenes_hdr_comp_graficos_comparativas`, donde en inglés el
  adjetivo «original» tiene que cambiar de lado respecto al `<em>`.

**Por qué es un problema**: REGISTRO.md prohíbe traducir por trozos, y esto lo
hace por la puerta de atrás — el extractor ve tres nodos de texto y saca tres
claves. Quien traduzca uno sin ver los otros produce una frase mal cosida, y
quien edite el castellano de uno solo deja los otros descolgados.

**Cómo se ha sorteado por ahora**: los agentes de traducción coordinaron los
fragmentos entre lotes y verificaron la concatenación renderizada en los dos
idiomas. Funciona, pero depende de que alguien se dé cuenta.

**Qué hacer**: que el extractor trate el bloque ENTERO como una unidad con
`data-i18n-html`, conservando el marcado en línea dentro del valor traducible
(`El repositorio <strong>DoviTools</strong> lo mantiene…`). Es lo que hace
cualquier i18n profesional. Implica re-clavar esas claves y volver a traducir
solo esas, no todo.

**Cuántas son**: hay que medirlo (buscar claves cuyo valor empiece o acabe sin
puntuación y cuyo hermano en el DOM sea otra clave).

---

## 3. Tres mensajes que se quedan en castellano: plantilla dentro de plantilla

**Dónde**: `tab2.js` (2) y `tab3.js` (1). Las claves que el propio `node
--check` rechazó al aplicarlas:

- `tab2.master_display_p1` — `Master display ${masterSource ? `<span…>` : ''}`
- `tab2.combos_unicos_p1`
- `tab3.que_la_hoja_no_explica_src`

**Por qué**: la expresión interpolada contiene **otra plantilla** (un backtick
dentro del `${…}`), y eso no se delimita con un regex — se corta en el primer
backtick anidado. El extractor los intentó, node los rechazó y se revirtieron
solos; quedan como castellano incrustado.

**Cómo se detectó**: aplicando cada mensaje uno a uno y preguntándole a `node
--check` si el fichero sigue siendo válido. Sin ese bucle, dos ficheros se
habrían commiteado roto.

**Qué hacer**: reescribir esas tres a mano sacando la ternaria fuera de la
plantilla (`const src = masterSource ? … : '';` y después `${src}`), que deja
el mensaje con un hueco simple y ya extraíble. Son tres sitios.

---

## 4. Seis parámetros llevan castellano cableado

**Dónde**: valores que el JS calcula y mete en un mensaje traducido, así que
saldrían **en castellano dentro de una frase inglesa o catalana**:

| clave del mensaje | parámetro | lo que trae |
|---|---|---|
| `tab3.*` (banner de cola) | `posicion` | «puesto N de M» / «siguiente en la cola» |
| `browser.*` | `filterdesc` | «ficheros .m2ts», «carpetas BDMV»… |
| `tab2.*` | `l2note` | nota del L2 |
| `tab3.correccion_p1` | `p1` | `'adicional'` \| `'manual'` |
| `tab3.se_omiten_p1` | `p1` | `'ninguna fase'` |
| `tab3.*` (sheet) | `src` | « (fila «…»)» |

**Por qué pasa**: el extractor traduce la PLANTILLA, no lo que se le
interpola. Un valor construido con un ternario de literales castellanos se
cuela por el hueco.

**Cómo se detectó**: el traductor de los mensajes con parámetros los encontró
al mirar de dónde venía cada `{…}`, no traduciendo.

**Qué hacer**: cada uno es un literal castellano en el JS que hay que
convertir en su propia clave (`tab3.puesto_n_de_m`, `browser.filtro_m2ts`…) y
pasar ya traducido. Son seis sitios y el arreglo es mecánico, pero hay que
mirar el código de cada uno para saber cuántas variantes tiene.

**Ampliación (backend)**: el mismo problema aparece en el servidor, y dos
traductores distintos lo señalaron:

| dónde | parámetro | lo que trae |
|---|---|---|
| `routers/cmv40.py:2696` y `:2765` | `{accion}` | `"borrar el proyecto"` / `"borrar los artefactos"` |
| `phases/cmv40_pipeline.py:671` | `eta_txt` | `" · quedan ~Xmin Ys"` |

El inglés se redactó para que aguante con el literal sin traducir («Cancel it
before you {accion}.»), pero saldrá mezclado hasta que se extraigan.

---

## 5. Un plural resuelto con un sufijo de una letra, que en inglés no existe

**Dónde**: `cmv40_pipeline.no_existe_ejecuta_fase_f_primero`, el único caso del
repo que pluraliza inyectando `{p2}` = `'n'` / `''` («no se ha / no se han
generado»).

**Qué pasa**: el catalán sale bien (`no s'ha{p2} generat`), pero **el inglés no
tiene inflexión de número de una letra**: el singular queda perfecto («not
found») y el plural escribe **«not foundn»**.

**Qué hacer**: partirla en dos claves (`…_uno` / `…_varios`), que es justo lo
que REGISTRO.md manda para los plurales y esta se saltó.

**Y hay un SEGUNDO caso, encontrado al traducir los mensajes con parámetros**:
`tab1.saltado_ya_existia` — `{p1} saltado{p2} (ya existía{p3})`, con `{p2}` =
`''`/`'s'` y `{p3}` = `''`/`'n'`. El catalán vuelve a salir bien (`ja es
va{p3} crear`, el auxiliar del passat perifràstic), y el inglés vuelve a no
tener dónde poner una `n`: la única pareja de palabras inglesas que se
distinguen por esa letra final es **`a` / `an`**, así que la traducción la usa
(`already processed into a{p3} MKV`) y el resultado es correcto como palabras
pero **cruzado como número** — el singular escribe «a MKV» (debería ser «an»,
por la pronunciación) y el plural escribe «an MKV» con tres proyectos.

Es menos grave que «not foundn» —las dos formas son inglés legible— pero tiene
el mismo arreglo y el mismo motivo: **un sufijo de una letra no es una regla de
plural, es una coincidencia del castellano.** Las dos claves se parten juntas.

**Y un TERCERO, este en catalán**: `tab1.hace_dia` (`tab1.js:1989`) hace «hace
{days} día{p2}», y el plural de `dia` en catalán es **dies**, no `dia+s`. La
traducción lo sortea cambiando de sustantivo (`fa {days} jorn{p2}`), que es
gramatical en los dos números pero de registro literario — el mismo recurso que
ya se usó con `punt{p2} de discrepància`. Con las claves partidas se puede
escribir «dia» y «dies», que es lo que diría cualquiera.

Los tres van juntos: son **los únicos tres sitios del repo** que pluralizan con
un sufijo de una letra.

## 6. Dos cadenas castellanas más, sin extraer

- `' o '.join(faltan)` en `phases/cmv40_pipeline.py:4360` — el separador de una
  lista, que saldrá « o » dentro de una frase inglesa.
- El fallback `'la serie'` de `{p3}` en `routers/tab1.py:2229`.

Las encontró el traductor del lote 3 mirando de dónde venía cada hueco.
