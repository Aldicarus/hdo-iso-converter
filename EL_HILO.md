# El hilo — mapa del diagnóstico

> **Qué se pide.** «Me cuesta mucho seguir el hilo, enlazar los mensajes y
> entender qué está pasando. Cada fase casi corre de manera independiente sin
> justificar ni aportar evidencias de lo que está haciendo. Si a eso le añades
> que la coherencia modal de ejecución vs ficha de proyecto no es clara, queda
> todo muy confuso.» — 2026-09-19.
>
> **Alcance acordado**: las dos superficies contando lo mismo · las tres
> pestañas · incluido deduplicar el pre-flight.

Este documento es el **bloque 0**: no toca código. Reconstruye lo que el
usuario ve y señala dónde se rompe, con la evidencia delante. Existe porque en
esta misma sesión mirar el render —y no el código— destapó tres defectos que
la lectura del fuente no había visto.

---

## 1. Las cinco superficies

Un job de CMv4.0 le habla al usuario por cinco sitios, cada uno con su
vocabulario y su propia derivación del estado:

| superficie | qué cuenta | de dónde sale |
|---|---|---|
| modal del pre-flight | checklist de 5 filas + veredicto + barra | `_cmv40PfChecks` / `_cmv40PfVeredicto`, sobre `GET /api/cmv40/{id}` |
| log de la sesión | la narración: `━━━`, `📋 Plan`, `🎯 Resultado` | `cmv40_pipeline`, línea a línea |
| card «Análisis y recomendación» | badge + motivo + tabla de niveles + decisión | `_renderCMv40RecommendationCard` |
| cards de fase A–H | estado por fase + qué hace cada herramienta | `_cmv40RenderFaseCard` + `_cmv40Fase?Body` |
| columna de trabajo + su modal | fase, %, transcurrido, ETA | `trabajos.py` + `workbar.js` |

**Ninguna se refiere a las otras.** No hay un sitio donde se resuelva «qué
está pasando» y del que beban todas — que es justo el patrón que este repo ya
aplicó dos veces con éxito (`cmv40_strategy.resolve_plan` → `session.plan`, y
`trabajos.py` con su registro de adaptadores).

---

## 2. Dónde se rompe, con la evidencia

Numerados para poder discutirlos por separado. La evidencia sale de los dos
jobs del 2026-09-19 (13:37 y 13:38) y de un job terminado.

### H1 · El log anuncia un paso que no va a ocurrir  ⚠️ confirmado en código

Al cancelar la Fase A, el log imprime el arranque del paso siguiente **después**
de que el proceso haya muerto:

```
[13:43:18]   Exiting normally, received signal 15.
[13:43:18] [Fase A] ┌─ Paso 1/4: Extrayendo stream HEVC del MKV origen con ffmpeg…
[13:43:18] 🛑 Cancelado: fase analyze_source detenida a petición del usuario tras 276.0s.
```

Causa: `_ffmpeg_extract_rpu_piped` devuelve `False` **sin lanzar** ante
cualquier problema —incluido el SIGTERM del cancel— así que el caller lo toma
por «el pipe no era viable» y cae al camino de dos pasadas, que anuncia su
paso antes de que `_run_streaming` compruebe la cancelación
(`cmv40_pipeline.py:1684-1689`).

### H2 · El mismo trabajo se numera de dos maneras  ⚠️ confirmado en código

La Fase A dice «Paso 1/3» por el camino del pipe y «Paso 1/4» por el de
reserva (`paso_1_3_extrayendo_el_hevc` / `paso_1_4_extrayendo_stream_hevc`).
El usuario ve cambiar el denominador a mitad de fase y no hay nada que lo
explique.

### H3 · La ficha dice «Análisis pendiente» mientras el trabajo corre

Los **dos** proyectos del 13:37, en estados distintos —uno que el usuario
decidió inyectar, otro que pasó de largo por tener L8 real— muestran el mismo
rótulo:

```
  Pulp Fiction      tone_mapping  maxΔ 41   user_choice=inject  → «Análisis pendiente»
  Posesión infernal real          maxΔ 328  decision=ok         → «Análisis pendiente»
```

Causa: `recommend_action` devuelve `unknown` hasta que la Fase A puebla
`source_l2_unique_count`, y el re-derivado del GET rellena el rótulo igual.
La card no distingue «no lo sé todavía» de «esto es lo que vas a obtener».

### H4 · La ficha no dice que cancelaste

Tras el cancel, `phase` vuelve a `created` y `recommended_action` queda
vacío. El log lo cuenta con su `🛑`; la card no menciona la cancelación por
ninguna parte. Es la incoherencia modal-vs-ficha en su forma más pura: **el
hecho más importante que le ha pasado al proyecto no está en la ficha.**

### H5 · El log imprime estructuras de Python

```
Validación final: /mnt/output/Predator. Badlands (2025) […].mkv.tmp
Validación final: {'profile': 7, 'el_type': 'FEL', 'cm_version': 'v4.0', …}
```

Dos líneas **sin timestamp y sin prefijo de fase** —rompen el formato del
resto— y la segunda es un `repr` de diccionario.

### H6 · Tab 3 no tiene bloque de validación legible

Tab 1 cierra enumerando lo que acabó en el fichero, con un ✅ por pista
(sección 3). Tab 3 cierra con un `🎯 Resultado` de una línea. Es literalmente
el «no aporta evidencias de lo que está haciendo».

### H7 · Ninguna fase dice de dónde viene

Cada `📋 Plan` describe lo que va a hacer sin referirse a lo que se midió
antes. La regla del proyecto —«describir estado, no predecir futuro»— nació de
un bug real y **se queda**, pero prohíbe mirar adelante, no mirar atrás. Hoy
nadie mira atrás.

