"""El gate L5 comparado frame a frame — la lógica calibrada, sin subprocesos.

Este gate decide si el RPU de la comunidad se puede inyectar tal cual o si hay
que parar y preguntar al usuario. Hasta 2026-09-04 lo decidía muestreando 24
frames **emparejados por el índice de la fila del export**, no por su número de
frame. Con dos RPUs de distinto número de bloques L5 los índices no se
corresponden con nada: sobre The Mandalorian and Grogu (113.247 filas en el BD
contra 190.021 en el bin) la intersección de los dos muestreos era **un único
par** —el índice 0, que además comparaba el frame 24 del source contra el frame
0 del target—. Como ese par cayó en la zona 'intro', el cuerpo se quedó con
CERO muestras y `body_coverage` valía `1.0` por el `else` del denominador: el
gate aprobó sin haber mirado nada.

Acertó de casualidad. Los números de aquí salen de medir los RPUs de verdad.

Lo que se fija:

  * **la ausencia de bloque L5 es el valor neutro**, no "sin dato". Es como el
    Blu-ray codifica las escenas expandidas (76.774 de 190.021 frames en el
    caso medido) y sin esa regla un disco correcto sale con un 40% de
    divergencia;
  * **el porcentaje solo no basta**: veinte transiciones de medio segundo suman
    lo mismo que ocho minutos de película con otro encuadre. El contraste A/B
    de `TestElTamanoDelTramoImporta` es el motivo de que exista el umbral de
    tramo;
  * **sin evidencia del cuerpo no se aprueba**.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_gate_l5 -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
for _p in (str(APP_DIR), str(APP_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cmv40_harness import (  # noqa: E402
    CollectingLog, PhaseTestCase, RpuProps, write_artifacts,
)

from phases.cmv40_pipeline import (  # noqa: E402
    L5_NEUTRO, TRAMO_L5_MAX_SEGUNDOS, TOPE_TRAMOS_PERSISTIDOS,
    TOPE_VALORES_PERSISTIDOS, _comparar_l5, _perfil_l5, _veredicto_l5,
)

FPS = 23.976
LETTERBOX = (275, 275, 0, 0)


def mapa(total, *, valor=LETTERBOX, huecos=()):
    """Perfil L5 con bloque en todos los frames salvo en `huecos`.

    `huecos` son rangos [desde, hasta] inclusivos SIN bloque — que es como el
    BD expresa que ahí la imagen ocupa el frame entero.
    """
    fuera = set()
    for a, b in huecos:
        fuera.update(range(a, b + 1))
    return {f: valor for f in range(total) if f not in fuera}


def mapa_explicito(total, *, valor=LETTERBOX, neutro=()):
    """Igual, pero los rangos `neutro` llevan bloque con el valor neutro
    escrito — que es como lo expresa el bin de la comunidad."""
    dentro = set()
    for a, b in neutro:
        dentro.update(range(a, b + 1))
    return {f: (L5_NEUTRO if f in dentro else valor) for f in range(total)}


class TestAusenciaEsNeutro(unittest.TestCase):
    """El caso Mandalorian: el BD omite el bloque donde el bin escribe (0,0)."""

    TOTAL = 20_000
    EXPANDIDAS = [(4_000, 8_000), (12_000, 15_000)]

    def test_los_dos_masters_coinciden(self):
        src = mapa(self.TOTAL, huecos=self.EXPANDIDAS)
        tgt = mapa_explicito(self.TOTAL, neutro=self.EXPANDIDAS)
        c = _comparar_l5(src, tgt, self.TOTAL, FPS)
        self.assertEqual(c["divergentes"], 0,
                         "un frame sin bloque L5 vale (0,0,0,0), no 'sin dato'")
        self.assertEqual(c["body_coverage"], 1.0)
        self.assertEqual(_veredicto_l5(c)[0], "warn")

    def test_sin_esa_regla_seria_el_40_por_ciento(self):
        """Contraste: si los ausentes se ignorasen en vez de valer neutro, la
        divergencia sería justo el tamaño de las escenas expandidas."""
        src = mapa(self.TOTAL, huecos=self.EXPANDIDAS)
        expandidos = sum(b - a + 1 for a, b in self.EXPANDIDAS)
        self.assertEqual(self.TOTAL - len(src), expandidos)
        self.assertGreater(expandidos / self.TOTAL, 0.35)

    def test_el_perfil_del_bd_es_variable_aunque_solo_tenga_un_valor(self):
        """El bug que el gate escribía: `tgt_variable_l5=False` sobre un bin
        llamado literalmente «VARIABLE L5». El BD tiene UN valor explícito y
        76.774 frames sin bloque — eso es encuadre variable, no constante."""
        p = _perfil_l5(mapa(self.TOTAL, huecos=self.EXPANDIDAS), self.TOTAL)
        self.assertTrue(p["variable"])
        self.assertEqual(len(p["valores"]), 1)
        self.assertGreater(p["sin_bloque"], 0)

    def test_un_master_sin_huecos_y_un_solo_valor_es_constante(self):
        p = _perfil_l5(mapa(self.TOTAL), self.TOTAL)
        self.assertFalse(p["variable"])
        self.assertEqual(p["sin_bloque"], 0)


class TestElTamanoDelTramoImporta(unittest.TestCase):
    """El contraste que justifica el umbral de tramo: MISMO porcentaje de
    divergencia, repartido de dos formas distintas."""

    TOTAL = 20_000
    DIVERGENTES = 2_400        # 12% del metraje en los dos casos

    def _cmp(self, rangos):
        src = mapa(self.TOTAL)
        tgt = mapa_explicito(self.TOTAL, neutro=rangos)
        return _comparar_l5(src, tgt, self.TOTAL, FPS)

    def test_una_escena_entera_pide_confirmacion(self):
        c = self._cmp([(6_000, 6_000 + self.DIVERGENTES - 1)])
        self.assertEqual(c["divergentes"], self.DIVERGENTES)
        self.assertGreaterEqual(c["segundos_cuerpo_max"], TRAMO_L5_MAX_SEGUNDOS)
        sev, ok, why = _veredicto_l5(c)
        self.assertEqual(sev, "ack_required")
        self.assertFalse(ok)
        self.assertIn("escena entera", why)

    def test_el_mismo_porcentaje_en_transiciones_cortas_pasa(self):
        rangos = [(1_000 + i * 700, 1_000 + i * 700 + 99) for i in range(24)]
        c = self._cmp(rangos)
        self.assertEqual(c["divergentes"], 2_400)
        self.assertLess(c["segundos_cuerpo_max"], TRAMO_L5_MAX_SEGUNDOS)
        self.assertEqual(_veredicto_l5(c)[0], "warn")

    def test_los_dos_casos_tienen_la_misma_cobertura(self):
        """Si la decisión dependiera solo del porcentaje, serían el mismo caso."""
        a = self._cmp([(6_000, 6_000 + self.DIVERGENTES - 1)])
        b = self._cmp([(1_000 + i * 700, 1_000 + i * 700 + 99) for i in range(24)])
        self.assertEqual(a["divergentes"], b["divergentes"])
        self.assertAlmostEqual(a["body_coverage"], b["body_coverage"], places=2)
        self.assertNotEqual(_veredicto_l5(a)[0], _veredicto_l5(b)[0])


class TestLosCuatroCasosReales(unittest.TestCase):
    """Escenarios calcados de los proyectos del NAS que llegaron al
    refinamiento (medidos el 2026-09-04)."""

    def test_mandalorian_transiciones_desalineadas(self):
        """113.247/190.021 con bloque · 593 frames divergen (0,31%) en 5 tramos,
        el mayor de 278 frames. Los dos másters coinciden en el encuadre."""
        total = 190_021
        expandidas = [(24, 60_000), (70_000, 120_000)]
        src = mapa(total, huecos=expandidas)
        tgt = mapa_explicito(total, neutro=[(0, 60_000), (70_000, 120_000)])
        c = _comparar_l5(src, tgt, total, FPS)
        self.assertLess(c["divergentes"] / total, 0.01)
        self.assertGreater(c["body_coverage"], 0.99)
        self.assertEqual(_veredicto_l5(c)[0], "warn")

    def test_montecristo_encuadres_distintos_del_todo(self):
        """BD a pantalla completa contra un bin en 2.39:1 — 25/25 muestras
        divergían y el gate ya paraba. Tiene que seguir parando."""
        total = 256_036
        src = {f: L5_NEUTRO for f in range(total)}
        tgt = {f: (277, 277, 0, 0) for f in range(total)}
        c = _comparar_l5(src, tgt, total, FPS)
        self.assertEqual(c["body_coverage"], 0.0)
        self.assertEqual(_veredicto_l5(c)[0], "ack_required")

    def test_los_pecadores_escenas_imax(self):
        """20,5% del metraje a pantalla completa en 19 tramos, el mayor de
        12.001 frames (8,3 min). Hoy pasa como 'warn tolerable' con 0,81 de
        cobertura; con el criterio de tramo pide confirmación."""
        total = 197_927
        rangos = [(2_106, 6_359), (58_811, 64_927), (80_545, 85_821),
                  (136_248, 138_096), (153_824, 165_824)]
        src = mapa(total, valor=(384, 384, 0, 0))
        tgt = mapa_explicito(total, valor=(384, 384, 0, 0), neutro=rangos)
        c = _comparar_l5(src, tgt, total, FPS)
        self.assertGreater(c["body_coverage"], 0.70,
                           "el porcentaje por sí solo lo dejaría pasar")
        self.assertGreaterEqual(c["segundos_cuerpo_max"], TRAMO_L5_MAX_SEGUNDOS)
        self.assertEqual(_veredicto_l5(c)[0], "ack_required")

    def test_devuelvemela_tres_escenas(self):
        """9,7% en 3 tramos, el mayor de 8.566 frames (5,9 min)."""
        total = 149_256
        rangos = [(78_972, 83_275), (103_736, 105_280), (140_690, 149_255)]
        src = mapa(total, valor=(120, 120, 0, 0))
        tgt = mapa_explicito(total, valor=(120, 120, 0, 0), neutro=rangos)
        c = _comparar_l5(src, tgt, total, FPS)
        self.assertGreater(c["body_coverage"], 0.90,
                           "por porcentaje sería un 'warn' silencioso")
        self.assertEqual(_veredicto_l5(c)[0], "ack_required")


class TestSinEvidenciaNoSeAprueba(unittest.TestCase):
    """El `else 1.0` del denominador: cero muestras del cuerpo se traducían en
    «el cuerpo coincide al 100%»."""

    def test_cuerpo_vacio_no_aprueba(self):
        c = {"comparados": 1, "divergentes": 0,
             "por_zona": {"intro": [0, 1], "body": [0, 0], "outro": [0, 0]},
             "body_coverage": 1.0, "segundos_cuerpo_max": 0.0,
             "tramos": [], "mayor_tramo": None}
        sev, ok, why = _veredicto_l5(c)
        self.assertEqual(sev, "ack_required")
        self.assertFalse(ok)
        self.assertIn("no cubrió el cuerpo", why)


class TestLoQueSePersiste(unittest.TestCase):
    """La comparación recorre ~190.000 frames y el detalle NO puede acabar en
    el JSON de la sesión."""

    def test_los_tramos_van_acotados_y_ordenados_por_tamano(self):
        total = 60_000
        rangos = [(1_000 + i * 500, 1_000 + i * 500 + (i + 1) * 5)
                  for i in range(40)]
        src = mapa(total)
        tgt = mapa_explicito(total, neutro=rangos)
        c = _comparar_l5(src, tgt, total, FPS)
        self.assertEqual(len(c["tramos"]), TOPE_TRAMOS_PERSISTIDOS)
        tamanos = [t[2] for t in c["tramos"]]
        self.assertEqual(tamanos, sorted(tamanos, reverse=True))

    def test_el_histograma_de_valores_va_acotado(self):
        total = 5_000
        m = {f: (f % 40, 0, 0, 0) for f in range(total)}
        p = _perfil_l5(m, total)
        self.assertEqual(len(p["valores"]), TOPE_VALORES_PERSISTIDOS)

    def test_cada_tramo_es_cuatro_campos_serializables(self):
        total = 10_000
        src = mapa(total)
        tgt = mapa_explicito(total, neutro=[(3_000, 3_500)])
        c = _comparar_l5(src, tgt, total, FPS)
        import json
        json.dumps(c)          # no debe lanzar
        for t in c["tramos"]:
            self.assertEqual(len(t), 4)
            self.assertIn(t[3], ("intro", "body", "outro"))


class GateL5Case(PhaseTestCase):
    """Ejecuta el refinamiento de verdad, con los binarios falsos."""

    def preparar(self, *, frames, patron_src, patron_tgt, px_max=275):
        for nombre, patron in (("RPU_source.bin", patron_src),
                               ("RPU_target.bin", patron_tgt)):
            props = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                             frames=frames)
            self.tb.define_rpu(nombre, **props.as_dict())
            self.tb.define_rpu_levels(nombre, l5_pattern=patron)
            write_artifacts(self.wd, nombre, props=props)
        gates = {"l5_div": {
            "ok": False, "px_max": px_max, "soft_px": 5, "critical_px": 30,
            "warn": False, "critical": True, "severity": "ack_required",
            "why": "estático",
        }}
        return gates

    async def refinar(self, gates, frames, *, nombre_bin="", notas=""):
        from phases.cmv40_pipeline import _refinar_gate_l5
        log = CollectingLog()
        await _refinar_gate_l5(
            gates, self.wd / "RPU_source.bin", self.wd / "RPU_target.bin",
            frames, frames, log, fps=23.976,
            nombre_bin=nombre_bin, notas_sheet=notas,
        )
        return gates["l5_div"], "\n".join(log.lines)


class TestLaFaseSeEjecuta(GateL5Case):

    FRAMES = 20_000
    EXPANDIDAS = [(4_000, 9_000)]

    def _mandalorian(self):
        """BD que OMITE el bloque en las escenas expandidas contra un bin que
        escribe (0,0) ahí — los dos dicen lo mismo por vías distintas."""
        a, b = self.EXPANDIDAS[0]
        return (
            [[0, a - 1, [275, 275, 0, 0]], [b + 1, self.FRAMES - 1, [275, 275, 0, 0]]],
            [[0, a - 1, [275, 275, 0, 0]], [a, b, [0, 0, 0, 0]],
             [b + 1, self.FRAMES - 1, [275, 275, 0, 0]]],
        )

    async def test_la_ausencia_de_bloque_no_cuenta_como_divergencia(self):
        src, tgt = self._mandalorian()
        g, log = await self.refinar(self.preparar(
            frames=self.FRAMES, patron_src=src, patron_tgt=tgt), self.FRAMES)
        self.assertEqual(g["divergentes"], 0, g.get("why"))
        self.assertEqual(g["severity"], "warn")
        self.assertTrue(g["ok"])
        self.assertIn("sin bloque → neutro", log)

    async def test_el_perfil_del_bd_sale_como_variable(self):
        """El dato que el muestreo viejo escribía al revés."""
        src, tgt = self._mandalorian()
        g, log = await self.refinar(self.preparar(
            frames=self.FRAMES, patron_src=src, patron_tgt=tgt), self.FRAMES)
        self.assertTrue(g["src_variable_l5"])
        self.assertTrue(g["tgt_variable_l5"])
        self.assertIn("Encuadre VARIABLE", log)

    async def test_compara_la_pelicula_entera_no_24_muestras(self):
        src, tgt = self._mandalorian()
        g, _ = await self.refinar(self.preparar(
            frames=self.FRAMES, patron_src=src, patron_tgt=tgt), self.FRAMES)
        self.assertEqual(g["comparados"], self.FRAMES)
        self.assertEqual(g["sampled_method"], "per_frame_completo")
        self.assertNotIn("sampled_per_frame", g)

    async def test_una_escena_larga_pide_confirmacion(self):
        """Cobertura del cuerpo por encima del 90% pero 1.100 frames seguidos
        (45,9 s) con otro encuadre: es una escena, no una transición."""
        recto = [[0, self.FRAMES - 1, [275, 275, 0, 0]]]
        con_escena = [[0, 7_999, [275, 275, 0, 0]],
                      [8_000, 9_099, [0, 0, 0, 0]],
                      [9_100, self.FRAMES - 1, [275, 275, 0, 0]]]
        g, _ = await self.refinar(self.preparar(
            frames=self.FRAMES, patron_src=recto, patron_tgt=con_escena),
            self.FRAMES)
        self.assertGreater(g["body_coverage"], 0.90,
                           "por porcentaje pasaría — lo que la para es el tramo")
        self.assertEqual(g["severity"], "ack_required")
        self.assertFalse(g["ok"])

    async def test_no_toca_el_gate_si_no_estaba_en_ack(self):
        src, tgt = self._mandalorian()
        gates = self.preparar(frames=self.FRAMES, patron_src=src, patron_tgt=tgt)
        gates["l5_div"]["severity"] = "warn"
        g, log = await self.refinar(gates, self.FRAMES)
        self.assertNotIn("comparados", g)
        self.assertEqual(log, "")


class TestElCruceConLaProcedencia(GateL5Case):
    """La señal del nombre del bin: precisión 100% y recall 46% medidos sobre
    los 99 proyectos del NAS. Avisa, no decide."""

    FRAMES = 8_000
    RECTO = [[0, 7_999, [275, 275, 0, 0]]]

    async def test_avisa_cuando_el_nombre_contradice_la_medicion(self):
        g, log = await self.refinar(
            self.preparar(frames=self.FRAMES, patron_src=self.RECTO,
                          patron_tgt=self.RECTO),
            self.FRAMES, nombre_bin="Peli.2025.UHDBD_P7 FEL VARIABLE L5.bin")
        self.assertTrue(g["procedencia"]["contradice"])
        self.assertIn("se declara", log)

    async def test_el_aviso_no_cambia_la_severidad(self):
        """Es un aviso, no un gate: con recall 46% no puede decidir nada."""
        sin = await self.refinar(
            self.preparar(frames=self.FRAMES, patron_src=self.RECTO,
                          patron_tgt=self.RECTO), self.FRAMES)
        con = await self.refinar(
            self.preparar(frames=self.FRAMES, patron_src=self.RECTO,
                          patron_tgt=self.RECTO),
            self.FRAMES, nombre_bin="Peli.2025.UHDBD_P7 FEL VARIABLE L5.bin")
        self.assertEqual(sin[0]["severity"], con[0]["severity"])
        self.assertEqual(sin[0]["ok"], con[0]["ok"])

    async def test_si_la_medicion_confirma_la_variabilidad_no_contradice(self):
        variable = [[0, 3_999, [275, 275, 0, 0]], [4_000, 7_999, [0, 0, 0, 0]]]
        g, log = await self.refinar(
            self.preparar(frames=self.FRAMES, patron_src=variable,
                          patron_tgt=variable),
            self.FRAMES, nombre_bin="Peli.2025.UHDBD_P7 FEL VARIABLE L5.bin")
        self.assertFalse(g["procedencia"]["contradice"])
        self.assertNotIn("se declara", log)

    async def test_un_nombre_que_calla_no_concluye_nada(self):
        g, _ = await self.refinar(
            self.preparar(frames=self.FRAMES, patron_src=self.RECTO,
                          patron_tgt=self.RECTO),
            self.FRAMES, nombre_bin="Peli.2025.UHDBD_P7 FEL.bin")
        self.assertFalse(g["procedencia"]["declara_l5_variable"])
        self.assertFalse(g["procedencia"]["contradice"])


if __name__ == "__main__":
    unittest.main()
