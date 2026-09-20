"""Tab 1 y Tab 2 cuentan lo mismo que Tab 3, con las mismas palabras.

Lo que se fija aquí no es que exista un objeto `relato`: es el defecto que
tenía cada pestaña y que el objeto cierra.

- **Tab 1** no sabía que lo habías cancelado. `status` vuelve a `pending`,
  `error_message` se limpia y no se apila `ExecutionRecord`, así que un rip
  cancelado era indistinguible de uno que nunca se lanzó — y si la
  cancelación interrumpía una RE-ejecución, la tarjeta se quedaba en
  «Completado» leyendo la pasada buena de ayer, con el MKV ya borrado.
- **Tab 2** derivaba el estado de cada fila en el JS, con palabras y colores
  propios: la misma idea que en las otras dos columnas se decía distinto
  según dónde la miraras.
- Y el **historial transversal** aceptaba cualquier cadena en `estado`. Un
  trabajo al que se lleva por delante un reinicio del contenedor llega con
  `running`, que no es del vocabulario: la columna lo pintaba con el chip
  rojo de error y sin mensaje, para siempre. Hay dos líneas así en el NAS.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import relato  # noqa: E402
from phases import tab1_relato, tab2_relato  # noqa: E402


def rip(**kw):
    """Una sesión de Tab 1 con la forma del summary (dict)."""
    base = {"id": "peli_2026_1", "status": "pending", "mkv_name": "Peli.mkv",
            "included_tracks": [{"type": "audio"}, {"type": "subtitle"}],
            "chapters": [{"name_custom": True}] * 12,
            "execution_history": []}
    base.update(kw)
    return base


def hechos(r):
    return {h["id"]: h for h in r["hechos"]}


class TestUnRipCanceladoSeNota(unittest.TestCase):
    """El defecto que abre este bloque, en sus dos formas."""

    def test_cancelado_no_se_confunde_con_nunca_ejecutado(self):
        sin_lanzar = tab1_relato.resolver(rip())
        parado = tab1_relato.resolver(
            rip(last_cancelled_at="2026-09-19T10:00:00Z"))
        self.assertEqual(sin_lanzar["situacion"], relato.PREPARANDO)
        self.assertEqual(parado["situacion"], relato.CANCELADO)
        # Y no es solo el id: el usuario lee dos frases distintas.
        self.assertNotEqual(sin_lanzar["situacion_rotulo"],
                            parado["situacion_rotulo"])
        self.assertNotEqual(sin_lanzar["porque"], parado["porque"])

    def test_cancelar_una_reejecucion_no_deja_la_tarjeta_en_completado(self):
        """El caso feo: el `except` del pipeline BORRA el MKV de la pasada
        anterior (escribe sobre el mismo nombre, así que lo que queda es el
        parcial), y la tarjeta leía `execution_history[-1]`, que es la pasada
        buena. Verde y «Completado» sobre un fichero que ya no existe."""
        s = rip(status="pending",
                last_cancelled_at="2026-09-19T10:00:00Z",
                execution_history=[{"status": "done", "run_number": 1}])
        self.assertEqual(tab1_relato.resolver(s)["situacion"], relato.CANCELADO)

    def test_relanzar_borra_la_marca(self):
        """`last_cancelled_at` describe la ÚLTIMA tentativa, no un historial:
        con la ejecución en marcha ya no es cierto que esté parado."""
        s = rip(status="running", last_cancelled_at="2026-09-19T10:00:00Z")
        self.assertEqual(tab1_relato.resolver(s)["situacion"], relato.EN_MARCHA)


class TestElRipCuentaLoQueSeEstablecio(unittest.TestCase):

    def test_las_pistas_y_los_capitulos_llevan_su_evidencia(self):
        h = hechos(tab1_relato.resolver(rip()))
        self.assertEqual(h["pistas"]["estado"], relato.HECHO_OK)
        self.assertIn("1", h["pistas"]["evidencia"])
        self.assertEqual(h["capitulos"]["estado"], relato.HECHO_OK)
        self.assertIn("12", h["capitulos"]["evidencia"])

    def test_los_capitulos_del_disco_se_distinguen_de_los_generados(self):
        del_disco = hechos(tab1_relato.resolver(rip()))["capitulos"]
        generados = hechos(tab1_relato.resolver(
            rip(chapters=[{"name_custom": False}] * 8)))["capitulos"]
        self.assertNotEqual(del_disco["evidencia"], generados["evidencia"])

    def test_sin_capitulos_avisa(self):
        h = hechos(tab1_relato.resolver(rip(chapters=[])))
        self.assertEqual(h["capitulos"]["estado"], relato.HECHO_AVISO)

    def test_el_dv_sin_confirmar_no_se_anuncia_como_fel(self):
        """`_detect_fel` solo sabe que hay capa de mejora; quien distingue FEL
        de MEL es `dovi_tool`, y es un paso opcional que puede fallar."""
        h = hechos(tab1_relato.resolver(rip(has_fel=True)))
        self.assertEqual(h["dolby_vision"]["estado"], relato.HECHO_AVISO)
        # Lo que NO puede decir es «Perfil 7 FEL», que es afirmar lo que el
        # análisis no confirmó. Nombrar las dos opciones sí es contar el dato.
        self.assertNotIn("Perfil", h["dolby_vision"]["evidencia"])
        self.assertIn("MEL", h["dolby_vision"]["evidencia"])

    def test_el_perfil_sale_del_bdinfo_cuando_esta(self):
        s = rip(bdinfo_result={"video_tracks": [
            {"dovi": {"profile": 7, "el_type": "FEL", "cm_version": "v2.9"}}]})
        h = hechos(tab1_relato.resolver(s))
        self.assertEqual(h["dolby_vision"]["estado"], relato.HECHO_OK)
        self.assertIn("7 FEL", h["dolby_vision"]["evidencia"])
        self.assertIn("v2.9", h["dolby_vision"]["evidencia"])

    def test_terminado_con_avisos_no_se_ve_igual_que_terminado(self):
        """El recuento de la validación vivía SOLO en una línea del log."""
        limpio = tab1_relato.resolver(rip(status="done"))
        con_avisos = tab1_relato.resolver(
            rip(status="done", last_validation_warnings=3))
        self.assertEqual(hechos(limpio)["validacion"]["estado"], relato.HECHO_OK)
        self.assertEqual(hechos(con_avisos)["validacion"]["estado"],
                         relato.HECHO_AVISO)
        self.assertIn("3", hechos(con_avisos)["validacion"]["evidencia"])
        self.assertNotEqual(limpio["porque"], con_avisos["porque"])

    def test_la_validacion_no_consta_si_no_ha_terminado(self):
        """Un hecho que no se ha comprobado no se enseña como comprobado."""
        self.assertNotIn("validacion", hechos(tab1_relato.resolver(rip())))

    def test_el_plural_de_una_discrepancia(self):
        """Ni `discrepancies` ni `discrepàncies` se forman añadiendo una
        letra, así que el truco del sufijo no vale y son dos claves."""
        una = hechos(tab1_relato.resolver(
            rip(status="done", last_validation_warnings=1)))["validacion"]
        self.assertNotIn("{n}", una["evidencia"])
        self.assertTrue(una["evidencia"].startswith("1 "), una["evidencia"])


class TestLaEtapaDelRip(unittest.TestCase):

    def test_la_fase_en_marcha_la_trae_el_router(self):
        """Ni el turno ni la fase están en la sesión: el turno lo sabe la cola
        y la fase vive en `_rip_progress`, memoria del proceso."""
        r = tab1_relato.resolver(rip(status="running"), fase_en_curso="extract")
        self.assertEqual(r["etapa"]["id"], "extraer")
        self.assertTrue(r["etapa"]["rotulo"])
        self.assertEqual(r["etapa"]["total"], len(tab1_relato.ETAPAS))

    def test_en_cola_aunque_el_status_no_se_haya_escrito(self):
        self.assertEqual(
            tab1_relato.resolver(rip(), en_cola=True)["situacion"],
            relato.ESPERANDO_TURNO)

    def test_las_etapas_tienen_todas_rotulo(self):
        r = tab1_relato.resolver(rip())
        for e in r["etapas"]:
            self.assertTrue(e["rotulo"], e["id"])
            self.assertNotIn("relato.", e["rotulo"], e["id"])


class TestElResolutorAceptaLasDosFormas(unittest.TestCase):
    """El sidebar se pinta con los dicts cacheados del summary y la ficha con
    el modelo. Reconstruir 73 `Session` en cada listado solo para contar lo
    mismo sería pagar el cache dos veces."""

    def test_el_modelo_y_el_dict_dicen_lo_mismo(self):
        from models import Session
        s = Session(id="x", iso_path="/mnt/isos/x.iso", status="done",
                    last_validation_warnings=2)
        d = json.loads(s.model_dump_json())
        self.assertEqual(tab1_relato.resolver(s)["situacion"],
                         tab1_relato.resolver(d)["situacion"])
        self.assertEqual(hechos(tab1_relato.resolver(s))["validacion"],
                         hechos(tab1_relato.resolver(d))["validacion"])


class TestElMkvAnalizado(unittest.TestCase):

    def mkv(self, **kw):
        base = {"ruta": "/mnt/output/x.mkv", "existe": True,
                "tiene_basico": True, "tiene_extendido": False,
                "tiene_luminancia": False}
        base.update(kw)
        return base

    def test_las_cuatro_situaciones(self):
        self.assertEqual(
            tab2_relato.resolver(self.mkv(existe=False))["situacion"],
            relato.NO_DISPONIBLE)
        self.assertEqual(
            tab2_relato.resolver(self.mkv(tiene_extendido=True))["situacion"],
            relato.TERMINADO)
        self.assertEqual(tab2_relato.resolver(self.mkv())["situacion"],
                         relato.PREPARANDO)
        self.assertEqual(
            tab2_relato.resolver(self.mkv(tiene_basico=False))["situacion"],
            relato.CADUCADO)

    def test_el_fichero_ausente_gana(self):
        """Sin MKV que abrir, qué análisis tenga guardado es secundario."""
        self.assertEqual(
            tab2_relato.resolver(
                self.mkv(existe=False, tiene_extendido=True))["situacion"],
            relato.NO_DISPONIBLE)

    def test_el_mismo_id_no_se_llama_igual_que_en_un_rip(self):
        """`preparando` en un rip es «sin ejecutar»; en un MKV analizado es
        «analizado». El estado es el mismo —queda trabajo— y la palabra no
        puede serlo."""
        self.assertNotEqual(
            tab2_relato.resolver(self.mkv())["situacion_rotulo"],
            tab1_relato.resolver(rip())["situacion_rotulo"])

    def test_los_hechos_estan_siempre_los_cuatro(self):
        """Una etiqueta que no aplica se apaga, no desaparece: si no, la
        posición de cada dato se mueve entre filas."""
        for caso in (self.mkv(), self.mkv(existe=False),
                     self.mkv(tiene_extendido=True, tiene_luminancia=True)):
            h = hechos(tab2_relato.resolver(caso))
            self.assertEqual(set(h), {"fichero", "analisis_basico",
                                      "analisis_extendido", "perfil_luminancia"})

    def test_el_fichero_que_no_esta_avisa_pero_no_es_un_error(self):
        h = hechos(tab2_relato.resolver(self.mkv(existe=False)))
        self.assertEqual(h["fichero"]["estado"], relato.HECHO_AVISO)
        self.assertNotEqual(h["fichero"]["estado"], relato.HECHO_FALLO)

    def test_no_tiene_etapas_porque_no_es_un_trabajo_por_fases(self):
        r = tab2_relato.resolver(self.mkv())
        self.assertEqual(r["etapas"], [])
        self.assertEqual(r["decision"]["estado"], relato.DECISION_NO_PROCEDE)


class TestLasTresFormasSonLaMisma(unittest.TestCase):
    """Un JS que ramifique por pestaña para leer el relato ya no sería un
    relato común."""

    def test_las_mismas_claves(self):
        from phases.cmv40_relato import resolver as cmv40
        from models import CMv40Session
        esperadas = set(cmv40(CMv40Session(
            id="c", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="x.mkv")))
        self.assertEqual(set(tab1_relato.resolver(rip())), esperadas)
        self.assertEqual(
            set(tab2_relato.resolver({"existe": True, "tiene_basico": True})),
            esperadas)

    def test_ningun_rotulo_sale_como_clave_cruda(self):
        """`tr()` devuelve la clave cuando no existe, así que una clave que
        falte se PINTA."""
        for r in (tab1_relato.resolver(rip(status="done")),
                  tab1_relato.resolver(rip(status="error")),
                  tab2_relato.resolver({"existe": False})):
            textos = [r["situacion_rotulo"], r["porque"]]
            textos += [h["que"] for h in r["hechos"]]
            textos += [h["evidencia"] for h in r["hechos"]]
            for t in textos:
                self.assertNotIn("relato.", t)
                self.assertNotIn("historial.", t)


class TestElHistorialNoAceptaCualquierCosa(unittest.TestCase):
    """Dos líneas del NAS dicen `running` y lo dirán siempre: el fichero es
    append-only. Son los dos episodios de Juego de Tronos que un deploy pilló
    a mitad de la cola el 2026-09-12."""

    def setUp(self):
        import tempfile, pathlib, paths, historial
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._cfg = paths.CONFIG_DIR
        paths.CONFIG_DIR = pathlib.Path(self.tmp.name)
        self.addCleanup(lambda: setattr(paths, "CONFIG_DIR", self._cfg))
        self.historial = historial

    def test_un_estado_que_no_es_del_vocabulario_se_anota_interrumpido(self):
        self.historial.anotar(
            id="s1", tab=self.historial.TAB_RIP, tipo=self.historial.TIPO_RIP,
            que="Conversión", inicio=datetime.now(timezone.utc),
            estado="running")
        # Se mira la LÍNEA ESCRITA, no lo que devuelve `leer`: el lector
        # normaliza también, así que por ahí la validación de escritura
        # parecería estar haciendo algo aunque no hiciera nada — y el fichero
        # es append-only, o sea que lo que se escribe mal se queda mal.
        crudo = json.loads(self.historial.ruta().read_text().splitlines()[0])
        self.assertEqual(crudo["estado"], self.historial.ESTADO_INTERRUMPIDO)
        # Y con un motivo: sin él la columna enseña un chip y nada más.
        self.assertTrue(crudo["error"])

    def test_los_estados_buenos_pasan_tal_cual(self):
        for estado in self.historial.ESTADOS:
            self.historial.anotar(
                id="s", tab=self.historial.TAB_RIP,
                tipo=self.historial.TIPO_RIP, que="x",
                inicio=datetime.now(timezone.utc), estado=estado)
        vistos = {r["estado"] for r in self.historial.leer()}
        self.assertEqual(vistos, set(self.historial.ESTADOS))

    def test_las_lineas_ya_escritas_se_normalizan_al_leer(self):
        """El historial NO se migra —es la regla desde que existe—, así que
        las dos líneas del NAS se arreglan al servirlas, igual que ya se hace
        con el nombre de los trabajos renombrados."""
        import pathlib
        f = pathlib.Path(self.tmp.name) / "historial.jsonl"
        f.write_text(json.dumps({
            "id": "Juego_de_tronos_S05E05", "tab": "rip", "tipo": "rip",
            "que": "Conversión a MKV", "inicio": "2026-09-12T21:01:21+00:00",
            "fin": "2026-09-12T21:07:56+00:00", "segundos": 394.3,
            "estado": "running", "error": None, "ref_log": None}) + "\n",
            encoding="utf-8")
        (r,) = self.historial.leer()
        self.assertEqual(r["estado"], self.historial.ESTADO_INTERRUMPIDO)
        self.assertTrue(r["error"])


if __name__ == "__main__":
    unittest.main()


def _objeto_js(src: str, nombre: str) -> dict[str, str]:
    """Las claves de un `const X = { ... };` plano del frontend.

    Se cuentan llaves en vez de emparejar con un regex: es la trampa que este
    subsistema ya pagó cuatro veces. Aquí los objetos son planos, pero contar
    cuesta lo mismo que confiar en que lo sigan siendo.
    """
    import re
    m = re.search(r"^const %s = \{" % re.escape(nombre), src, re.M)
    assert m, f"no está {nombre}"
    i = src.index("{", m.start())
    prof = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            prof += 1
        elif src[j] == "}":
            prof -= 1
            if prof == 0:
                cuerpo = src[i + 1:j]
                break
    else:
        raise AssertionError(f"{nombre} sin cerrar")
    return {k: v for k, v in re.findall(r"(\w+)\s*:\s*\[?'([^']*)'", cuerpo)}


class TestLaPinturaCubreElVocabulario(unittest.TestCase):
    """El servidor resuelve QUÉ pasa y el JS dice cómo se ve. Una situación
    sin fila en la tabla de pintado cae al respaldo y se pinta como otra cosa,
    sin dar ningún error — que es el modo de fallo de siempre."""

    def setUp(self):
        from frontend_sources import pieza_de
        _, self.core = pieza_de("pinturaDeSituacion")

    def test_cada_situacion_tiene_chip_y_se_sabe_su_acento(self):
        chips = _objeto_js(self.core, "ICONO_DE_SITUACION")
        self.assertEqual(set(chips), set(relato.SITUACIONES))

    def test_los_chips_existen_en_el_catalogo_de_iconos(self):
        """Un nombre que no está en `_ICONOS_ESTADO` deja el hueco vacío."""
        import re
        chips = _objeto_js(self.core, "ICONO_DE_SITUACION")
        m = re.search(r"^const _ICONOS_ESTADO = \{(.*?)^\};", self.core,
                      re.M | re.S)
        self.assertTrue(m)
        catalogo = set(re.findall(r"^  (\w+):", m.group(1), re.M))
        self.assertEqual(set(chips.values()) - catalogo, set())

    def test_el_acento_solo_usa_clases_que_existen_en_el_css(self):
        import pathlib
        css = pathlib.Path(__file__).resolve().parents[1] / "static" / "style.css"
        texto = css.read_text(encoding="utf-8")
        for clase in _objeto_js(self.core, "ACENTO_DE_SITUACION").values():
            self.assertIn("." + clase, texto, clase)


class TestNingunPillFiltraElVacio(unittest.TestCase):
    """Un pill cuyo `data-filter` no esté en la tabla no filtra: devuelve la
    lista vacía y el usuario concluye que no tiene proyectos."""

    def _pills(self, n: int) -> set[str]:
        """Los `data-filter` de la columna de la pestaña `n`.

        El corte va de `id="sidebar-tab-N"` al siguiente, no al primer
        `</div>`: el contenedor tiene hijos, así que ese cierre es el de la
        cabecera y el trozo salía sin ninguna pill — un guard que pasa en
        verde vigilando el vacío.
        """
        import pathlib, re
        html = (pathlib.Path(__file__).resolve().parents[1]
                / "static" / "index.html").read_text(encoding="utf-8")
        i = html.index(f'id="sidebar-tab-{n}"')
        j = html.find(f'id="sidebar-tab-{n + 1}"')
        trozo = html[i:j if j > 0 else len(html)]
        pills = set(re.findall(r'data-filter="(\w+)"', trozo)) - {"all"}
        assert pills, f"sin pills en sidebar-tab-{n}"
        return pills

    def test_tab1(self):
        from frontend_sources import pieza_de
        _, src = pieza_de("_situacionDeSesion")
        tabla = _objeto_js(src, "_PILL_SITUACIONES")
        self.assertEqual(self._pills(1), set(tabla))
        for sits in tabla.values():          # y cada pill pide algo real
            self.assertIn(sits, relato.SITUACIONES)

    def test_tab2(self):
        from frontend_sources import pieza_de
        _, src = pieza_de("_situacionDeMkv")
        tabla = _objeto_js(src, "_MKV_PILL_SITUACIONES")
        self.assertEqual(self._pills(2), set(tabla))

    def test_los_pills_de_tab2_cubren_todas_las_tarjetas(self):
        """La regla de esta columna: «Sin extendido» incluye también las de
        caché caducada. Si exigiera el básico al día, esas solo saldrían con
        «Todos» y quien filtra las daría por desaparecidas."""
        import re
        from frontend_sources import pieza_de
        _, src = pieza_de("_situacionDeMkv")
        m = re.search(r"const _MKV_PILL_SITUACIONES = \{(.*?)\};", src, re.S)
        cubiertas = set(re.findall(r"'(\w+)'", m.group(1)))
        posibles = {tab2_relato.resolver(t)["situacion"] for t in (
            {"existe": False}, {"existe": True, "tiene_extendido": True},
            {"existe": True, "tiene_basico": True}, {"existe": True})}
        self.assertEqual(posibles - cubiertas, set())


class TestNadieVuelveADerivarElEstadoAMano(unittest.TestCase):
    """La regla vive en el servidor y el JS la LEE. Es el mismo guard que
    `TestNadieVuelveADerivarloAMano` para Tab 3, y por el mismo motivo."""

    #: función → por qué puede leer `execution_history` cruda
    EXENTAS = {
        "renderExecutionHistory":
            "es la tabla del historial: su trabajo ES enseñar esas filas, "
            "una por ejecución, con su estado y sus tiempos por fase",
        "_ripTimelineHTML":
            "lee `phase_elapsed` de la última ejecución para el transcurrido "
            "por fase de la tira; el estado de cada paso lo decide aparte",
        "_getExecRecord":
            "devuelve el registro que el visor de log pide por índice, sin "
            "mirar su estado",
    }

    def _funciones(self, src: str):
        import re
        for m in re.finditer(r"^function (\w+)\(", src, re.M):
            nombre, i = m.group(1), m.start()
            prof, abierto = 0, False
            for j in range(i, len(src)):
                if src[j] == "{":
                    prof += 1; abierto = True
                elif src[j] == "}":
                    prof -= 1
                    if abierto and prof == 0:
                        yield nombre, src[i:j + 1]
                        break

    def test_el_estado_de_la_tarjeta_no_sale_del_historial(self):
        from frontend_sources import pieza_de
        _, src = pieza_de("_situacionDeSesion")
        malas = []
        for nombre, cuerpo in self._funciones(src):
            if nombre in self.EXENTAS:
                continue
            codigo = "\n".join(l for l in cuerpo.splitlines()
                               if not l.lstrip().startswith(("//", "*", "/*")))
            if "execution_history" in codigo:
                malas.append(nombre)
        self.assertEqual(sorted(malas), [], "\n  · ".join([""] + sorted(malas)))

    def test_cada_exencion_sigue_existiendo(self):
        from frontend_sources import pieza_de
        _, src = pieza_de("_situacionDeSesion")
        nombres = {n for n, _ in self._funciones(src)}
        self.assertEqual(set(self.EXENTAS) - nombres, set())

    def test_cada_exencion_lleva_su_motivo(self):
        for nombre, motivo in self.EXENTAS.items():
            self.assertGreater(len(motivo), 20, nombre)


class TestElRegistroDeLosRotulos(unittest.TestCase):
    """Un rótulo de estado NOMBRA la situación; no se dirige al usuario.

    Lo reportó el usuario el 2026-09-20 sobre los literales de este hilo:
    «En marcha» donde la app dice «En curso», «Lo paraste tú» donde tocaba
    «Cancelado», y un tooltip que decía «Ya está en curso. El botón vuelve
    cuando termine», que es una conversación, no un rótulo.

    El guard se queda en los rótulos —las situaciones y los titulares— y no
    en la prosa: ahí la segunda persona sigue siendo el registro de la app
    («Pega aquí tu clave»), que es lo que dice `REGISTRO.md`. Lo que no cabe
    en una etiqueta de estado es hablarle a nadie.
    """

    #: marcas de segunda persona, por lengua
    SEGUNDA_PERSONA = {
        "es": (r"\btú\b", r"\bti\b", r"aste\b", r"iste\b", r"\bdecides\b",
               r"\bpuedes\b", r"\btienes\b", r"\bespera\b"),
        "en": (r"\byou\b", r"\byour\b"),
        "ca": (r"\btu\b", r"\bvas\b", r"\bpots\b", r"\btens\b"),
    }
    #: un rótulo que no cabe en un chip ya no es un rótulo
    TOPE = 32

    def _rotulos(self, lang):
        import json
        from pathlib import Path
        cat = json.loads((Path(__file__).resolve().parents[1] / "i18n" /
                          f"{lang}.json").read_text(encoding="utf-8"))
        return {k: v for k, v in cat.items()
                if k.startswith("relato.") and
                ("situacion_" in k or k.startswith("relato.titulo_"))}

    def test_ningun_rotulo_se_dirige_al_usuario(self):
        import re
        malos = []
        for lang, marcas in self.SEGUNDA_PERSONA.items():
            for k, v in self._rotulos(lang).items():
                for m in marcas:
                    if re.search(m, v, re.I):
                        malos.append(f"[{lang}] {k}: «{v}»")
        self.assertEqual(sorted(malos), [], "\n  · ".join([""] + sorted(malos)))

    def test_ninguna_situacion_es_una_frase(self):
        """Solo las SITUACIONES: son el chip y el subtítulo de la tarjeta, y
        ahí no cabe una oración. Los titulares del modal son otra cosa —
        encabezan un bloque— y no llevan tope de largo."""
        largos = [f"[{lang}] {k}: «{v}»" for lang in self.SEGUNDA_PERSONA
                  for k, v in self._rotulos(lang).items()
                  if "situacion_" in k and len(v) > self.TOPE]
        self.assertEqual(sorted(largos), [], "\n  · ".join([""] + sorted(largos)))

    def test_el_guard_mira_algo(self):
        """Una lista vacía pasaría en verde sin vigilar nada."""
        self.assertGreaterEqual(len(self._rotulos("es")), 15)


class TestNingunaClaveDelRelatoSeQuedaSinConsumidor(unittest.TestCase):
    """Una clave que nadie pide es texto muerto que parece cobertura.

    Al revisar los literales de este hilo había **seis**: las cinco primeras
    redacciones del veredicto en `tab3.*`, que el paso a `relato.*` dejó
    atrás, y un `relato.titulo_error` que nunca llegó a cablearse. Ninguna
    daba un error — sencillamente no se leían.

    Se comprueba solo el espacio de nombres `relato.*`, donde la composición
    de claves es conocida y acotada (`tr(f'relato.etapa_{id}')` y sus cuatro
    hermanas). Un guard general daría falsos positivos con patrones como
    `tr(etiqueta + '_uno')`, y un guard que denuncia de más se desactiva.
    """

    def test_todas_se_piden_desde_el_codigo(self):
        import json, re
        from pathlib import Path
        app = Path(__file__).resolve().parents[1]
        fuente = "\n".join(p.read_text(encoding="utf-8") for p in app.rglob("*.py")
                           if "tests" not in p.parts)
        literales = set(re.findall(r"""['"](relato\.[\w.]+)['"]""", fuente))
        # `tr(f"relato.etapa_{etapa}")` → el prefijo `relato.etapa_`
        prefijos = set(re.findall(r"""f['"](relato\.[\w.]*?)\{""", fuente))
        cat = json.loads((app / "i18n" / "es.json").read_text(encoding="utf-8"))
        huerfanas = [k for k in cat if k.startswith("relato.")
                     and k not in literales
                     and not any(k.startswith(p) for p in prefijos)]
        self.assertEqual(sorted(huerfanas), [],
                         "\n  · ".join(["claves sin consumidor:"] + sorted(huerfanas)))

    def test_el_guard_encuentra_los_dos_caminos(self):
        """Si la detección de prefijos se rompiera, el test de arriba
        denunciaría las 30 claves compuestas y alguien lo desactivaría."""
        import re
        from pathlib import Path
        app = Path(__file__).resolve().parents[1]
        fuente = "\n".join(p.read_text(encoding="utf-8") for p in app.rglob("*.py")
                           if "tests" not in p.parts)
        self.assertTrue(re.findall(r"""['"](relato\.[\w.]+)['"]""", fuente))
        self.assertTrue(re.findall(r"""f['"](relato\.[\w.]*?)\{""", fuente))