### H8 · El estado de la decisión vive en tres campos

`preflight_decision` · `recommended_action` · `preflight_user_choice`. Cada
superficie mira uno. Los tres defectos corregidos el 2026-09-19 por la mañana
eran de esta familia, y se arreglaron uno a uno.

### H9 · El pre-flight está duplicado

`_cmv40_dispatch_preflight` y el endpoint `preflight-target` son dos copias del
mismo bloque. El comentario de la segunda cuenta que una vez se quedó sin
`_paso` durante días, y esta misma tarde hubo que poner los mismos dos
guardados en las dos.

### H10 · Tres vocabularios para la misma fase

El log dice `[Fase A]`, la card dice «Fase A — Analizar MKV origen», el estado
interno dice `analyze_source` y la columna de trabajo dice «Fase A —
Analizando el MKV origen». Cuatro nombres para lo mismo, y el usuario los ve
los cuatro.

---

## 3. Lo que YA funciona, y hay que copiar: Tab 1

El pipeline del rip hace hoy tres de las cuatro cosas que se piden. Del log
real de `Juego_de_tronos_S03E03`:

```
[Pipeline] ━━━ Iniciando: GOT UHD S03 DISC1 ━━━
[Pipeline] 📋 Plan: leer la carpeta BDMV, localizar el playlist principal, extraer…
[Fase A]   ┌─ Paso 1: Origen directo — leyendo la carpeta BDMV
[Fase A]   └─ ✓ Carpeta lista: /mnt/isos/GOT UHD S03/GOT UHD S03 DISC1
[Pipeline] 🎯 Ruta directa: hay pistas reordenadas o excluidas, así que un solo
           mkvmerge hace selección + reorganización + metadatos
[Validación] 🎞️ Pistas: 1 vídeo · 2 audio · 2 subtítulos
[Validación]   🔊 Audio #1: AC-3 · spa [DEFAULT] · "Castellano DD 5.1" ✅
```

- **cabecera con el nombre del trabajo**, no con un id;
- **una decisión anunciada con su motivo, mirando atrás** («hay pistas
  reordenadas o excluidas, **así que**…») — exactamente lo que falta en Tab 3;
- **un bloque de validación legible** que enumera el resultado y lo tica.

Lo que le falta a Tab 1: el plan es del pipeline entero y no se retoma, y la
ficha tampoco recoge nada de esto.

**Conclusión del mapa: el modelo a seguir ya existe dentro de la aplicación.**
Tab 3 no lo perdió por descuido — lo perdió al partirse en nueve fases
independientes, cada una con su propio arranque y su propio cierre.

---

## 4. El orden propuesto

| bloque | qué | cierra | |
|---|---|---|---|
| 1 | **Un solo relato, resuelto en el servidor**: qué se ha hecho, por qué, qué toca ahora y qué queda. Servido y pintado sin re-derivar. | H3, H4, H8, H10 | ✅ |
| 2 | **Cada fase justifica mirando atrás** y cierra con evidencia legible, al estilo de Tab 1. | H5, H6, H7 | ✅ |
| 3 | **La ficha cuenta el trabajo**; el detalle técnico se pliega. | el resto de H3/H6 | ✅ |
| 4 | **Deduplicar el pre-flight** y arreglar el anuncio del paso fantasma. | H1, H2, H9 | ✅ |
| 5 | **Tab 1 y Tab 2 al mismo modelo.** | — | ✅ |

Bloque 1 va primero porque H3, H4 y H10 no se pueden arreglar caso a caso sin
que vuelvan: son derivaciones paralelas del mismo estado. Es el mismo
razonamiento que llevó a `cmv40_strategy`, y la mañana del 2026-09-19 es la
demostración de lo que pasa si se parchean de uno en uno.


---

## 5. Cerrado — 2026-09-19/20

Los diez hallazgos, cerrados y desplegados (`v2.8.1-317-g3ce95a0`). El diseño
que salió está en CLAUDE.md, «El relato: un solo sitio donde se resuelve qué
está pasando»; aquí queda lo que este documento aportó y que no estaba en el
encargo.

**Tres defectos no se veían desde el código, solo desde el render o desde los
datos del NAS**, y son los que justifican el bloque 0:

- el veredicto `tone_mapping` estaba **sin cablear en seis sitios** y el
  sidebar y la ficha del MISMO proyecto discrepaban, porque `GET
  /api/cmv40/{id}` re-derivaba la clasificación del L8 sin L3;
- **`CMv40Cancelled` es una `Exception`**, así que cinco `except Exception`
  alrededor de subprocesos se la tragaban: al cancelar, la Fase A remataba
  con «el MKV origen no tiene duración detectable… fichero corrupto», o sea
  culpando al fichero del usuario de que el usuario pulsara cancelar;
- y **dos líneas del `historial.jsonl` del NAS dicen `running`** y lo dirían
  siempre, pintadas como un error rojo mudo. Son los dos episodios de Juego
  de Tronos del deploy del 2026-09-12.

**Y dos de los cinco bloques destaparon divergencias que nadie había pedido**,
las dos al fusionar o unificar: las dos copias del pre-flight diferían en los
tramos de progreso y en si exigían `target_preflight_ok` antes de encadenar;
y los fixtures de `test_columnas_de_proyecto` no correspondían a lo que manda
el endpoint —el de la sesión con error decía `status: "pending"` con el error
dentro del historial, que es literalmente la derivación que el bloque 5
quitó.

Lo que queda por hacer no es de este hilo: **verificarlo con discos reales**.
Nada de esto se ha usado todavía contra un rip ni contra un upgrade de verdad.
