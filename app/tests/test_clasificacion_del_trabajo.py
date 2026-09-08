"""Las tres clases de trabajo, y que la tabla no se quede atrás.

Antes de tocar la política de admisión había que saber qué hay. El censo de
los 94 endpoints: **70 son navegación**, 14 hacen trabajo diferido y 10 hacen
trabajo pesado con el usuario delante. Y de esos 10, **cinco corrían a ciegas**
—no aparecían en ninguna parte— aunque leen gigabytes: el análisis del disco,
la detección de contenido, la relectura de capítulos, la creación de una serie
entera y abrir un MKV.

La distinción que importa no es cuánto tarda sino **quién espera**:

  · lo DIFERIDO bloquea (409 hoy, cola después) porque nadie mira;
  · lo INTERACTIVO se registra para VERSE pero no veta a nadie — negarle abrir
    un MKV a alguien porque hay un rip deja la pestaña inservible media hora;
  · lo LIGERO ni se apunta.

El riesgo de una tabla así es que envejezca en silencio. Por eso `CLASE_POR_RUTA`
no es documentación: la ejecuta `workload.marca` como dependencia de FastAPI, y
aquí se compara contra el esquema OpenAPI real en las dos direcciones.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_clasificacion_del_trabajo -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
import workload  # noqa: E402


def _rutas_de(app) -> dict[str, object]:
    """`"MÉTODO /ruta" → route` para las rutas HTTP de la app (sin WS)."""
    out = {}
    for r in app.routes:
        metodos = getattr(r, "methods", None)
        if not metodos or not getattr(r, "path", "").startswith("/api/"):
            continue
        for m in metodos:
            if m not in ("HEAD", "OPTIONS"):
                out[f"{m} {r.path}"] = r
    return out


def _marcadas(app) -> set[str]:
    """Las rutas que llevan puesta la dependencia de `workload.marca`."""
    out = set()
    for clave, r in _rutas_de(app).items():
        deps = getattr(getattr(r, "dependant", None), "dependencies", []) or []
        if any(getattr(d.call, "__wl_interactivo__", False) for d in deps):
            out.add(clave)
    return out


class TestLaTablaCubreLaAppReal(ApiTestCase):
    """En las dos direcciones: ni falta ni sobra."""

    def test_toda_ruta_de_la_app_esta_clasificada(self):
        faltan = sorted(set(_rutas_de(self.main.app)) - set(workload.CLASE_POR_RUTA))
        self.assertEqual(
            faltan, [],
            "endpoints sin clase. Decide si es ligero (no se registra), "
            "interactivo (se ve, no bloquea) o diferido (bloquea) y añádelo a "
            "`CLASE_POR_RUTA`: " + ", ".join(faltan))

    def test_la_tabla_no_clasifica_rutas_que_ya_no_existen(self):
        """Una entrada huérfana es peor que ninguna: parece cobertura."""
        sobran = sorted(set(workload.CLASE_POR_RUTA) - set(_rutas_de(self.main.app)))
        self.assertEqual(sobran, [], "rutas en la tabla que la app no sirve")

    def test_solo_hay_tres_clases(self):
        self.assertEqual(
            set(workload.CLASE_POR_RUTA.values()),
            {workload.CLASE_LIGERO, workload.CLASE_INTERACTIVO,
             workload.CLASE_DIFERIDO})


class TestLaTablaSeEjecuta(ApiTestCase):
    """La marca puesta en el decorador tiene que coincidir con la tabla."""

    def test_las_interactivas_llevan_la_marca(self):
        esperadas = {r for r, c in workload.CLASE_POR_RUTA.items()
                     if c == workload.CLASE_INTERACTIVO}
        self.assertEqual(
            _marcadas(self.main.app), esperadas,
            "la tabla y los decoradores discrepan: una ruta interactiva sin "
            "`Depends(workload.marca(...))` no aparece en /api/activity y el "
            "dashboard la contará como que no pasa nada")

    def test_ninguna_diferida_lleva_la_marca(self):
        """Se registran ellas mismas, dentro de la tarea que hace el trabajo.

        Ponerles además la marca las registraría dos veces con claves distintas
        y la de la petición se liberaría al devolver el 200 —los endpoints de
        fase son fire-and-forget— dejando un hueco fantasma.
        """
        diferidas = {r for r, c in workload.CLASE_POR_RUTA.items()
                     if c == workload.CLASE_DIFERIDO}
        self.assertEqual(_marcadas(self.main.app) & diferidas, set())

    def test_ninguna_ligera_lleva_la_marca(self):
        ligeras = {r for r, c in workload.CLASE_POR_RUTA.items()
                   if c == workload.CLASE_LIGERO}
        self.assertEqual(_marcadas(self.main.app) & ligeras, set())

    def test_los_que_corrian_a_ciegas_ya_se_ven(self):
        """El objetivo del bloque: medir la contención real.

        Eran cinco. `create-series-sessions` salió de esta lista al pasar a la
        cola: los diferidos se registran desde su runner, cuando el trabajo
        empieza de verdad, no cuando se pide.
        """
        for ruta in ("POST /api/analyze",
                     "POST /api/disc-probe",
                     "POST /api/sessions/{session_id}/reset-chapters",
                     "POST /api/mkv/analyze"):
            self.assertIn(ruta, _marcadas(self.main.app), ruta)


class TestLoInteractivoNoBloquea(unittest.TestCase):
    """El punto entero del bloque: se registra, no se veta."""

    def setUp(self):
        workload.limpiar()
        self.addCleanup(workload.limpiar)

    def test_un_trabajo_interactivo_no_bloquea_a_nadie(self):
        workload.registrar("abrir-1", workload.TAB_MKV, "apertura de un MKV",
                           workload.CLASE_INTERACTIVO)
        self.assertIsNone(workload.bloqueado_por())

    def test_pero_sí_se_ve(self):
        workload.registrar("abrir-1", workload.TAB_MKV, "apertura de un MKV",
                           workload.CLASE_INTERACTIVO)
        self.assertEqual([t.clave for t in workload.en_curso()], ["abrir-1"])

    def test_y_cuenta_como_contencion_para_las_mediciones(self):
        """Que no lo vetemos no significa que el NAS no lo note: un
        `ffmpeg_wall_seconds` medido con un análisis de disco en marcha no
        describe la máquina, y de ahí salen `_adaptive_timeout` y el ETA."""
        workload.registrar("abrir-1", workload.TAB_MKV, "apertura de un MKV",
                           workload.CLASE_INTERACTIVO)
        self.assertTrue(workload.hay_contencion())
        self.assertFalse(workload.hay_contencion(excepto="abrir-1"))

    def test_lo_diferido_sigue_bloqueando_igual(self):
        workload.registrar("rip-1", workload.TAB_RIP, "rip de Peli")
        self.assertIsNotNone(workload.bloqueado_por())

    def test_el_default_de_registrar_es_diferido(self):
        """Los siete puntos que ya registraban no pasaron clase: si el default
        fuera interactivo, dejarían de bloquear en silencio y la app entera
        cambiaría de política sin que nadie lo pidiera."""
        workload.registrar("x", workload.TAB_RIP, "algo")
        self.assertTrue(workload.en_curso()[0].bloquea)


class TestElContextoSueltaSiempre(unittest.TestCase):
    """`ocupado` existe para que el `finally` no se pueda olvidar."""

    def setUp(self):
        workload.limpiar()
        self.addCleanup(workload.limpiar)

    def test_al_salir_bien(self):
        with workload.ocupado("k", workload.TAB_RIP, "algo"):
            self.assertIsNotNone(workload.bloqueado_por())
        self.assertIsNone(workload.bloqueado_por())

    def test_al_lanzar(self):
        with self.assertRaises(RuntimeError):
            with workload.ocupado("k", workload.TAB_RIP, "algo"):
                raise RuntimeError("boom")
        self.assertEqual(workload.en_curso(), [],
                         "un hueco sin soltar deja la app en 409 hasta reiniciar")

    def test_dos_a_la_vez_no_se_pisan(self):
        """Dos "abrir MKV" simultáneos: `registrar` es idempotente por clave,
        así que con clave compartida el `liberar` del primero soltaría el
        hueco del segundo. Por eso `marca` usa un contador."""
        with workload.ocupado("k1", workload.TAB_MKV, "abrir A",
                              workload.CLASE_INTERACTIVO):
            with workload.ocupado("k2", workload.TAB_MKV, "abrir B",
                                  workload.CLASE_INTERACTIVO):
                self.assertEqual(len(workload.en_curso()), 2)
            self.assertEqual([t.clave for t in workload.en_curso()], ["k1"])


class TestElEndpointDeActividadDistingueLasClases(ApiTestCase):

    def setUp(self):
        super().setUp()
        workload.limpiar()
        self.addCleanup(workload.limpiar)

    def test_dice_la_clase_y_si_bloquea(self):
        workload.registrar("rip-1", workload.TAB_RIP, "rip de Peli")
        workload.registrar("abrir-1", workload.TAB_MKV, "apertura de un MKV",
                           workload.CLASE_INTERACTIVO)
        r = self.client.get("/api/activity").json()
        por_clave = {t["clave"]: t for t in r["trabajos"]}
        self.assertEqual(por_clave["rip-1"]["clase"], "diferido")
        self.assertTrue(por_clave["rip-1"]["bloquea"])
        self.assertEqual(por_clave["abrir-1"]["clase"], "interactivo")
        self.assertFalse(por_clave["abrir-1"]["bloquea"])

    def test_ocupado_es_solo_lo_que_bloquea(self):
        """`ocupado` lo lee quien quiere saber si puede arrancar algo. Con un
        interactivo en curso la respuesta es que sí."""
        workload.registrar("abrir-1", workload.TAB_MKV, "apertura de un MKV",
                           workload.CLASE_INTERACTIVO)
        r = self.client.get("/api/activity").json()
        self.assertFalse(r["ocupado"])
        self.assertEqual(len(r["trabajos"]), 1, "pero se ve")


if __name__ == "__main__":
    unittest.main()
