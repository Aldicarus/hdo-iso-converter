"""
Los niveles L8/L9/L11 del RPU, que nunca llegaban a la radiografía DV.

`_enrich_dovi_from_json_export` (198 líneas, complejidad 82) **no rellenaba
un solo campo**. Recorría `vdr_dm_data.ext_metadata_blocks`, y en el volcado
real de `dovi_tool export` los bloques viven un nivel más abajo —separados en
`cmv29_metadata` y `cmv40_metadata`— y el nivel no es un campo (`{"level":
8}`) sino la CLAVE del bloque (`{"Level8": {...}}`). Con las dos
incompatibilidades la lista quedaba vacía, el bucle no iteraba y la función
retornaba sin tocar nada; ni siquiera lanzaba, así que el `except: pass` del
caller no tenía qué registrar.

Verificado ejecutándola sobre un P7 FEL CMv4.0 real de 176.448 frames del
repo DoviTools: **0 campos rellenados**. Los chips de trim targets y el tipo
de contenido L11 de la "Cadena de mastering" nunca han aparecido.

Reescrita sobre `rpu_analyze.export_levels`, que ya lee ese JSON bien y tiene
37 tests detrás. Los formatos de este fichero están tomados de RPUs reales:

    level8  {"frame", "length", "target_display_index", "trim_*"}
    level9  {"frame", "length", "source_primary_index"}
    level11 {"frame", "content_type", "whitepoint", "reference_mode_flag"}
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import FakeToolbox, RpuProps, write_artifacts  # noqa: E402


class NivelesCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dovi_lvl_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)
        self.rpu = self.tmp / "RPU.bin"
        props = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                         frames=50, has_l8=True)
        self.tb.define_rpu(self.rpu.name, **props.as_dict())
        write_artifacts(self.tmp, self.rpu.name, props=props)

    async def enriquecer(self, **niveles):
        from models import DoviInfo
        from phases.mkv_analyze import _enrich_dovi_from_json_export
        self.tb.define_rpu_levels(self.rpu.name, **niveles)
        dovi = DoviInfo(profile=7, el_type="FEL", cm_version="v4.0")
        await _enrich_dovi_from_json_export(dovi, str(self.rpu))
        return dovi


class TestL8TrimTargets(NivelesCase):

    async def test_un_solo_target_display(self):
        # Caso de "The Amateur": índice 1 en los 176.448 frames.
        d = await self.enriquecer(l8_indices=[1])
        self.assertTrue(d.has_l8)
        self.assertEqual(d.l8_trim_nits, [100])
        self.assertEqual(d.l8_trim_count, 1)

    async def test_dos_target_displays(self):
        # Caso de "Zootopia 2": índices 1 y 28.
        d = await self.enriquecer(l8_indices=[1, 28])
        self.assertTrue(d.has_l8)
        self.assertEqual(d.l8_trim_nits, [100, 600])
        self.assertEqual(d.l8_trim_count, 2)

    async def test_los_nits_van_ordenados_y_sin_repetir(self):
        d = await self.enriquecer(l8_indices=[35, 1, 35, 28])
        self.assertEqual(d.l8_trim_nits, [100, 600, 1000])

    async def test_un_indice_desconocido_no_impide_marcar_l8(self):
        # Si Dolby añade un índice que no está en la tabla, el nivel sigue
        # estando presente: no se puede negar el L8 por no saber sus nits.
        d = await self.enriquecer(l8_indices=[200])
        self.assertTrue(d.has_l8)
        self.assertEqual(d.l8_trim_nits, [])

    async def test_sin_l8_no_se_marca(self):
        d = await self.enriquecer(l8_indices=[])
        self.assertFalse(d.has_l8)
        self.assertEqual(d.l8_trim_nits, [])


class TestL9Primaries(NivelesCase):
    """El caso que el código viejo habría roto igualmente: el índice 0."""

    async def test_indice_cero_es_bt709_y_no_ausente(self):
        # `source_primary_index` es 0 en los RPUs reales. Un `or` lo trataría
        # como ausente y el nivel se perdería.
        d = await self.enriquecer(l9_primary=0)
        self.assertTrue(d.has_l9)
        self.assertEqual(d.l9_primaries, "BT.709")

    async def test_otros_indices_conocidos(self):
        for idx, nombre in ((9, "BT.2020"), (11, "DCI-P3"), (12, "DCI-P3 D65")):
            with self.subTest(indice=idx):
                d = await self.enriquecer(l9_primary=idx)
                self.assertEqual(d.l9_primaries, nombre)

    async def test_un_indice_desconocido_se_reporta_tal_cual(self):
        d = await self.enriquecer(l9_primary=99)
        self.assertTrue(d.has_l9)
        self.assertEqual(d.l9_primaries, "Index 99")

    async def test_sin_l9_no_se_marca(self):
        d = await self.enriquecer(l9_primary=None)
        self.assertFalse(d.has_l9)
        self.assertEqual(d.l9_primaries, "")


class TestL11ContentType(NivelesCase):

    async def test_cinema(self):
        # Es lo que trae Zootopia 2: content_type 1.
        d = await self.enriquecer(l11_content_type=1)
        self.assertTrue(d.has_l11)
        self.assertEqual(d.l11_content_type, "Cinema")

    async def test_los_demas_tipos(self):
        for ct, nombre in ((2, "Games"), (3, "Sports"), (4, "User generated")):
            with self.subTest(content_type=ct):
                d = await self.enriquecer(l11_content_type=ct)
                self.assertEqual(d.l11_content_type, nombre)

    async def test_content_type_cero_no_se_confunde_con_ausente(self):
        d = await self.enriquecer(l11_content_type=0)
        self.assertTrue(d.has_l11)
        self.assertEqual(d.l11_content_type, "Reserved")

    async def test_sin_l11_no_se_marca(self):
        d = await self.enriquecer(l11_content_type=None)
        self.assertFalse(d.has_l11)


class TestLosTresNivelesJuntos(NivelesCase):
    """Un RPU como los reales: L8 con dos targets, L9 a BT.709, L11 Cinema."""

    async def test_rellena_todo_lo_que_hay(self):
        d = await self.enriquecer(l8_indices=[1, 28], l9_primary=0,
                                  l11_content_type=1)
        self.assertEqual(d.l8_trim_nits, [100, 600])
        self.assertEqual(d.l9_primaries, "BT.709")
        self.assertEqual(d.l11_content_type, "Cinema")
        self.assertTrue(d.has_l8 and d.has_l9 and d.has_l11)

    async def test_no_inventa_l10(self):
        # L10 sale vacío en los RPUs reales probados, y no se pide.
        d = await self.enriquecer(l8_indices=[1], l9_primary=0)
        self.assertFalse(d.has_l10)
        self.assertEqual(d.l10_primaries, "")

    async def test_si_el_export_falla_no_revienta(self):
        # El enriquecimiento es opcional: un dovi_tool que falle debe dejar el
        # DoviInfo como estaba, no tumbar el análisis del MKV.
        from models import DoviInfo
        from phases.mkv_analyze import _enrich_dovi_from_json_export
        self.tb.define_rpu_levels(self.rpu.name, l8_indices=[1])
        self.tb.fail("dovi_tool", "export", rc=1, stderr="no soportado")
        dovi = DoviInfo(profile=7, el_type="FEL", cm_version="v4.0")
        await _enrich_dovi_from_json_export(dovi, str(self.rpu))
        self.assertFalse(dovi.has_l8)


class TestElParserEsCompartido(unittest.TestCase):
    """Se reusa `rpu_analyze.export_levels` en vez de tener un segundo parser
    del mismo JSON. Dos parsers es lo que permitió que este divergiera del
    formato real sin que nadie se enterara."""

    def test_usa_export_levels(self):
        import ast
        src = (APP_DIR / "phases" / "mkv_analyze.py").read_text(encoding="utf-8")
        nodo = next(
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "_enrich_dovi_from_json_export")
        # Solo el CÓDIGO: el docstring menciona la ruta vieja a propósito,
        # para explicar el bug.
        cuerpo = [x for x in nodo.body
                  if not (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant))]
        codigo = "\n".join(ast.dump(x) for x in cuerpo)
        self.assertIn("export_levels", codigo)
        self.assertNotIn("ext_metadata_blocks", codigo,
                         "esa ruta no existe en el volcado real de dovi_tool")


if __name__ == "__main__":
    unittest.main()
