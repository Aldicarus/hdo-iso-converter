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
main = None


def setUpModule():
    global _orig_cwd, main
    _orig_cwd = os.getcwd()
    os.chdir(APP_DIR)          # main.py monta StaticFiles con ruta relativa
    import main as _main
    main = _main


def tearDownModule():
    if _orig_cwd:
        os.chdir(_orig_cwd)


class TestCleanupWorkdir(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self._tmp.name) / "cmv40_peli_2025_1"
        self.wd.mkdir(parents=True)
        self.saved = {}
        self._orig_load = main.load_cmv40_session
        self._orig_save = main.save_cmv40_session
        self._orig_log = main._cmv40_log
        main.load_cmv40_session = lambda sid: self.saved.get(sid)
        main.save_cmv40_session = lambda s: self.saved.__setitem__(s.id, s)

        async def _fake_log(s, msg):
            pass
        main._cmv40_log = _fake_log

        from models import CMv40Session
        s = CMv40Session(
            id="sX", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="out.mkv", artifacts_dir=str(self.wd), phase="done",
        )
        self.saved["sX"] = s

    def tearDown(self):
        main.load_cmv40_session = self._orig_load
        main.save_cmv40_session = self._orig_save
        main._cmv40_log = self._orig_log
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
        res = await main.cmv40_cleanup("sX")
        self.assertTrue(res["ok"])
        self.assertFalse(self.wd.exists(),
                         "el workdir debe quedar borrado por completo")

    async def test_contabiliza_el_espacio_liberado(self):
        self._write("BL.hevc", 5000)
        self._write("desconocido.bin", 3000)
        res = await main.cmv40_cleanup("sX")
        self.assertEqual(res["freed_bytes"], 8000)

    async def test_marca_el_proyecto_como_archivado(self):
        self._write("BL.hevc")
        await main.cmv40_cleanup("sX")
        self.assertTrue(self.saved["sX"].archived)

    async def test_sin_workdir_no_revienta(self):
        self._tmp.cleanup()                      # el directorio ya no existe
        res = await main.cmv40_cleanup("sX")
        self.assertTrue(res["ok"])
        self.assertEqual(res["freed_bytes"], 0)


class TestPhaseArtifactsMap(unittest.TestCase):
    def test_los_artefactos_de_profile8_estan_registrados(self):
        """reset-to sí depende del mapa: los nuevos deben constar en 'injected'.

        Sin esto, rehacer desde Fase F dejaría el RPU convertido de la
        ejecución anterior en el workdir.
        """
        arts = main._CMV40_PHASE_ARTIFACTS["injected"]
        for name in ("RPU_merged_p81.bin", "_profile8_mode.json"):
            self.assertIn(name, arts)


if __name__ == "__main__":
    unittest.main()
