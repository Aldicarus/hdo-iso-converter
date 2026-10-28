"""La herramienta de regresión real (`tools/regresion_real.py`): lo puro.

La captura se ejecuta en el NAS con ficheros de verdad y no cabe en la suite;
lo que sí cabe, y es lo que decide si una diferencia se VE, es la
normalización y la comparación. Un normalizador que tire de más hace que dos
capturas distintas salgan iguales —el peor fallo posible de una regresión—, y
pasó: la primera versión tiraba todas las claves `id`, también los ids de
pista, así que una renumeración de pistas no se habría visto.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_regresion_real -v
"""
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "tools"))

import regresion_real as RR  # noqa: E402


class ConBase(unittest.TestCase):

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="regresion_"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.ctx = RR.Contexto(self.base)
        self.ctx.caso = "caso"


class TestLaNormalizacion(ConBase):

    def test_el_id_de_una_pista_se_queda_y_el_de_una_sesion_no(self):
        x = {"id": "Peli_2026_1793111547", "tracks": [{"id": 3, "codec": "TrueHD"}]}
        n = self.ctx.normalizar(x)
        self.assertNotIn("id", n)
        self.assertEqual(n["tracks"][0]["id"], 3)

    def test_las_fechas_y_los_logs_fuera(self):
        n = self.ctx.normalizar({"created_at": "2026-10-28T10:00:00Z", "output_log": ["a"],
                                 "analysis_log": ["b"], "relato": {"x": 1}, "mkv_name": "P.mkv"})
        self.assertEqual(n, {"mkv_name": "P.mkv"})

    def test_lo_crudo_sale_de_lo_comparado_y_se_guarda_aparte(self):
        """Un sha de lo crudo dentro de lo comparado haría diferir dos
        análisis idénticos por el número de versión que MediaInfo escribe en
        su JSON (revisión de Codex del 2026-10-28)."""
        n = self.ctx.normalizar({"mediainfo_raw": {"media": {"track": []}}, "bitrate": 1}, "a")
        self.assertEqual(n, {"bitrate": 1})
        guardado = self.base / "crudo" / "caso" / "a.mediainfo_raw.json"
        self.assertTrue(guardado.exists())
        self.assertEqual(json.loads(guardado.read_text())["media"], {"track": []})
        self.assertEqual(set(self.ctx.crudos), {"a.mediainfo_raw.json"})

    def test_un_crudo_vacio_no_se_guarda(self):
        self.assertEqual(self.ctx.normalizar({"raw_summary": "", "b": 2}), {"b": 2})
        self.assertEqual(self.ctx.crudos, {})

    def test_el_sha_del_crudo_no_depende_de_la_carpeta_de_trabajo(self):
        a = self.ctx.guardar_crudo("x.txt", f"leído de {self.base}/tmp/x.mkv")
        otra = RR.Contexto(self.base / "otra")
        otra.caso = "caso"
        b = otra.guardar_crudo("x.txt", f"leído de {otra.base}/tmp/x.mkv")
        self.assertEqual(a, b)

    def test_la_ruta_de_trabajo_se_neutraliza(self):
        n = self.ctx.normalizar({"output_mkv_path": f"{self.base}/output/P.mkv"})
        self.assertEqual(n["output_mkv_path"], "<BASE>/output/P.mkv")

    def test_un_modelo_se_vuelca(self):
        from pydantic import BaseModel

        class Pista(BaseModel):
            id: int
            nombre: str

        self.assertEqual(self.ctx.normalizar(Pista(id=2, nombre="x")), {"id": 2, "nombre": "x"})


class TestLaComparacion(unittest.TestCase):

    def _difs(self, a, b):
        out = []
        RR._diferencias(a, b, "c", out)
        return out

    def test_iguales_no_dan_nada(self):
        self.assertEqual(self._difs({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}), [])

    def test_un_valor_cambiado(self):
        self.assertEqual(self._difs({"a": {"b": 1}}, {"a": {"b": 2}}), [("c.a.b", 1, 2)])

    def test_una_clave_que_aparece_o_desaparece(self):
        d = self._difs({"a": 1}, {"b": 1})
        self.assertIn(("c.a", 1, "<ya no está>"), d)
        self.assertIn(("c.b", "<no estaba>", 1), d)

    def test_una_lista_mas_larga_dice_la_longitud_y_compara_lo_comun(self):
        d = self._difs({"p": [1, 2]}, {"p": [1, 3, 4]})
        self.assertIn(("c.p (longitud)", 2, 3), d)
        self.assertIn(("c.p[1]", 2, 3), d)

    def test_el_orden_de_las_pistas_cuenta(self):
        self.assertTrue(self._difs({"t": [{"id": 1}, {"id": 2}]}, {"t": [{"id": 2}, {"id": 1}]}))


