"""El castellano actual no se toca. Este test es lo que lo hace verificable.

La traducción a inglés y catalán mueve ~2.650 frases castellanas de estar
incrustadas en el código a estar en un catálogo. Ese movimiento **no debe
cambiar ni una palabra del castellano**, y una intención no basta: un cambio de
una coma en una frase que se mueve de sitio no lo nota nadie leyendo el diff de
un refactor de 3.000 líneas.

`golden_castellano.json` se capturó con `captura_castellano.py` sobre el estado
`pre-i18n` (etiqueta de git, punto de retorno). Cada frase de ahí tiene que
seguir existiendo, **byte a byte**, en alguno de estos dos sitios:

  · el código, si todavía no se ha extraído;
  · el catálogo `es`, si ya se extrajo.

Así el test acompaña la migración entera: pasa antes de empezar, pasa a mitad y
pasa al final, y solo falla si el castellano cambia.

## Las frases reescritas

Las 248 plantillas con interpolación **hay que reescribirlas** —un fragmento
suelto no se puede traducir— y ahí el literal castellano sí cambia de forma.
Para esas va `EXCEPCIONES`: cada una declara qué frase desapareció y por qué, y
**la lista no se puede ampliar sin escribir el motivo**. Lo que se conserva en
esos casos es el texto RENDERIZADO, y eso lo comprueba
`test_mensajes_con_parametros.py` llamando a `t()` con parámetros de ejemplo.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_castellano_intacto -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import captura_castellano as captura  # noqa: E402

GOLDEN = APP_DIR / "tests" / "golden_castellano.json"

# Frases que han cambiado de FORMA a propósito, con el motivo. Formato:
#   "frase exacta del golden": "por qué ya no existe tal cual"
# Ampliarla es una decisión, no un arreglo: si una frase desaparece sin entrada
# aquí, el test falla y hace bien.
EXCEPCIONES: dict[str, str] = {
    # ── La Fase H, renombrada el 2026-09-23. El usuario preguntó por qué se
    #    llama «validar y guardar» si «sólo mueve el MKV y borra temporales».
    #    Sí valida —por la rama merge son dos `extract-rpu` completos, minutos
    #    en un UHD— pero su job iba por drop-in, donde son segundos, y el
    #    texto era el mismo en las dos: nada en la pantalla le daba motivos
    #    para pensar otra cosa. Se conserva la comprobación en el nombre (que
    #    es lo que hace) y se nombra el objeto, como las otras siete etapas;
    #    «comprobar» además deja «validar» para la validación PREVIA y para la
    #    card 🛡️ Validaciones, que son otras dos cosas.
    "Fase H — Validación final":
        "el título de la card nombraba el acto y no su objeto, al revés que "
        "las otras siete etapas: «Fase H — Comprobar el MKV y guardarlo»",
    "Validar y finalizar":
        "el botón: «finalizar» no dice qué pasa con el fichero, y lo que pasa "
        "es que se guarda — «Comprobar y guardar»",
    "antes de mover el MKV al directorio de salida, la app verifica que el "
    "resultado es estructuralmente correcto y que el upgrade a CMv4.0 se ha "
    "materializado en el fichero. Si algo falla, el proyecto queda en error y "
    "puedes rehacer desde la fase que prefieras.":
        "el bloque didáctico decía QUÉ se valida sin decir sobre qué: hoy "
        "parte de que el MKV ya está escrito con sufijo .mkv.tmp, que es lo "
        "que explica por qué hay una comprobación antes de darle su nombre, y "
        "menciona que ese .mkv.tmp se conserva si algo falla",
    "hace un momento":
        "había DOS escalas de edad —la de las tarjetas de proyecto y la de la "
        "columna de trabajo— y cada una nombraba el primer escalón a su "
        "manera. Al fundirlas en `hace()` (core.js) queda «ahora mismo», que "
        "es la que ya veían las tres columnas de proyecto. La divergencia era "
        "el bug: la de la columna componía la edad con el formateador de "
        "DURACIONES y por eso no bajaba de las horas («hace 258h 18 min»).",
    "API keys e integraciones. Se persisten ofuscadas en local; ganan sobre "
    "las variables de entorno.":
        "el subtítulo del modal de ⚙︎ describía SOLO la sección de "
        "Integraciones, y desde el 2026-09-21 hay tres: General, Aspecto e "
        "idioma e Integraciones. La advertencia sobre las claves pertenece a "
        "su sección, no a la cabecera de todo el modal. Se vio mirando la "
        "captura del modo oscuro, no el código.",
    # ── Los textos del veredicto CMv4.0, reescritos el 2026-09-19 a petición
    #    del usuario: «estructurado, entendible y formal para un usuario
    #    medio». Todos describían el FORMATO de la metadata —combos, trims,
    #    frames neutros, L8— en lugar de lo que el usuario tiene que decidir,
    #    y el propio usuario citó una frase que no se entendía. Ver
    #    CORRECCIONES_DEL_CASTELLANO.md.
    "El L8 es trabajo de colorista":
        "la fila del checklist nombraba un nivel de la spec; hoy dice lo que "
        "comprueba: «Los ajustes del bin son trabajo de un colorista»",
    "El bin no aporta un L8 trabajado":
        "mismo caso en el título del veredicto: «ningún ajuste hecho a mano» "
        "dice lo mismo sin pedir saber qué es el L8",
    "No — el RPU es sintético":
        "«sintético» es la palabra del clasificador, no del usuario: lo que "
        "significa es que el RPU no lleva ningún ajuste",
    "El RPU es sintético: inyectarlo daría el mismo resultado visible":
        "igual, en el cuerpo del veredicto; además «resultado visible» pasa a "
        "«resultado en pantalla», que es lo que se mira",
    "CMv4 sintético":
        "el chip de calidad, con la misma palabra; hoy «CMv4 sin ajustes»",
    "Decisión Mantener vs Inyectar (rápido / preserva L2) basada en el análisis del bin: clasificación L8, tier de calidad CMv4 y comparación L2 source vs target":
        "el subtítulo de la card encadenaba cinco términos internos "
        "(clasificación L8, tier, L2 source vs target) para decir qué aporta "
        "el bin y qué hacer con él, que es lo que dice ahora",
    "⟦⟧ % de frames neutros":
        "un «frame neutro» es un frame sin ajuste, y así se dice: el dato no "
        "cambia, cambia el nombre que se le da",

    # ── Y el vocabulario de la MISMA decisión, unificado en la misma tanda.
    #    Convivían «al vuelo», «en runtime», «resultado visible» y «bin
    #    sintético» con los textos ya reescritos, que es la incoherencia que
    #    el usuario señalaba un nivel por encima de cada frase suelta. Hoy:
    #    «sobre la marcha», «resultado en pantalla», «no lleva ajustes
    #    propios». Ni una de las tres decía nada que la nueva no diga.
    "Cierra el proyecto sin tocar el MKV original. Un reproductor compatible con CMv4.0 (p3i T4 / Sony / LG modernos) hará la conversión al vuelo en runtime.":
        "«al vuelo en runtime» dice dos veces lo mismo y la segunda en inglés",
    "El resultado visible es equivalente a la conversión al vuelo del":
        "«resultado visible» → «resultado en pantalla», que es lo que se mira",
    "Inyectar RPU CMv4.0 aunque el bin sea sintético":
        "«sintético» es la palabra del clasificador; el botón dice ahora qué "
        "le pasa al bin: no lleva ajustes propios",
    "Procesa el MKV inyectando el RPU CMv4.0 aunque el bin sea sintético. Resultado equivalente a la conversión al vuelo del reproductor pero quedará archivado como MKV CMv4.0 completo.":
        "el tooltip del mismo botón, con las dos sustituciones",
    "compatible con CMv4.0 hace la conversión al vuelo, con el":
        "fragmento del veredicto de «Mantener»: «sobre la marcha»",
    "hará la conversión al vuelo en runtime — el resultado visible es":
        "fragmento del diálogo de confirmación, con las dos sustituciones",
    "mismo resultado visible que tendría inyectar el RPU.":
        "cierre del mismo veredicto: «resultado en pantalla»",
    "— el fichero original quedó intacto. Tu reproductor (p3i T4 / Sony / LG modernos) hace la conversión CMv4.0 al vuelo en runtime.":
        "el banner del proyecto cerrado por «Mantener», con la misma "
        "sustitución que su veredicto",
    "Analizando los combos del RPU…":
        "el rótulo del paso mientras corre el pre-flight, en la misma "
        "pantalla donde el veredicto ya dice «ajustes»: dejarlo en «combos» "
        "era el vocabulario viejo sobreviviendo justo al lado del nuevo",

    # ── Los nueve defectos DEL CASTELLANO, corregidos el 2026-09-17 con el
    #    visto bueno del usuario. Están en CORRECCIONES_DEL_CASTELLANO.md con
    #    su motivo: casi todos son texto que describía bien algo que después
    #    cambió, más tres faltas de ortografía y concordancia.
    "Se recalcula automáticamente al cambiar los toggles":
        "los toggles se retiraron —los sustituyeron las dos tarjetas del "
        "disco— y el nombre lo construye solo el backend",
    "Iniciando extracción… Sigue el progreso en \"Trabajos en Curso\".":
        "el panel «Trabajos en Curso» se movió al modal de detalle de la "
        "columna de trabajo; mandaba al usuario a un sitio que no existe",
    "Añadido a la cola en posición ⟦⟧ . Sigue el progreso en \"Trabajos en Curso\".":
        "el segundo de los tres mensajes que mandaban al panel retirado; hoy "
        "cita la columna de trabajo",
    "Monitoriza el progreso en el panel":
        "el tercero, y además estaba PARTIDO por el `<strong>`: el golden "
        "capturó solo el trozo de delante. Hoy es una clave con el marcado "
        "dentro, que es la regla de «una frase es UNA clave»",
    "Trabajos en Curso":
        "clave huérfana: la única referencia que quedaba estaba dentro de un "
        "comentario, y llevaba mayúsculas de título, que el REGISTRO prohíbe",
    "Error en analisis: ⟦⟧":
        "le faltaba la tilde de «análisis»",
    "mkvpropedit in-place (solo ruta sin reordenación, — en ruta directa)":
        "la frase COLGABA: faltaba lo que iba después de la raya. Hoy dice "
        "que en la ruta directa no se ejecuta, que es lo que pasa",
    "Paso 2: elige el origen (un fichero) y púlsa Analizar.":
        "«pulsa» es llana y no lleva tilde",
    "Paso 2: elige el origen (varios episodios) y púlsa Analizar.":
        "la misma falta en la hermana: se escribió una vez y se copió",
    "── Subtítulos adaptado ( ⟦⟧ pistas) ──":
        "falta de concordancia: «adaptados»",

    # ── El idioma que manda pasa a ser un HUECO.
    #
    # La frase afirmaba «Castellano» y desde el 2026-09-17 el perfil de
    # pistas depende del idioma de la app: con el perfil inglés eso es
    # falso. Es el mismo defecto que el rótulo del toggle, un nivel más
    # abajo. El castellano RENDERIZADO no cambia —`{pref}` vale
    # «Castellano» con la app en castellano— y lo que cambia es que hay un
    # hueco donde había una palabra escrita.
    "— sin flag forced de Matroska porque no es Castellano":
        "el idioma preferido pasa a `{pref}`, que lo manda el servidor con "
        "la siembra del catálogo (`idiomaDePistaPreferido`)",

    # ── Los tres plurales por sufijo de una letra, partidos en dos claves.
    #
    # `{p2}` = 's'/'' y `{p3}` = 'n'/'' pluralizan en castellano por pura
    # coincidencia ortográfica: el inglés no tiene ninguna palabra que se
    # pluralice añadiendo una `n` («not foundn») y el catalán tampoco cuando
    # el plural es irregular («dia» → «dies»). El castellano RENDERIZADO no
    # cambia — sale «1 saltado (ya existía)» y «3 saltados (ya existían)»
    # igual que antes—, lo que cambia es que son dos claves en vez de una con
    # un hueco.
    "⟦⟧ saltado ⟦⟧ (ya existía ⟦⟧ )":
        "partida en `tab1.saltado_ya_existia_uno` / `_varios`",
    "hace ⟦⟧ día ⟦⟧":
        "partida en `tab1.hace_dia_uno` / `_varios`",
    "⟦⟧ no existe ⟦⟧ — ejecuta Fase F primero (workflow ⟦⟧ )":
        "partida en `cmv40_pipeline.no_existe_ejecuta_fase_f_primero_uno` / `_varios`",
    "+ ⟦⟧ consulta ⟦⟧ en curso":
        "partida en `workbar.mas_una_consulta_en_curso` / `_n_consultas`: el "
        "plural de «consulta» en catalán es «consultes», no «consultas», y en "
        "inglés «query» tampoco pluraliza con una `s` pegada",
    "aparece ⟦⟧ desmarcado ⟦⟧ con badge":
        "partida en `tab1.uno_ya_procesado_aparece_desmarcado` / "
        "`_n_ya_procesados`: el sufijo del verbo era `n` (aparece/aparecen) y "
        "en catalán es `en` (apareix/apareixen)",

    # ── Un valor castellano que se colaba por el hueco de un parámetro.
    #
    # `Corrección {p1}` con `p1` ∈ {'adicional', 'manual'}: el adjetivo es un
    # literal del JS, así que en inglés y en catalán saldría en castellano
    # dentro de la frase. Son dos claves completas.
    "Corrección ⟦⟧":
        "partida en `tab3.correccion_adicional` / `tab3.correccion_manual`",

    # ── El mensaje con una plantilla DENTRO de un `${…}`.
    #
    # El extractor no puede delimitar eso con un regex —se corta en el primer
    # backtick anidado— así que este mensaje se quedó a medio extraer: un
    # trozo con `data-i18n` y el resto en castellano, con la sintaxis de la
    # ternaria incluida en lo que capturó el golden. Reescrito sacando la
    # ternaria a un `const`, es UNA clave con tres parámetros.
    "Se detecta un desfase de":
        "absorbida en `tab3.se_detecta_un_desfase_que_la_hoja_no_explica`",
    "que la hoja no explica ⟦⟧ (ahí ⟦⟧ ). Revisa el chart antes de inyectar.":
        "era la cola de la ternaria anidada; hoy es la misma clave de arriba",

    # ── El otro mensaje con plantilla dentro de plantilla.
    #
    # `<strong>{n}</strong> combos únicos ${… ? `· {n} target_pqs` : ''}` se
    # quedó a medio extraer por lo mismo; hoy el rótulo es un `data-i18n` y
    # el contador va delante, así que la frase ya no lleva el hueco detrás.
    "combos únicos ⟦⟧":
        "el rótulo pasa a `tab2.combos_unicos`; el hueco iba en la plantilla "
        "anidada que se reescribió",

    # ── Dos `${…}` contiguos que pasan a un solo parámetro.
    #
    # El fuente era `(Δ = ${signo}${delta})`, o sea el signo y el número
    # interpolados por separado, y el golden capturó los dos huecos. Hoy el
    # signo se compone antes y viaja dentro de `{delta}`: el castellano
    # RENDERIZADO es el mismo —«(Δ = +12)»— y lo que cambia es el número de
    # huecos, que la equivalencia de `⟦⟧` no puede normalizar porque son dos
    # contra uno.
    "Diferencia de frames detectada (Δ = ⟦⟧ ⟦⟧ ). ⟦⟧":
        "el signo y el número pasan a un solo `{delta}` en "
        "`tab3.diferencia_de_frames_detectada_delta`",

    # ── El rótulo del gráfico L8, partido por una ternaria de sufijos.
    #
    # Era `L8 target displays · escala logarítmica de nits${tieneLuz
    # ? ' · validado film completo' : ' · sample 30s'}`: el rótulo traducido y
    # el sufijo en castellano, así que en inglés salía media frase en cada
    # idioma. Hoy son DOS claves completas, una por rama, y por eso la forma
    # con el hueco al final ya no existe.
    "L8 target displays · escala logarítmica de nits ⟦⟧":
        "partida en `tab2.l8_escala_validado_film_completo` / `_sample_30s`",

    # ── El hueco que llevaba el artículo dentro, y la contracción perdida.
    #
    # `Encuadre VARIABLE en {cuales}` con `cuales` ∈ {«ambos másters», «el
    # BD», «el bin»}: en castellano `en` no contrae y la frase salía bien,
    # pero en catalán la preposición es `a` y «a el bin» tiene que ser «al
    # bin» — y eso no se puede resolver interpolando el sujeto. Son tres
    # claves completas. El castellano RENDERIZADO no cambia, y de paso se fue
    # el «el BD» que estaba cableado en el código.
    "[Fase B] Encuadre VARIABLE en ⟦⟧ — típico de un máster con escenas expandidas (IMAX / open matte).":
        "partida en `cmv40_pipeline.encuadre_variable_ambos` / `_bd` / `_bin`",
    # ── «escena» donde el dato es POR FRAME (2026-09-18, a petición del
    #    usuario). El perfil de luminancia sale del bloque L1, que tiene un
    #    registro por FRAME; lo que el chart pinta son cubos de la serie y
    #    los `bucket_dim/mid/high` cuentan FRAMES. Y justo al lado, en la
    #    misma pantalla, hay un stat «scene cuts» que sí son escenas de
    #    verdad: dos cosas distintas con el mismo nombre.
    #
    #    La peor era «Distribución por brillo de escena», porque el número
    #    entre paréntesis es un recuento de frames y en un UHD de 159.000 se
    #    lee como 159.000 escenas.
    #
    #    Los dos del gate L1 de Tab 3 tenían además otra imprecisión: compara
    #    el `l1_max_cll` del `info --summary`, que es el PICO de todo el
    #    metraje, no un promedio ni una comparación escena a escena.
    #
    #    NO se ha tocado la prosa didáctica del manual («metadata de
    #    tone-mapping dinámico por escena», «MaxCLL/MaxFALL dinámico por
    #    escena»): ahí «por escena» describe bien la naturaleza del L1 y
    #    cambiarlo a «frame» empeoraría la explicación.
    "Perfil de luminancia DV L1 por escena ⟦⟧":
        "el L1 tiene un registro por FRAME; hoy dice «por frame»",
    "pico de luz por escena · nits (escala logarítmica)":
        "el eje Y del chart es el max_pq del frame; hoy dice «por frame»",
    "Distribución por brillo de escena":
        "cuenta FRAMES (`bucket_dim/mid/high`), y el recuento iba bajo la "
        "palabra «escena»; hoy «Distribución de frames por brillo»",
    "Valores extraídos del bloque L1 del RPU Dolby Vision (peak/avg de PQ por escena, según etiquetó el colorista). No son medidas reales en pantalla — un disco conservadoramente mastered (BR2049, p.ej.) puede mostrar peaks de metadata bajos aunque la imagen real alcance valores mayores tras tone-mapping. Coincide exactamente con dovi_tool info --summary.":
        "el peak/avg de PQ es por FRAME; el resto del tooltip no cambia",
    "Análisis per-escena no generado":
        "es el estado vacío del perfil de luminancia y no hay ningún "
        "análisis «per-escena»; hoy «Perfil de luminancia no generado»",
    "L1 — MaxCLL dinámico por escena":
        "el gate compara el `l1_max_cll` del summary, que es el pico de todo "
        "el metraje; hoy «L1 — MaxCLL del metadata dinámico»",
    "Promedio de brillo escena a escena. Por encima del umbral el grading del bin diverge del BD.":
        "ni es un promedio (es el PICO) ni se compara escena a escena (son "
        "dos números agregados del `info --summary`)",
    "Source y target tienen exactamente el mismo número de frames — condición crítica para que el RPU se inyecte alineado escena a escena.":
        "el gate cuenta FRAMES y la inyección del RPU es frame a frame, que "
        "es como lo llama el resto de la app",
}


class TestLaListaDeExcepcionesNoSeQuedaVieja(unittest.TestCase):
    """Una excepción que ya no corresponde a ninguna frase del golden parece
    cobertura y no cubre nada — y encima documenta un cambio que quizá se
    revirtió. Pasó al reconstruir el golden: la forma que la entrada citaba
    era un ARTEFACTO de la captura vieja, y al arreglarla se quedó huérfana."""

    def test_cada_entrada_corresponde_a_una_frase_del_golden(self):
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        todas = set(golden["frontend"]) | set(golden["backend"])
        fantasma = sorted(k for k in EXCEPCIONES if k not in todas)
        self.assertEqual(fantasma, [], (
            "\nestas entradas de EXCEPCIONES ya no citan ninguna frase del "
            "golden:\n  · " + "\n  · ".join(x[:70] for x in fantasma)))


def _catalogo_es() -> set[str]:
    """Las frases castellanas ya extraídas: catálogo de UI y manual.

    El manual va aparte porque no es un catálogo de claves sino tres
    documentos paralelos de HTML, así que sus frases se sacan parseándolo
    igual que se sacaron del código.
    """
    fuera: set[str] = set()
    manual = APP_DIR / "static" / "i18n" / "manual" / "es.json"
    if manual.exists():
        for html in json.loads(manual.read_text(encoding="utf-8")).values():
            fuera.update(x for x in captura._del_html(
                re.sub(r"\$\{[^}]*\}", " ⟦⟧ ", html)) if captura.es_frase(x))
    # Los dos catálogos: el del frontend y el del BACKEND (`app/i18n/`), que
    # es donde han ido las 483 frases del log y de los errores HTTP.
    rutas = [APP_DIR / "static" / "i18n" / "es.json",
             APP_DIR / "i18n" / "es.json"]
    if not any(r.exists() for r in rutas):
        return fuera
    def hojas(nodo):
        """Los valores TAL CUAL, sin normalizar el espacio.

        Normalizar aquí dentro se llevaba por delante los saltos de línea
        antes de que nadie pudiera convertirlos a la forma del golden, que es
        justo la última equivalencia de abajo.
        """
        if isinstance(nodo, str):
            yield nodo
        elif isinstance(nodo, dict):
            for v in nodo.values():
                yield from hojas(v)
        elif isinstance(nodo, list):
            for v in nodo:
                yield from hojas(v)
    crudos: set[str] = set()
    for r in rutas:
        if r.exists():
            crudos |= set(hojas(json.loads(r.read_text(encoding="utf-8"))))
    valores = {" ".join(v.split()) for v in crudos}
    fuera |= valores
    # Y la misma frase con los huecos normalizados al centinela del golden.
    #
    # Las frases interpoladas se capturaron con `${…}` sustituido por `⟦⟧`, y
    # al convertirlas en mensajes con parámetros pasaron a llevar `{max}`,
    # `{total}`… Es el MISMO hueco escrito de otra forma, no un cambio del
    # castellano: las palabras de alrededor tienen que seguir coincidiendo
    # byte a byte, y eso es lo que se comprueba. Listar cincuenta excepciones
    # habría escondido justo lo que el guard existe para ver.
    fuera |= {" ".join(re.sub(r"\{\w+\}", " ⟦⟧ ", v).split()) for v in valores}
    # Y la misma frase con el salto de línea escrito como en el fuente.
    #
    # En la plantilla, `\n` son DOS caracteres que el motor de JS resuelve al
    # ejecutar, y el golden capturó el fuente: los guarda tal cual. En el
    # catálogo tienen que ser un salto de verdad —si se guardan como texto, el
    # modal imprime `\n` en pantalla, que es un bug que hubo y está
    # arreglado—. Es el MISMO salto escrito de dos formas, así que se
    # normaliza en vez de listar seis excepciones que esconderían un cambio
    # real en esas frases.
    # Las dos normalizaciones se combinan, porque hay frases que llevan las
    # dos cosas (`{name}\n{timestamp}\nArrastra para mover…`).
    for v in crudos:
        escapado = v.replace("\n", "\\n")
        fuera.add(" ".join(escapado.split()))
        fuera.add(" ".join(re.sub(r"\{\w+\}", " ⟦⟧ ", escapado).split()))
    # Y un valor con MARCADO dentro aporta además sus nodos de texto.
    #
    # Las frases que el HTML partía con un `<strong>` en medio se capturaron
    # como trozos —el golden tiene «El repositorio», «lo mantiene» y «por su
    # cuenta…» por separado, porque eran tres nodos de texto— y hoy son UNA
    # clave con el marcado dentro. Es la misma frase: se desparte igual que
    # se despartía el manual, que ya pasaba por aquí.
    for v in crudos:
        if "<" not in v:
            continue
        for trozo in captura._del_html(v):
            if trozo:
                fuera.add(trozo)
                fuera.add(" ".join(re.sub(r"\{\w+\}", " ⟦⟧ ", trozo).split()))
    return fuera


class TestElCastellanoSigueSiendoElMismo(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        cls.vivas = (captura.frases_del_frontend()
                     | captura.frases_del_backend()
                     | _catalogo_es())

    @staticmethod
    def _sin_prefijo(frase: str) -> str:
        """La frase sin su `[Fase C]`, su marker y su dibujo de árbol.

        El bloque 5 dejó los prefijos LITERALES en el código —son claves del
        parser del frontend y de la persistencia del log— y mandó al catálogo
        solo la prosa. Así que el golden guarda «[Audit] L2: …» y el catálogo
        «L2: …»: es la misma frase, partida donde tocaba.

        Se usa el MISMO regex que el extractor, no una copia: si el criterio
        de qué es un prefijo cambia, cambia en un sitio.
        """
        import extraer_backend as eb
        m = eb._PREFIJO.match(frase)
        prosa = m.group(5) if m else frase
        # Y el marker de CIERRE: hay líneas de fase que van entre dos `━━━`.
        # El de apertura ya lo quitaba el regex del prefijo; el de cierre se
        # quedaba dentro y esas cuatro frases parecían perdidas.
        prosa = re.sub(r"\s*━+\s*$", "", prosa)
        return " ".join(prosa.split())

    def _absorbida(self, frase: str) -> bool:
        """La frase sigue igual, dentro de una frase más larga.

        Los fragmentos que el marcado partía se capturaron por separado —«.
        Click para seleccionar.», «— marca solo los que quieras añadir o
        rehacer.»— y al unirlos en una sola clave dejaron de existir como
        cadena suelta. El castellano no cambió: la secuencia de caracteres
        está ahí, byte a byte, dentro del valor del catálogo. Lo que cambió es
        dónde acaba la frase, que es justo lo que unir fragmentos hace.

        Medido sobre el golden actual: **208 de 2.660** frases son además
        subcadena de otra viva, así que para esas el guard deja de poder ver
        un borrado. Es el precio de la equivalencia, y a cambio lo que sigue
        garantizando —«este castellano existe en la app»— sigue siendo cierto
        para ellas. Listar las ocho como excepciones habría escondido lo
        mismo sin dejar la cuenta a la vista.
        """
        return any(len(v) > len(frase) and frase in v for v in self.vivas)

    def _comprobar(self, clave: str):
        esperadas = set(self.golden[clave])
        faltan = sorted(f for f in esperadas - self.vivas
                        if f not in EXCEPCIONES
                        and self._sin_prefijo(f) not in self.vivas
                        and not self._absorbida(f))
        self.assertEqual(faltan, [], (
            f"\n{len(faltan)} frase(s) castellanas de `{clave}` han "
            f"desaparecido o cambiado.\nSi el cambio es deliberado, añádelas a "
            f"EXCEPCIONES con el motivo:\n  · "
            + "\n  · ".join(faltan[:15])))

    def test_las_frases_del_frontend_siguen_intactas(self):
        self._comprobar("frontend")

    def test_las_frases_del_backend_siguen_intactas(self):
        self._comprobar("backend")


class TestElGoldenEsUtil(unittest.TestCase):
    """Un golden vacío o con basura pasaría siempre: no vigilaría nada."""

    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_tiene_el_volumen_que_se_midio(self):
        self.assertGreater(len(self.golden["frontend"]), 2000)
        self.assertGreater(len(self.golden["backend"]), 300)

    def test_no_se_ha_colado_codigo(self):
        malas = [f for f in self.golden["frontend"] + self.golden["backend"]
                 if not captura.es_frase(f)]
        self.assertEqual(malas, [], f"entradas que no son frases: {malas[:5]}")

    def test_cada_excepcion_lleva_su_motivo(self):
        sin_motivo = [k for k, v in EXCEPCIONES.items() if len(v.strip()) < 15]
        self.assertEqual(sin_motivo, [],
                         "excepciones sin explicar por qué cambió la frase")

    def test_ninguna_excepcion_sobra(self):
        """Una excepción que ya no hace falta oculta un cambio futuro."""
        vivas = (captura.frases_del_frontend() | captura.frases_del_backend()
                 | _catalogo_es())
        sobran = sorted(k for k in EXCEPCIONES if k in vivas)
        self.assertEqual(sobran, [],
                         f"excepciones que ya no aplican: {sobran[:5]}")


if __name__ == "__main__":
    unittest.main()
