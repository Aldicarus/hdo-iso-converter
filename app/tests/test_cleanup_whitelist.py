"""El borrado de huérfanos valida la ruta RESUELTA, no la cadena cruda.

`_delete_orphan_path` comprobaba `path_str.startswith(prefix)` sobre el texto
que llega en el body, sin normalizar. Así que:

    /mnt/tmp/cmv40/../../library/Peliculas

empezaba por `/mnt/tmp/cmv40/`, pasaba el whitelist y llegaba a `rmtree`. Con
`/mnt/output` (los MKV terminados), `/mnt/tmp` y `/config` (las sesiones y las
API keys) montados `rw` y el contenedor en modo privileged. Los otros dos
validadores de rutas del fichero (`_safe_library_path`,
`_resolve_mkv_path_safe`) ya resolvían antes de comparar; el único que no lo
hacía era justo el que borra.

Y el inventario estaba escrito por triplicado y desincronizado: el barrido de
arranque miraba `/tmp` y `TMP_DIR`, el panel solo `/tmp` —donde los workdirs
ya no están— y el whitelist solo aceptaba `/tmp/lightprof_`, así que la ruta
real tampoco se habría podido borrar.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cleanup_whitelist -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))


class CleanupTestCase(unittest.TestCase):
    """Redirige los directorios a un tmpdir, como hace `api_harness`."""

    def setUp(self):
        import main
        import paths
        import storage
        from phases import cmv40_pipeline, iso_mount

        self.main = main
        self.tmp = Path(tempfile.mkdtemp(prefix="cleanup_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.work = self.tmp / "mnt" / "tmp"
        self.cmv40_work = self.work / "cmv40"
        self.output = self.tmp / "mnt" / "output"
        self.library = self.tmp / "mnt" / "library"
        self.mount_base = self.tmp / "mnt" / "bd"
        self.audits = self.tmp / "config" / "mkv_audits"
        for d in (self.cmv40_work, self.output, self.library,
                  self.mount_base, self.audits):
            d.mkdir(parents=True, exist_ok=True)

        parches = [
            # Los directorios viven en `paths.py` y todo el mundo los lee
            # como `paths.X`: se parchea ahí.
            (paths, "TMP_DIR", str(self.work)),
            (paths, "OUTPUT_DIR_MKV", self.output),
            (paths, "CONFIG_DIR", self.tmp / "config"),
            (cmv40_pipeline, "CMV40_WORK_BASE", self.cmv40_work),
            (iso_mount, "MOUNT_BASE", self.mount_base),
            (storage, "MKV_AUDIT_DIR", self.audits),
        ]
        originales = [(m, a, getattr(m, a)) for m, a, _ in parches]
        for m, a, v in parches:
            setattr(m, a, v)
        self.addCleanup(lambda: [setattr(m, a, v) for m, a, v in originales])


class TestBypassConDosPuntos(CleanupTestCase):

    def test_no_se_puede_salir_del_root_con_dos_puntos(self):
        """EL BUG. La víctima es un directorio que existe y no es huérfano."""
        victima = self.library / "Peliculas"
        victima.mkdir(parents=True, exist_ok=True)
        (victima / "nomeborres.mkv").write_text("peli")

        payload = str(self.cmv40_work / ".." / ".." / "library" / "Peliculas")
        ok, freed, err = self.main._delete_orphan_path(payload)

        self.assertFalse(ok, f"el whitelist aceptó {payload!r}")
        self.assertTrue(victima.exists(), "¡la biblioteca se ha borrado!")
        self.assertTrue((victima / "nomeborres.mkv").exists())

    def test_tampoco_con_una_ruta_absoluta_ajena(self):
        ok, _, err = self.main._delete_orphan_path(str(self.library))
        self.assertFalse(ok)
        self.assertIn("fuera de los roots", err)

    def test_el_propio_root_no_es_un_huerfano(self):
        """Borrar `/mnt/tmp/cmv40` entero se llevaría los workdirs de todos
        los proyectos activos."""
        ok, _, err = self.main._delete_orphan_path(str(self.cmv40_work))
        self.assertFalse(ok, err)

    def test_ni_un_fichero_de_dentro_de_un_workdir(self):
        """Solo el primer nivel: el panel lista workdirs, no sus tripas."""
        wd = self.cmv40_work / "sesion_1"
        wd.mkdir()
        (wd / "BL.hevc").write_bytes(b"x")
        ok, _, err = self.main._delete_orphan_path(str(wd / "BL.hevc"))
        self.assertFalse(ok, err)


class TestLoQueSiSePuedeBorrar(CleanupTestCase):

    def test_workdir_cmv40(self):
        wd = self.cmv40_work / "cmv40_Peli_2024_1700000000"
        wd.mkdir()
        (wd / "BL.hevc").write_bytes(b"x" * 100)
        ok, freed, err = self.main._delete_orphan_path(str(wd))
        self.assertTrue(ok, err)
        self.assertEqual(freed, 100)
        self.assertFalse(wd.exists())

    def test_workdir_de_luminancia_en_el_directorio_real(self):
        """La ruta que el whitelist NO aceptaba: los workdirs se crean en
        TMP_DIR, y solo estaba permitido `/tmp/lightprof_`."""
        wd = self.work / "lightprof_abc123"
        wd.mkdir()
        (wd / "sample.hevc").write_bytes(b"x" * 50)
        ok, freed, err = self.main._delete_orphan_path(str(wd))
        self.assertTrue(ok, err)
        self.assertFalse(wd.exists())

    def test_workdir_de_la_auditoria_de_calidad(self):
        """No tenía categoría en el panel ni entrada en el whitelist."""
        wd = self.work / "mkv_quality_audit_xyz"
        wd.mkdir()
        ok, _, err = self.main._delete_orphan_path(str(wd))
        self.assertTrue(ok, err)

    def test_mkv_tmp_del_remux_si_pero_el_mkv_final_no(self):
        tmp_mkv = self.output / "Peli (2024).mkv.tmp"
        tmp_mkv.write_bytes(b"x" * 10)
        final = self.output / "Peli (2024).mkv"
        final.write_bytes(b"x" * 10)

        ok, _, err = self.main._delete_orphan_path(str(tmp_mkv))
        self.assertTrue(ok, err)
        ok2, _, err2 = self.main._delete_orphan_path(str(final))
        self.assertFalse(ok2, "¡un MKV terminado no es basura!")
        self.assertTrue(final.exists())

    def test_cache_json_si_pero_otro_fichero_no(self):
        cache = self.audits / "deadbeef.json"
        cache.write_text("{}")
        otro = self.audits / "no_soy_cache.db"
        otro.write_text("x")
        self.assertTrue(self.main._delete_orphan_path(str(cache))[0])
        self.assertFalse(self.main._delete_orphan_path(str(otro))[0])
        self.assertTrue(otro.exists())


class TestUnaSolaTabla(CleanupTestCase):
    """Las categorías que el panel emite y las que el borrado acepta tienen
    que salir de la misma tabla: divergieron y el panel quedó mirando `/tmp`
    mientras los workdirs estaban en `/mnt/tmp`."""

    def test_las_bases_tmp_incluyen_el_directorio_real(self):
        objetivos = {t["category"]: t for t in self.main._cleanup_targets()}
        for cat in ("lightprofile_tmp", "quality_audit_tmp"):
            self.assertIn(str(self.work), objetivos[cat]["bases"],
                          f"{cat} no mira TMP_DIR")
            self.assertIn("/tmp", objetivos[cat]["bases"],
                          f"{cat} debe seguir mirando /tmp por los restos viejos")

    def test_el_panel_lista_los_workdirs_de_tab2_del_directorio_real(self):
        import time
        for nombre in ("lightprof_viejo", "mkv_quality_audit_viejo"):
            wd = self.work / nombre
            wd.mkdir()
            (wd / "big.hevc").write_bytes(b"x" * 32)
            antiguo = time.time() - 7200
            import os
            os.utime(wd, (antiguo, antiguo))
        hallados = {o["category"]: o for o in self.main._scan_orphans()}
        self.assertIn("lightprofile_tmp", hallados)
        self.assertIn("quality_audit_tmp", hallados)
        for o in hallados.values():
            if o["category"].endswith("_tmp"):
                self.assertTrue(o["safe"], o)

    def test_todo_lo_que_el_panel_lista_se_puede_borrar(self):
        """El panel no debe ofrecer botones que el whitelist rechace."""
        wd = self.cmv40_work / "huerfano"
        wd.mkdir()
        (self.work / "lightprof_x").mkdir()
        (self.work / "mkv_quality_audit_x").mkdir()
        (self.output / "Peli.mkv.tmp").write_bytes(b"x")
        (self.audits / "cafe.json").write_text('{"bad json')
        for o in self.main._scan_orphans():
            permitido, motivo = self.main._cleanup_path_allowed(o["path"])
            self.assertTrue(permitido,
                            f"el panel lista {o['category']} en {o['path']} "
                            f"pero el borrado lo rechaza: {motivo}")


if __name__ == "__main__":
    unittest.main()
