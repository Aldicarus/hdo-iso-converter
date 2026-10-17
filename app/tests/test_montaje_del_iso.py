"""El montaje de un ISO, ejecutado con un `mount` falso.

`iso_mount` tenía un 5 % de cobertura (auditoría de la suite del
2026-10-17): el loop mount necesita privilegios y ningún test lo podía
ejecutar, así que todo lo que cuelga de un origen ISO —el argv del `mount`,
el desmontaje en el `finally` aunque el rip falle, la limpieza del punto de
montaje, el `umount -l` de reserva— solo se probaba en el NAS.

El `mount` y el `umount` del arnés (`cmv40_harness`) resuelven por nombre en
el PATH igual que el resto, así que no se toca el código de producción.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_montaje_del_iso -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import CollectingLog, FakeToolbox  # noqa: E402


class MontajeCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        from phases import iso_mount
        self.im = iso_mount
        self.tmp = Path(tempfile.mkdtemp(prefix="montaje_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.base = self.tmp / "bd"
        orig = iso_mount.MOUNT_BASE
        iso_mount.MOUNT_BASE = self.base
        self.addCleanup(setattr, iso_mount, "MOUNT_BASE", orig)
        self.tb = FakeToolbox(self.tmp).install()
        self.addCleanup(self.tb.uninstall)
        self.iso = self.tmp / "isos" / "Peli (2024).iso"
        self.iso.parent.mkdir()
        self.iso.write_bytes(b"\x00" * 4096)

    def puntos(self):
        return list(self.base.glob("*")) if self.base.exists() else []


class TestMontarYDesmontar(MontajeCase):

    async def test_monta_en_solo_lectura_y_desmonta_al_salir(self):
        self.tb.define_iso(self.iso.name, mpls={"00800.mpls": 4096})
        async with await self.im.Source.open(str(self.iso)) as src:
            raiz = Path(src.bdmv_root)
            self.assertTrue((raiz / "BDMV" / "PLAYLIST" / "00800.mpls").exists())
        [m] = self.tb.find("mount")
        self.assertEqual(m.argv[:4], ["-t", "udf", "-o", "ro,loop"])
        self.assertEqual(Path(m.argv[4]), self.iso)
        # El punto EXACTO que se montó: desmontar `/mnt/bd` entero también
        # «vaciaría los puntos» y dejaría el ISO montado (revisión de Codex).
        [u] = self.tb.find("umount")
        self.assertEqual(u.argv, [m.argv[5]], "se desmonta otra ruta que la montada")
        self.assertEqual(self.puntos(), [], "queda el punto de montaje")

    async def test_desmonta_aunque_falle_lo_de_dentro(self):
        self.tb.define_iso(self.iso.name, mpls={"00800.mpls": 4096})
        with self.assertRaises(RuntimeError):
            async with await self.im.Source.open(str(self.iso)):
                raise RuntimeError("el rip ha fallado")
        [m], [u] = self.tb.find("mount"), self.tb.find("umount")
        self.assertEqual(u.argv, [m.argv[5]])
        self.assertEqual(self.puntos(), [])

    async def test_un_mount_que_falla_no_deja_el_punto(self):
        self.tb.fail("mount", "*", rc=32, stderr="mount: permission denied\n")
        with self.assertRaises(RuntimeError) as ctx:
            await self.im.mount_iso(str(self.iso))
        self.assertIn("32", str(ctx.exception))
        self.assertEqual(self.puntos(), [])

    async def test_un_iso_sin_bdmv_se_desmonta_y_se_dice(self):
        self.tb.define_iso(self.iso.name, mpls=None)
        with self.assertRaises(RuntimeError) as ctx:
            await self.im.mount_iso(str(self.iso))
        self.assertIn("BDMV", str(ctx.exception))
        self.assertTrue(self.tb.ran("umount"))
        self.assertEqual(self.puntos(), [])

    async def test_si_el_umount_falla_prueba_el_lazy(self):
        self.tb.define_iso(self.iso.name, mpls={"00800.mpls": 4096})
        punto = await self.im.mount_iso(str(self.iso))
        self.tb.fail_when_arg("umount", punto, rc=32, stderr="target is busy\n")
        await self.im.unmount_iso(punto)
        llamadas = [c.argv for c in self.tb.find("umount")]
        self.assertIn(["-l", punto], llamadas, llamadas)

    async def test_el_umount_falso_solo_desmonta_lo_montado(self):
        """Fidelidad del fake: el real sale con 32 («not mounted») ante una
        ruta que no es un punto de montaje. El falso vaciaba cualquiera, y
        con eso desmontar `/mnt/bd` entero pasaba en verde."""
        import subprocess
        self.base.mkdir(parents=True, exist_ok=True)
        (self.base / "otro").mkdir()
        r = subprocess.run(["umount", str(self.base)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 32)
        self.assertIn("not mounted", r.stderr)
        self.assertTrue((self.base / "otro").exists(), "ha vaciado una ruta que no montó")

    async def test_un_fichero_que_no_es_iso_no_se_monta(self):
        otro = self.iso.with_suffix(".img")
        otro.write_bytes(b"\x00")
        with self.assertRaises(RuntimeError):
            await self.im.mount_iso(str(otro))
        self.assertFalse(self.tb.ran("mount"))


class TestUnRipDesdeUnIso(MontajeCase):
    """El orquestador de Tab 1 entero con un origen ISO: monta, extrae desde
    el MPLS del punto de montaje y desmonta, también si mkvmerge falla."""

    def setUp(self):
        super().setUp()
        import paths
        import storage
        from phases import phase_d, phase_e
        from routers import tab1
        self.tab1, self.storage = tab1, storage
        tab1._session_save_locks.clear()
        tab1._session_save_throttle.clear()
        self.config = self.tmp / "config"
        self.salida = self.tmp / "output"
        self.trabajo = self.tmp / "work"
        for d in (self.config, self.salida, self.trabajo):
            d.mkdir()
        parches = [(storage, "CONFIG_DIR", self.config), (paths, "CONFIG_DIR", self.config),
                   (paths, "TMP_DIR", str(self.trabajo)),
                   (phase_e, "OUTPUT_DIR", str(self.salida)), (phase_d, "MIN_MPLS_SIZE", 200)]
        originales = [(m, a, getattr(m, a)) for m, a, _ in parches]
        for m, a, v in parches:
            setattr(m, a, v)
        self.addCleanup(lambda: [setattr(m, a, v) for m, a, v in originales])
        getattr(storage, "_sessions_summary_by_file", {}).clear()
        self.tb.define_iso(self.iso.name, mpls={"00800.mpls": 4096})
        self.tb.define_mkv("00800.mpls", tracks=[
            {"type": "video", "codec": "HEVC/H.265/MPEG-H"},
            {"type": "audio", "codec": "TrueHD Atmos", "language": "spa", "channels": 8}])

    def _sesion(self):
        from models import (Chapter, IncludedAudioTrack, RawAudioTrack, Session)
        s = Session(
            id="Peli_2024_1700000000", iso_path=str(self.iso), source_type="iso",
            mkv_name="Peli (2024).mkv", status="queued",
            included_tracks=[IncludedAudioTrack(
                position=0, raw=RawAudioTrack(codec="Dolby TrueHD/Atmos Audio",
                                              language="Spanish", bitrate_kbps=0,
                                              description="7.1 / 48 kHz"),
                language_literal="Castellano", codec_literal="TrueHD Atmos 7.1",
                label="Castellano TrueHD Atmos 7.1", flag_default=True,
                selection_reason="test")],
            chapters=[Chapter(number=1, timestamp="00:00:00.000", name="Capítulo 01")])
        self.storage.save_session(s)
        return s

    async def test_termina_y_desmonta(self):
        s = self._sesion()
        await self.tab1._run_pipeline(s.id)
        s = self.storage.load_session(s.id)
        self.assertEqual(s.status, "done", s.error_message)
        mux = [c for c in self.tb.find("mkvmerge") if c.opt("-o")]
        self.assertTrue(any(str(self.base) in a for a in mux[-1].argv),
                        "mkvmerge no ha leído del punto de montaje")
        [m], [u] = self.tb.find("mount"), self.tb.find("umount")
        self.assertEqual(u.argv, [m.argv[5]])
        self.assertEqual(self.puntos(), [])

    async def test_si_mkvmerge_falla_tambien_desmonta(self):
        self.tb.fail_when_arg("mkvmerge", "--gui-mode", rc=2, stderr="petó\n")
        s = self._sesion()
        await self.tab1._run_pipeline(s.id)
        self.assertEqual(self.storage.load_session(s.id).status, "error")
        self.assertTrue(self.tb.ran("umount"))
        self.assertEqual(self.puntos(), [])


if __name__ == "__main__":
    unittest.main()
