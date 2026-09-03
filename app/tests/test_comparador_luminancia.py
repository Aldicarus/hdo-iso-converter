"""El comparador A/B del perfil de luminancia (Tab 2).

Superpone la curva L1 de otro MKV sobre la del que está abierto. Existe para
responder «¿mereció la pena el upgrade a CMv4.0?» con el grading delante, en
vez de con la clasificación de `classify_l8`, que es un proxy.

Dos decisiones se fijan aquí porque deshacerlas rompería la pantalla en
silencio:

* **El endpoint no lanza análisis.** Extraer el RPU de un UHD son ~10 min y
  eso no puede dispararse por elegir un fichero en un navegador de ficheros.
  Sólo devuelve lo que ya está en `/config/mkv_audits/`.
* **El eje X va normalizado al metraje**, así que dos montajes de duración
  distinta se superponen ocupando el mismo ancho. Es deseado —así es como se
  ve un desfase— pero obliga a AVISAR de la diferencia, o el usuario compara
  escenas que no se corresponden creyendo que sí.
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
from frontend_sources import js_completo  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()


def _extraer(nombre: str) -> str:
    for marca in (f"async function {nombre}(", f"function {nombre}("):
        i = JS.find(marca)
        if i != -1:
            return JS[i:JS.index("\n}\n", i) + 3]
    raise AssertionError(f"no encuentro {nombre}() en el JS del frontend")


class TestElEndpoint(ApiTestCase):
    """`GET /api/mkv/light-profile-cached` — barato y de sólo lectura."""

    def _mkv(self, nombre="Peli (2024).mkv"):
        import paths
        p = Path(paths.OUTPUT_DIR_MKV) / nombre
        p.write_bytes(b"\x00" * 4096)
        return p

    def test_sin_file_path_es_400(self):
        self.assertEqual(self.client.get("/api/mkv/light-profile-cached").status_code, 400)

    def test_fichero_inexistente_es_404(self):
        import paths
        r = self.client.get("/api/mkv/light-profile-cached",
                            params={"file_path": f"{paths.OUTPUT_DIR_MKV}/no_existe.mkv"})
        self.assertEqual(r.status_code, 404)

    def test_path_traversal_rechazado(self):
        r = self.client.get("/api/mkv/light-profile-cached",
                            params={"file_path": "/etc/passwd"})
        self.assertIn(r.status_code, (400, 403, 404), r.text)

    def test_sin_cache_dice_por_que_y_no_analiza(self):
        p = self._mkv()
        r = self.client.get("/api/mkv/light-profile-cached",
                            params={"file_path": str(p)})
        self.assertEqual(r.status_code, 200)
        cuerpo = r.json()
        self.assertFalse(cuerpo["cached"])
        self.assertIn("sin análisis previo", cuerpo["reason"])

    def test_con_cache_pero_sin_perfil_lo_distingue(self):
        """Un MKV analizado con la auditoría vieja tiene `quality` pero no
        `light_profile`. El mensaje tiene que llevar al usuario al botón
        correcto, no decir 'no analizado'."""
        p = self._mkv()
        from storage import compute_mkv_fingerprint, write_mkv_cache_quality
        from phases.mkv_analyze import CACHE_VERSION_BASIC, CACHE_VERSION_QUALITY
        fp = compute_mkv_fingerprint(str(p))
        write_mkv_cache_quality(
            fingerprint=fp,
            cache_version_basic_existing=CACHE_VERSION_BASIC,
            cache_version_quality=CACHE_VERSION_QUALITY,
            quality_payload={"quality_classification": "real"},
        )
        cuerpo = self.client.get("/api/mkv/light-profile-cached",
                                 params={"file_path": str(p)}).json()
        self.assertFalse(cuerpo["cached"])
        self.assertIn("Análisis extendido", cuerpo["reason"])

    def test_con_perfil_lo_devuelve_con_la_duracion(self):
        p = self._mkv()
        from storage import compute_mkv_fingerprint, write_mkv_cache_quality
        from phases.mkv_analyze import CACHE_VERSION_BASIC, CACHE_VERSION_QUALITY
        fp = compute_mkv_fingerprint(str(p))
        write_mkv_cache_quality(
            fingerprint=fp,
            cache_version_basic_existing=CACHE_VERSION_BASIC,
            cache_version_quality=CACHE_VERSION_QUALITY,
            quality_payload={"light_profile": {"per_scene_max_cll": [10, 500, 200],
                                       "stats": {"peak": 500, "p50": 200}}},
        )
        cuerpo = self.client.get("/api/mkv/light-profile-cached",
                                 params={"file_path": str(p)}).json()
        self.assertTrue(cuerpo["cached"], cuerpo)
        self.assertEqual(cuerpo["light_profile"]["per_scene_max_cll"], [10, 500, 200])
        self.assertEqual(cuerpo["file_name"], "Peli (2024).mkv")
        self.assertIn("duration_seconds", cuerpo,
                      "sin duración no se puede avisar de que son montajes distintos")


@unittest.skipUnless(NODE, "node no disponible")
class TestElOverlay(unittest.TestCase):
    """El SVG del sparkline con la curva de comparación encima."""

    def _svg(self, series, opts):
        script = (
            "globalThis.Math.random = () => 0.5;\n"
            + _extraer("_rgrfFmtTime") + _extraer("_rgrfSparklineSvg")
            + f"\nprocess.stdout.write(_rgrfSparklineSvg({json.dumps(series)}, 'x', 7200,"
              f" {json.dumps(opts)}) || '');"
        )
        r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[:500]}")
        return r.stdout

    def test_sin_comparacion_no_hay_curva_extra(self):
        svg = self._svg([10, 400, 200, 900], {})
        self.assertNotIn("#e11d48", svg)

    def test_con_comparacion_se_dibuja_y_se_lista_en_la_leyenda(self):
        svg = self._svg([10, 400, 200, 900],
                        {"compareSeries": [20, 300, 250, 700], "compareLabel": "Antes.mkv"})
        self.assertIn("#e11d48", svg)
        self.assertIn("Antes.mkv", svg)

    def test_el_eje_y_abarca_las_DOS_curvas(self):
        """Si el máximo se calculara sólo sobre la serie propia, una
        comparación más brillante se saldría del chart sin decirlo."""
        def tope(svg):
            return float(svg.split('data-y-max="')[1].split('"')[0])
        sin = tope(self._svg([100, 200], {}))
        con = tope(self._svg([100, 200], {"compareSeries": [100, 5000]}))
        self.assertLess(sin, 300, f"con una serie que llega a 200 el tope es {sin}")
        self.assertGreater(con, 5000, f"la comparación llega a 5000 y el tope es {con}")

    def test_una_comparacion_con_otro_numero_de_cubos_no_se_comprime(self):
        """Las dos curvas van normalizadas a 0-100 % del metraje: la de
        comparación tiene que repartirse por TODO el ancho aunque traiga otro
        número de puntos. Mapearla con el `xOf` de la serie propia la
        aplastaría contra el margen izquierdo."""
        svg = self._svg([10, 400, 200, 900], {"compareSeries": [20, 300]})
        self.assertIn("#e11d48", svg)
        # El path de comparación debe llegar al borde derecho del área útil
        # (svgW 720 - padR 118 = 602).
        cmp_path = svg.split('stroke="#e11d48"')[0].rsplit('<path d="', 1)[1].split('"')[0]
        ultimo_x = float(cmp_path.replace(",", " ").split()[-2])
        self.assertGreater(ultimo_x, 590, f"la curva se queda corta: {cmp_path[:80]}")


if __name__ == "__main__":
    unittest.main()
