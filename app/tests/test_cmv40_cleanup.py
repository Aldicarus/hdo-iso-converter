"""
El cleanup debe vaciar el workdir entero, no una lista de nombres conocidos.

Bug real (2026-08-15, "Te van a matar"): `cmv40_cleanup` recorría
`_CMV40_PHASE_ARTIFACTS` borrando fichero a fichero, así que los artefactos
de la conversión a Profile 8.1 —añadidos al pipeline el día anterior y no
registrados en ese mapa— sobrevivieron. El proyecto quedó `archived=True`
(modo solo lectura) con 29 MB huérfanos y sin forma de volver a limpiarlo
desde la UI: el usuario tuvo que pedirlo a mano.

La lista sigue existiendo para `reset-to` (que borra solo de una fase en
adelante), pero la limpieza final ya no depende de que esté al día.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

_orig_cwd = None
cmv40_routes = None


def setUpModule():
    # El `os.chdir(APP_DIR)` que había aquí era para que
    # `StaticFiles(directory="static")` de main.py encontrara el
    # directorio; desde que se monta por ruta absoluta ya no hace
    # falta cambiar el cwd del proceso de test.
    global cmv40_routes
    from routers import cmv40 as _routes
    cmv40_routes = _routes


class TestCleanupWorkdir(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self._tmp.name) / "cmv40_peli_2025_1"
        self.wd.mkdir(parents=True)
        self.saved = {}
        self._orig_load = cmv40_routes.load_cmv40_session
        self._orig_save = cmv40_routes.save_cmv40_session
        self._orig_log = cmv40_routes._cmv40_log
        cmv40_routes.load_cmv40_session = lambda sid: self.saved.get(sid)
        cmv40_routes.save_cmv40_session = lambda s: self.saved.__setitem__(s.id, s)

        async def _fake_log(s, msg):
            pass
        cmv40_routes._cmv40_log = _fake_log

        from models import CMv40Session
        s = CMv40Session(
            id="sX", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="out.mkv", artifacts_dir=str(self.wd), phase="done",
        )
        self.saved["sX"] = s

    def tearDown(self):
        cmv40_routes.load_cmv40_session = self._orig_load
        cmv40_routes.save_cmv40_session = self._orig_save
        cmv40_routes._cmv40_log = self._orig_log
        self._tmp.cleanup()

    def _write(self, name, size=1024):
        p = self.wd / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"0" * size)
        return p

    async def test_borra_tambien_los_artefactos_desconocidos(self):
        self._write("BL.hevc", 4096)                 # conocido
        self._write("RPU_merged_p81.bin", 2048)      # nuevo (el del bug)
        self._write("_profile8_mode.json", 16)       # nuevo
        self._write("algo_que_aun_no_existe.bin")    # futuro artefacto
        res = await cmv40_routes.cmv40_cleanup("sX")
        self.assertTrue(res["ok"])
        self.assertFalse(self.wd.exists(),
                         "el workdir debe quedar borrado por completo")

    async def test_contabiliza_el_espacio_liberado(self):
        self._write("BL.hevc", 5000)
        self._write("desconocido.bin", 3000)
        res = await cmv40_routes.cmv40_cleanup("sX")
        self.assertEqual(res["freed_bytes"], 8000)

    async def test_marca_el_proyecto_como_archivado(self):
        self._write("BL.hevc")
        await cmv40_routes.cmv40_cleanup("sX")
        self.assertTrue(self.saved["sX"].archived)

    async def test_sin_workdir_no_revienta(self):
        self._tmp.cleanup()                      # el directorio ya no existe
        res = await cmv40_routes.cmv40_cleanup("sX")
        self.assertTrue(res["ok"])
        self.assertEqual(res["freed_bytes"], 0)


class TestPhaseArtifactsMap(unittest.TestCase):
    def test_los_artefactos_de_profile8_estan_registrados(self):
        """reset-to sí depende del mapa: los nuevos deben constar en 'injected'.

        Sin esto, rehacer desde Fase F dejaría el RPU convertido de la
        ejecución anterior en el workdir.
        """
        arts = cmv40_routes._CMV40_PHASE_ARTIFACTS["injected"]
        for name in ("RPU_merged_p81.bin", "_profile8_mode.json"):
            self.assertIn(name, arts)


if __name__ == "__main__":
    unittest.main()


class TestSourceHevcSeLiberaAlValidar(unittest.TestCase):
    """`source.hevc` se quedaba en disco para siempre en los jobs drop-in.

    Fase C solo lo borra cuando hubo demux, y en drop-in no lo hay. Fase H
    limpiaba los pre-mux pero no él. Medido en el NAS el 2026-08-16: 227 GB
    en cuatro ficheros de otros tantos jobs ya terminados — el 99,6 % de todo
    lo que había en /mnt/tmp.
    """

    def test_la_fase_h_lo_incluye_en_su_limpieza(self):
        from pathlib import Path
        src = (Path(__file__).parent.parent / "phases" / "cmv40_pipeline.py").read_text(
            encoding="utf-8")
        # La lista de artefactos que Fase H borra tras el rename atómico
        i = src.find('for hevc_name in ("source.hevc"')
        self.assertGreater(i, 0, "source.hevc debe estar en el cleanup de Fase H")
        bloque = src[i:i + 400]
        for nombre in ("source_injected.hevc", "DV_dual.hevc",
                       "EL_injected.hevc", "BL_injected.hevc"):
            self.assertIn(nombre, bloque)

    def test_se_borra_despues_de_mover_el_mkv(self):
        """Orden importante: primero el rename atómico, luego la limpieza. Si
        se borrara antes y el rename fallara, no habría de dónde rehacer."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "phases" / "cmv40_pipeline.py").read_text(
            encoding="utf-8")
        pos_rename = src.find("session.output_mkv_path = str(final_path)")
        pos_limpieza = src.find('for hevc_name in ("source.hevc"')
        self.assertGreater(pos_rename, 0)
        self.assertGreater(pos_limpieza, pos_rename)
