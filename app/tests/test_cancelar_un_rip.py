"""Cancelar un rip de Tab 1, de punta a punta.

El endpoint de cancelar tenía un 7 % de cobertura (auditoría de la suite del
2026-10-17): lo que ya se probaba era el `except` del orquestador con la marca
de cancelación puesta a mano, no que el endpoint mate el proceso en curso ni
que, con el proceso muerto a mitad del mux, el rip acabe como CANCELADO —y no
como un error— y sin dejar nada a medias en /mnt/output.

Para eso hace falta un proceso vivo: el `mkvmerge` falso tarda
(`retrasar_cuando_arg`) y la cancelación llega mientras corre.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cancelar_un_rip -v
"""
import asyncio
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import FakeToolbox  # noqa: E402
from models import (  # noqa: E402
    Chapter, IncludedAudioTrack, RawAudioTrack, Session,
)

TARDA = 20.0


class CancelarCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import paths
        import storage
        from phases import phase_d, phase_e
        from routers import tab1
        self.tab1, self.storage = tab1, storage
        tab1._session_save_locks.clear()
        tab1._session_save_throttle.clear()
        self.tmp = Path(tempfile.mkdtemp(prefix="cancelar_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.trabajo, self.salida = (self.tmp / n for n in ("config", "tmp", "output"))
        for d in (self.config, self.trabajo, self.salida):
            d.mkdir()
        self.bdmv = self.tmp / "Peli (2024)"
        (self.bdmv / "BDMV" / "PLAYLIST").mkdir(parents=True)
        (self.bdmv / "BDMV" / "STREAM").mkdir(parents=True)
        self.mpls = self.bdmv / "BDMV" / "PLAYLIST" / "00800.mpls"
        self.mpls.write_bytes(b"\x00" * 4096)
        self.tb = FakeToolbox(self.tmp).install()
        self.addCleanup(self.tb.uninstall)
        self.tb.define_mkv(self.mpls.name, tracks=[
            {"type": "video", "codec": "HEVC/H.265/MPEG-H"},
            {"type": "audio", "codec": "TrueHD Atmos", "language": "spa", "channels": 8},
            {"type": "audio", "codec": "DTS-HD Master Audio", "language": "eng", "channels": 6}])
        parches = [(storage, "CONFIG_DIR", self.config), (paths, "CONFIG_DIR", self.config),
                   (paths, "TMP_DIR", str(self.trabajo)),
                   (phase_e, "OUTPUT_DIR", str(self.salida)), (phase_d, "MIN_MPLS_SIZE", 200)]
        originales = [(m, a, getattr(m, a)) for m, a, _ in parches]
        for m, a, v in parches:
            setattr(m, a, v)
        self.addCleanup(lambda: [setattr(m, a, v) for m, a, v in originales])
        getattr(storage, "_sessions_summary_by_file", {}).clear()

    def _sesion(self, *, ruta_directa=True):
        def _a(lang, codec, label, ch):
            return IncludedAudioTrack(
                position=0, raw=RawAudioTrack(codec=codec, language=lang, bitrate_kbps=0,
                                              description=f"{ch}.0 / 48 kHz"),
                language_literal=label.split()[0], codec_literal=label, label=label,
                flag_default=lang == "Spanish", selection_reason="test")
        incluidas = [_a("Spanish", "Dolby TrueHD/Atmos Audio", "Castellano TrueHD Atmos 7.1", 7)]
        if not ruta_directa:
            incluidas.append(_a("English", "DTS-HD Master Audio", "Inglés DTS-HD MA 5.1", 5))
        s = Session(id="Peli_2024_1700000000", iso_path=str(self.bdmv),
                    source_type="bdmv_folder", source_path=str(self.bdmv),
                    mkv_name="Peli (2024).mkv", status="queued", included_tracks=incluidas,
                    chapters=[Chapter(number=1, timestamp="00:00:00.000", name="Capítulo 01")])
        self.storage.save_session(s)
        return s

    async def _cancelar_a_mitad(self, s):
        # `parcial`: el mux deja su salida a medias ANTES de que llegue la
        # cancelación, como el de verdad. Sin eso, «no queda nada» no probaba
        # ninguna limpieza (revisión de Codex del 2026-10-17).
        self.tb.retrasar_cuando_arg("mkvmerge", "--gui-mode", TARDA, parcial=True)
        t0 = time.monotonic()
        rip = asyncio.create_task(self.tab1._run_pipeline(s.id))
        # Se espera a que el MUX esté corriendo, no a que haya un proceso
        # registrado: el `--identify` previo también se registra, y cancelar
        # ahí pasaba en verde sin haber tocado el mux (la primera versión de
        # este test lo hacía).
        for _ in range(300):
            if self._muxes() and s.id in self.tab1._active_processes:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(self._muxes(), "el mux no ha arrancado")
        self.assertFalse(rip.done(), "el rip ha terminado antes de cancelar")
        for _ in range(100):
            if self._destino_del_mux().exists():
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self._destino_del_mux().exists(),
                        "no hay parcial que limpiar: el test no probaría nada")
        r = await self.tab1.cancel_running_session(s.id)
        self.assertEqual(r["ok"], True)
        await asyncio.wait_for(rip, timeout=TARDA - 5)
        self.assertLess(time.monotonic() - t0, TARDA - 5, "el proceso no ha muerto")
        self.assertFalse(self._destino_del_mux().exists(), "el parcial del mux sigue ahí")
        return self.storage.load_session(s.id)

    def _muxes(self):
        return [c for c in self.tb.find("mkvmerge") if "--gui-mode" in c.argv]

    def _destino_del_mux(self):
        [mux] = self._muxes()
        return Path(mux.opt("-o"))


class TestCancelarAMitadDelMux(CancelarCase):

    async def test_la_ruta_directa_acaba_cancelada_y_limpia(self):
        s = await self._cancelar_a_mitad(self._sesion(ruta_directa=True))
        self.assertEqual(self._destino_del_mux().parent, self.salida, "no es la ruta directa")
        self.assertEqual(s.status, "pending")
        self.assertIsNone(s.error_message, "una cancelación no es un error")
        self.assertIsNotNone(s.last_cancelled_at)
        self.assertEqual(list(self.salida.rglob("*")), [], "queda algo en /mnt/output")

    async def test_la_ruta_con_intermedio_tampoco_deja_el_intermedio(self):
        s = await self._cancelar_a_mitad(self._sesion(ruta_directa=False))
        self.assertIn("_intermediate", self._destino_del_mux().name, "no es la ruta con intermedio")
        self.assertEqual(s.status, "pending")
        self.assertEqual(list(self.trabajo.glob("*_intermediate.mkv")), [])
        self.assertEqual(list(self.salida.rglob("*")), [])


class TestCancelarEntreDosPasos(CancelarCase):
    """Una cancelación que llega sin proceso registrado.

    El endpoint mata lo que esté en `_active_processes`, y el `--identify`
    no se registra: cancelar ahí ponía la marca, nadie la miraba antes del
    mux, y el rip podía acabar en `done` (auditoría con Codex del
    2026-10-17)."""

    IDENT = 2.0

    def _identifies(self):
        return [c for c in self.tb.find("mkvmerge") if "--identify" in c.argv]

    async def _cancelar_en_el_identify(self, s, n):
        self.tb.retrasar_cuando_arg("mkvmerge", "--identify", self.IDENT)
        self.tb.retrasar_cuando_arg("mkvmerge", "--gui-mode", TARDA, parcial=True)
        t0 = time.monotonic()
        rip = asyncio.create_task(self.tab1._run_pipeline(s.id))
        for _ in range(300):
            if len(self._identifies()) >= n:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(len(self._identifies()), n, "no se llegó al identify esperado")
        await self.tab1.cancel_running_session(s.id)
        await asyncio.wait_for(rip, timeout=TARDA - 5)
        self.assertLess(time.monotonic() - t0, TARDA - 5, "se ha esperado al mux entero")
        return self.storage.load_session(s.id)

    async def test_durante_el_identify_de_la_ruta_no_arranca_ningun_mux(self):
        s = await self._cancelar_en_el_identify(self._sesion(ruta_directa=True), 1)
        self.assertEqual(self._muxes(), [], "ha arrancado el mux con el rip cancelado")
        # Ni siquiera la Fase E: el mux lo mataría el registro, pero el rip
        # tiene que pararse en el primer punto de control, no en el último.
        self.assertEqual(len(self._identifies()), 1, "ha seguido hasta la Fase E")
        self.assertEqual(s.status, "pending")
        self.assertIsNotNone(s.last_cancelled_at)

    async def test_durante_el_identify_de_la_fase_e_el_mux_muere_al_nacer(self):
        """El segundo `--identify` es el de dentro de la Fase E: ya no hay
        punto de control antes del mux, así que lo mata el registro."""
        s = await self._cancelar_en_el_identify(self._sesion(ruta_directa=True), 2)
        self.assertEqual(s.status, "pending", "el rip ha acabado como si nada")
        self.assertIsNotNone(s.last_cancelled_at)
        self.assertEqual(list(self.salida.rglob("*")), [], "queda algo en /mnt/output")


class TestElEndpoint(CancelarCase):

    async def test_una_sesion_que_no_corre_da_400(self):
        from fastapi import HTTPException
        s = self._sesion()
        with self.assertRaises(HTTPException) as ctx:
            await self.tab1.cancel_running_session(s.id)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_una_que_no_existe_da_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await self.tab1.cancel_running_session("no_existe")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
