"""El rip extrae el título de la SESIÓN, y lo que entrega no se borra.

Cinco fallos de Tab 1 que encontró la auditoría completa con Codex del
2026-10-17, todos reproducidos antes de arreglarlos:

  · **La ruta con intermedio extraía otro título.** El orquestador elegía bien
    el MPLS de la sesión —el del episodio, o el que eligió la Fase A— pero a
    la Fase D le pasaba `source_path=None` en un ISO o una carpeta BDMV, y
    ahí `find_main_mpls` vuelve a elegir **el `.mpls` más grande**: el MKV
    salía con el contenido de otro playlist y los capítulos y metadatos del
    bueno, en verde. Estaba así desde el primer commit.
  · **Un origen que la sesión conoce y ya no está se sustituía por otro.**
    Sin el MPLS de la sesión se caía a `find_main_mpls`, y sin el M2TS del
    episodio, al primer fichero del conjunto.
  · **Una excepción después de entregar el MKV lo borraba** —y en una
    re-ejecución el anterior ya estaba sustituido, así que se perdían los
    dos—.
  · **Reaplicar las reglas a un episodio le ponía nombre de película**: dos
    episodios del mismo disco acababan con el mismo `mkv_name` y el segundo
    rip sobrescribía el MKV del primero.
  · **«Reemplazar» episodios borraba los proyectos antes de saber si la cola
    admitía el trabajo.**

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_el_rip_no_cambia_de_titulo -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from test_orquestador_tab1 import VIDEO, AUDIO_ES, AUDIO_EN, OrquestadorCase  # noqa: E402


class TestLaRutaConIntermedioExtraeElMplsDeLaSesion(OrquestadorCase):

    def setUp(self):
        super().setUp()
        # Otro playlist del disco, MÁS GRANDE que el de la sesión: es el que
        # `find_main_mpls` elegiría por su cuenta.
        self.otro = self.bdmv / "BDMV" / "PLAYLIST" / "00999.mpls"
        self.otro.write_bytes(b"\x00" * 9000)
        self.tb.define_mkv(self.otro.name, tracks=[VIDEO, AUDIO_ES, AUDIO_EN],
                           duration_s=600.0)

    def _mux_lee(self):
        [mux] = [c for c in self.muxes if "_intermediate.mkv" in (c.opt("-o") or "")]
        return [Path(a).name for a in mux.argv if a.endswith(".mpls")]

    async def test_una_pelicula_extrae_el_main_mpls_de_la_fase_a(self):
        from test_serie_auto_ejecuta import _bdinfo
        bd = _bdinfo()
        bd.main_mpls = "00800.mpls"
        s = self._sesion(bdinfo_result=bd)
        s = await self._correr(s)
        self.assertEqual(s.status, "done", s.error_message)
        self.assertEqual(self._mux_lee(), ["00800.mpls"],
                         "la Fase D extrajo otro playlist que el de la sesión")

    async def test_un_episodio_extrae_su_mpls(self):
        s = self._sesion(mpls_path="00800.mpls", media_type="series",
                         season_number=1, episode_number=1)
        s = await self._correr(s)
        self.assertEqual(s.status, "done", s.error_message)
        self.assertEqual(self._mux_lee(), ["00800.mpls"])

    async def test_si_el_mpls_de_la_sesion_ya_no_esta_no_coge_otro(self):
        s = self._sesion(mpls_path="00801.mpls", media_type="series",
                         season_number=1, episode_number=2)
        s = await self._correr(s)
        self.assertEqual(s.status, "error")
        self.assertIn("00801.mpls", s.error_message)
        self.assertFalse(self.muxes, "se ha extraído otro título")


class TestUnEpisodioM2tsNoCambiaDeFichero(OrquestadorCase):

    def setUp(self):
        super().setUp()
        self.carpeta = self.tmp / "Serie M2TS"
        self.carpeta.mkdir()
        self.a = self.carpeta / "e01.m2ts"
        self.b = self.carpeta / "e02.m2ts"
        for f in (self.a, self.b):
            f.write_bytes(b"\x00" * (8 * 1024 * 1024))
            self.tb.define_mkv(f.name, tracks=[VIDEO, AUDIO_ES, AUDIO_EN], duration_s=2700.0)

    def _episodio_b(self):
        return self._sesion(source_type="m2ts", iso_path=str(self.a), source_path=str(self.a),
                            mpls_path=str(self.b), media_type="series",
                            season_number=1, episode_number=2)

    async def test_extrae_su_fichero(self):
        s = await self._correr(self._episodio_b())
        self.assertEqual(s.status, "done", s.error_message)
        self.assertTrue(all("e02.m2ts" in " ".join(c.argv) for c in self.muxes), self.muxes)

    async def test_si_falta_su_fichero_no_usa_el_primero_del_conjunto(self):
        s = self._episodio_b()
        self.b.unlink()
        s = await self._correr(s)
        self.assertEqual(s.status, "error")
        self.assertFalse(self.muxes, "se ha extraído el episodio de otro fichero")

    def test_la_disponibilidad_mira_su_fichero(self):
        s = self._episodio_b()
        self.a.unlink()                # el primero del conjunto ya no está
        ok, _, ruta = self.main._check_source_available(s)
        self.assertTrue(ok, ruta)
        self.b.unlink()
        ok, _, ruta = self.main._check_source_available(s)
        self.assertFalse(ok)
        self.assertEqual(Path(ruta).name, "e02.m2ts")


class TestElMkvEntregadoNoSeBorra(OrquestadorCase):

    async def test_un_fallo_al_validar_no_borra_el_mkv(self):
        async def _revienta(*a, **k):
            raise OSError(11, "Resource temporarily unavailable")
        with patch.object(self.main, "_validate_final_mkv", _revienta):
            s = await self._correr(self._sesion())
        final = self.salida / "Peli (2024) [DV FEL].mkv"
        self.assertTrue(final.exists(), "se ha borrado el MKV ya entregado")
        self.assertEqual(s.output_mkv_path, str(final))
        # Una validación que no se pudo hacer no es un rip fallido: el MKV
        # está escrito y se reproduce. Queda `done`, con el aviso.
        self.assertEqual(s.status, "done")
        self.assertTrue(s.error_message, "el fallo de la validación se dice")


if __name__ == "__main__":
    unittest.main()
