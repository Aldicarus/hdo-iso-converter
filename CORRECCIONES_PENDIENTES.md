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