class TestCompararDeExtremoAExtremo(ConBase):

    def _escribir(self, nombre, casos):
        p = self.base / nombre
        p.write_text(json.dumps({"versiones": {"mkvmerge": nombre}, "casos": casos}))
        return str(p)

    def _comparar(self, a, b):
        salida = io.StringIO()
        with redirect_stdout(salida):
            rc = RR.main(["comparar", a, b])
        return rc, salida.getvalue()

    def test_igual_sale_con_cero(self):
        caso = {"x": {"tipo": "tab2", "derivado": {"tracks": [{"id": 0}]}, "segundos": 1}}
        rc, texto = self._comparar(self._escribir("a.json", caso), self._escribir("b.json", caso))
        self.assertEqual(rc, 0, texto)
        self.assertIn("0 diferencias y 0 fallos en total", texto)

    def test_una_diferencia_sale_con_uno_y_dice_donde(self):
        a = {"x": {"tipo": "tab2", "derivado": {"tracks": [{"id": 0, "codec": "AC-3"}]}}}
        b = {"x": {"tipo": "tab2", "derivado": {"tracks": [{"id": 0, "codec": "E-AC-3"}]}}}
        rc, texto = self._comparar(self._escribir("a.json", a), self._escribir("b.json", b))
        self.assertEqual(rc, 1)
        self.assertIn("x.tracks[0].codec", texto)

    def test_un_caso_que_falta_cuenta(self):
        caso = {"tipo": "tab2", "derivado": {"x": 1}}
        rc, texto = self._comparar(self._escribir("a.json", {"x": caso, "y": caso}),
                                   self._escribir("b.json", {"x": caso}))
        self.assertEqual(rc, 1, texto)
        self.assertIn("y: solo en la vieja", texto)

    def test_dos_fallos_iguales_no_son_un_exito(self):
        caso = {"tipo": "tab4", "derivado": {"ventana": {"rc": 2, "ficheros": 0}}}
        rc, texto = self._comparar(self._escribir("a.json", {"x": caso}),
                                   self._escribir("b.json", {"x": caso}))
        self.assertEqual(rc, 1, texto)
        self.assertIn("fallos en la vieja", texto)
        self.assertIn("rc=2", texto)

    def test_con_tipos_distintos_se_compara_solo_la_salida(self):
        salida = {"tamano": 5, "estructura": {"flujos": []}}
        a = {"x": {"tipo": "huella", "derivado": {"salida": salida}}}
        b = {"x": {"tipo": "tab4_completo",
                   "derivado": {"salida": salida, "plan": {"p": 1}, "fase_c": {"ok": True}}}}
        rc, texto = self._comparar(self._escribir("a.json", a), self._escribir("b.json", b))
        self.assertEqual(rc, 0, texto)
        self.assertIn("se compara solo la salida", texto)

    def test_lo_crudo_distinto_se_dice_pero_no_cuenta(self):
        a = {"x": {"tipo": "tab2", "derivado": {"d": 1}, "crudo": {"m.json": "aaa"}}}
        b = {"x": {"tipo": "tab2", "derivado": {"d": 1}, "crudo": {"m.json": "bbb"}}}
        rc, texto = self._comparar(self._escribir("a.json", a), self._escribir("b.json", b))
        self.assertEqual(rc, 0, texto)
        self.assertIn("1 salidas crudas distintas", texto)

    def test_una_excepcion_cuenta_como_diferencia(self):
        a = {"x": {"tipo": "tab2", "derivado": {}}}
        b = {"x": {"tipo": "tab2", "excepcion": "RuntimeError: no"}}
        rc, texto = self._comparar(self._escribir("a.json", a), self._escribir("b.json", b))
        self.assertEqual(rc, 1)
        self.assertIn("la nueva falló", texto)


class TestRetomarSoloConLasMismasHerramientas(ConBase):

    def test_con_otras_versiones_se_niega(self):
        p = self.base / "c.json"
        p.write_text(json.dumps({"versiones": {"mkvmerge": "v88"}, "casos": {"x": {}}}))
        with self.assertRaises(SystemExit):
            RR.retomar(p, {"mkvmerge": "v103"})

    def test_con_las_mismas_devuelve_lo_hecho(self):
        p = self.base / "c.json"
        p.write_text(json.dumps({"versiones": {"mkvmerge": "v103"}, "casos": {"x": {"a": 1}}}))
        self.assertEqual(RR.retomar(p, {"mkvmerge": "v103"})["casos"], {"x": {"a": 1}})


class TestCorreEnLaImagenVieja(unittest.TestCase):
    """La herramienta se ejecuta TAMBIÉN en la imagen anterior a la que se
    mide, y la de la v3.5.0 va con Python 3.10. El guard general mira la
    minor de la imagen nueva (3.12): una sintaxis de 3.11 o 3.12 aquí
    mataría la captura vieja antes de empezar (revisión de Codex del
    2026-10-28)."""

    def test_parsea_con_python_3_10(self):
        import ast
        src = (TESTS.parent / "tools" / "regresion_real.py").read_text(encoding="utf-8")
        ast.parse(src, feature_version=(3, 10))


class TestLaVentanaDelMultiplexado(unittest.TestCase):

    def test_el_split_va_tras_la_salida_y_no_toca_el_resto(self):
        cmd = ["mkvmerge", "--gui-mode", "-o", "/x/v.mkv", "--title", "T",
               "--sync", "1:-4", "a.mkv"]
        out = RR.con_ventana(cmd, "00:20:00", "00:22:00")
        self.assertEqual(out, ["mkvmerge", "--gui-mode", "-o", "/x/v.mkv",
                               "--split", "parts:00:20:00-00:22:00",
                               "--title", "T", "--sync", "1:-4", "a.mkv"])
        self.assertEqual(cmd[4], "--title", "no modifica la lista que recibe")


if __name__ == "__main__":
    unittest.main()
