"""El perfil L5 declarable del arnés, ejecutando el binario falso de verdad.

`dovi_tool export --levels level5=…` devuelve una lista plana de registros
`{"frame", "active_area_{top,bottom,left,right}_offset"}`, y lo que importa
—verificado sobre RPUs reales del NAS— es que **un RPU puede no emitir bloque
L5 en algunos frames**: ahí el decodificador asume el neutro (0,0,0,0). En el
Blu-ray de "The Mandalorian and Grogu" son 113.247 frames con (275,275,0,0) y
76.774 sin bloque ninguno (las escenas expandidas tipo IMAX).

El falso emitía un registro fijo por frame para los primeros 200, así que esa
ausencia no se podía expresar y los tests que la necesitan no se podían
escribir. `define_rpu_levels(l5_pattern=…)` la declara por tramos.

Aquí no se lee el fuente del arnés: se lanza `dovi_tool` (el falso) a través
de `rpu_analyze.export_levels`, que es el mismo camino que usa la app.
"""
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
for _p in (str(APP_DIR), str(APP_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cmv40_harness import PhaseTestCase, RpuProps, write_artifacts  # noqa: E402

# Un tramo desde 24 (el RPU arranca sin bloque, como el disco de referencia),
# un hueco en 1001-1499 y un segundo tramo hasta el final del RPU.
PATRON_CON_HUECO = [
    [24, 1000, [275, 275, 0, 0]],
    [1500, 1999, [275, 275, 0, 0]],
]
CUBIERTOS = set(range(24, 1001)) | set(range(1500, 2000))


class L5Case(PhaseTestCase):
    """RPU de 2.000 frames sobre el que pedir el level5 al falso."""

    FRAMES = 2000

    def setUp(self):
        super().setUp()
        self.rpu = self.tmp / "RPU_source.bin"
        props = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                         frames=self.FRAMES, has_l8=True)
        self.tb.define_rpu(self.rpu.name, **props.as_dict())
        write_artifacts(self.tmp, self.rpu.name, props=props)

    async def exportar_l5(self):
        from phases.rpu_analyze import export_levels
        niveles = await export_levels(self.rpu, ("level5",), out_dir=self.tmp)
        self.assertIsNotNone(niveles, "el export del falso debe completar")
        return niveles["level5"]


class TestPatronDeclarado(L5Case):

    async def test_el_hueco_no_produce_registros(self):
        self.tb.define_rpu_levels(self.rpu.name, l5_pattern=PATRON_CON_HUECO)
        filas = await self.exportar_l5()
        frames = {f["frame"] for f in filas}
        self.assertEqual(len(filas), len(CUBIERTOS))
        # Lo que importa es la AUSENCIA: ni el arranque ni el hueco central.
        self.assertNotIn(0, frames)
        self.assertNotIn(23, frames)
        self.assertNotIn(1001, frames)
        self.assertNotIn(1250, frames)
        self.assertNotIn(1499, frames)

    async def test_los_frames_son_los_numeros_reales(self):
        # Si el falso emitiese el índice de la lista (0..1476) los frames
        # cubiertos y los del hueco serían indistinguibles para el consumidor.
        self.tb.define_rpu_levels(self.rpu.name, l5_pattern=PATRON_CON_HUECO)
        filas = await self.exportar_l5()
        self.assertEqual({f["frame"] for f in filas}, CUBIERTOS)

    async def test_un_tramo_que_empieza_en_24_arranca_en_24(self):
        self.tb.define_rpu_levels(self.rpu.name, l5_pattern=PATRON_CON_HUECO)
        filas = await self.exportar_l5()
        self.assertEqual(filas[0]["frame"], 24)
        self.assertEqual(filas[-1]["frame"], 1999)

    async def test_los_offsets_van_en_orden_top_bottom_left_right(self):
        self.tb.define_rpu_levels(self.rpu.name, l5_pattern=[
            [0, 99, [275, 276, 10, 11]],
        ])
        filas = await self.exportar_l5()
        self.assertEqual(len(filas), 100)
        self.assertEqual(filas[0]["active_area_top_offset"], 275)
        self.assertEqual(filas[0]["active_area_bottom_offset"], 276)
        self.assertEqual(filas[0]["active_area_left_offset"], 10)
        self.assertEqual(filas[0]["active_area_right_offset"], 11)

    async def test_recorre_el_frame_count_entero_sin_tope_de_200(self):
        # El camino por defecto corta en 200; un patrón que cubra el RPU
        # entero tiene que dar los 2.000 registros.
        self.tb.define_rpu_levels(self.rpu.name, l5_pattern=[
            [0, self.FRAMES - 1, [275, 275, 0, 0]],
        ])
        filas = await self.exportar_l5()
        self.assertEqual(len(filas), self.FRAMES)
        self.assertEqual(filas[-1]["frame"], self.FRAMES - 1)

    async def test_un_tramo_que_se_pasa_del_final_se_recorta(self):
        self.tb.define_rpu_levels(self.rpu.name, l5_pattern=[
            [1900, 99999, [275, 275, 0, 0]],
        ])
        filas = await self.exportar_l5()
        self.assertEqual(filas[-1]["frame"], self.FRAMES - 1)
        self.assertEqual(len(filas), 100)


class TestSinPatron(L5Case):
    """Sin `l5_pattern` la salida tiene que ser la de siempre.

    Varios tests del arnés (perfil de luminancia, radiografía DV) consumen
    esos 200 registros con offsets 138; el patrón es aditivo, no sustituto.
    """

    async def test_sigue_dando_200_registros_con_offsets_138(self):
        filas = await self.exportar_l5()
        self.assertEqual(len(filas), 200)
        self.assertEqual([f["frame"] for f in filas], list(range(200)))
        self.assertTrue(all(f["active_area_top_offset"] == 138 for f in filas))
        self.assertTrue(all(f["active_area_bottom_offset"] == 138 for f in filas))
        self.assertTrue(all(f["active_area_left_offset"] == 0 for f in filas))
        self.assertTrue(all(f["active_area_right_offset"] == 0 for f in filas))

    async def test_declarar_otros_niveles_no_activa_el_patron(self):
        # `define_rpu_levels` sin `l5_pattern` deja el L5 en el camino viejo.
        self.tb.define_rpu_levels(self.rpu.name, l8_indices=[1, 28])
        filas = await self.exportar_l5()
        self.assertEqual(len(filas), 200)
        self.assertEqual(filas[0]["active_area_top_offset"], 138)
