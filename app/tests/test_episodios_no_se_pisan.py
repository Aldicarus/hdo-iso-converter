"""Dos episodios de un disco no acaban escribiendo el mismo MKV, y reemplazar
episodios no pierde proyectos.

Dos fallos de la creación y edición de series que encontró la auditoría con
Codex del 2026-10-17, reproducidos antes de arreglarlos:

  · **Reaplicar las reglas a un episodio le ponía nombre de PELÍCULA**:
    `reapply-rules` (los toggles de modo de audio y subtítulos) y
    `recalculate-name` construían el nombre a partir del origen, que es el
    mismo para todo el disco. Dos episodios acababan con el mismo `mkv_name`
    en la raíz de output —sin la estructura de carpetas— y el segundo rip
    sobrescribía el MKV del primero. Además, ejecutar ya no deja que dos
    proyectos escriban el mismo fichero.
  · **«Reemplazar» borraba los proyectos antes de saber si la cola admitía
    el trabajo**: con el modal de serie abierto dos veces sobre el mismo disco,
    la cola descartaba el segundo por duplicado y el episodio desaparecía. Hoy
    los borra el runner, cada uno cuando el que lo sustituye ya está guardado.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_episodios_no_se_pisan -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
from test_serie_auto_ejecuta import _bdinfo  # noqa: E402
import workload  # noqa: E402


class EpisodiosCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        self.iso = self.isos_dir / "Juego de Tronos S04 DISC1.iso"
        self.iso.write_bytes(b"\x00" * 4096)

    def _episodio(self, n, **kw):
        from models import Session
        from phases.phase_b import build_series_mkv_name
        from storage import save_session
        datos = dict(
            id=f"Juego_S04E0{n}_170000000{n}", iso_path=str(self.iso), source_type="iso",
            status="pending", bdinfo_result=_bdinfo(), media_type="series",
            series_name="Juego de Tronos", series_year=2011, season_number=4,
            episode_number=n, episode_title=f"Episodio {n}", mkv_name_manual=False,
            mkv_name=build_series_mkv_name("Juego de Tronos", 2011, 4, n, f"Episodio {n}"))
        datos.update(kw)
        s = Session(**datos)
        save_session(s)
        return s

    def _nombre(self, sid):
        return self.client.get(f"/api/sessions/{sid}").json()["mkv_name"]


class TestReaplicarConservaElNombreDeEpisodio(EpisodiosCase):

    def test_reaplicar_las_reglas(self):
        a, b = self._episodio(1), self._episodio(2)
        for s in (a, b):
            r = self.client.post(f"/api/sessions/{s.id}/reapply-rules",
                                 json={"audio_mode": "keep_all"})
            self.assertEqual(r.status_code, 200, r.text)
        na, nb = self._nombre(a.id), self._nombre(b.id)
        self.assertNotEqual(na, nb, "dos episodios con el mismo MKV")
        self.assertIn("Season 04/", na)
        self.assertIn("S04E01", na)
        self.assertIn("S04E02", nb)

    def test_recalcular_el_nombre(self):
        a = self._episodio(1, mkv_name="cualquiera.mkv")
        r = self.client.post(f"/api/sessions/{a.id}/recalculate-name")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("S04E01", r.json()["mkv_name"])
        self.assertEqual(self._nombre(a.id), r.json()["mkv_name"], "no se guardó")

    def test_un_nombre_editado_a_mano_no_se_toca(self):
        a = self._episodio(1, mkv_name="Mi nombre.mkv", mkv_name_manual=True)
        self.client.post(f"/api/sessions/{a.id}/reapply-rules", json={"audio_mode": "keep_all"})
        self.assertEqual(self._nombre(a.id), "Mi nombre.mkv")


class TestDosProyectosNoEscribenElMismoMkv(EpisodiosCase):

    def test_ejecutar_con_el_destino_de_otro_da_409(self):
        a = self._episodio(1)
        b = self._episodio(2, mkv_name=a.mkv_name)
        r = self.client.post(f"/api/sessions/{b.id}/execute")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.encolados, [], "se ha encolado igualmente")

    def test_con_destinos_distintos_se_encola(self):
        self._episodio(1)
        b = self._episodio(2)
        r = self.client.post(f"/api/sessions/{b.id}/execute")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.encolados, [b.id])


class TestReemplazarNoPierdeProyectos(EpisodiosCase):

    def setUp(self):
        super().setUp()
        self.carpeta = self.isos_dir / "Serie (2024)"
        for sub in ("PLAYLIST", "STREAM"):
            (self.carpeta / "BDMV" / sub).mkdir(parents=True, exist_ok=True)
        for i in (1, 2):
            (self.carpeta / "BDMV" / "PLAYLIST" / f"0080{i}.mpls").write_bytes(b"\0" * 64)
        (self.carpeta / "BDMV" / "STREAM" / "00800.m2ts").write_bytes(b"\1" * 8192)
        from storage import compute_iso_fingerprint
        self.huella = compute_iso_fingerprint(str(self.carpeta / "BDMV" / "STREAM" / "00800.m2ts"))
        self.viejo = self._episodio(1, iso_path=str(self.carpeta), source_type="bdmv_folder",
                                    iso_fingerprint=self.huella, season_number=1,
                                    mpls_path="00801.mpls")

    def _post(self, ep, modo):
        return self.client.post("/api/create-series-sessions", json={
            "source_type": "bdmv_folder", "source_path": "Serie (2024)",
            "series_name": "Serie", "season_number": 1, "mode": modo,
            "episodes": [{"mpls_path": f"0080{ep}.mpls", "episode_number": ep,
                          "episode_title": f"Ep {ep}"}]})

    def test_un_reemplazo_que_la_cola_descarta_no_borra_nada(self):
        from storage import load_session
        self.assertEqual(self._post(2, "add_only").status_code, 200)
        r = self._post(1, "replace")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("duplicado"), r.json())
        self.assertIsNotNone(load_session(self.viejo.id), "el episodio se ha perdido")

    def test_una_sesion_antigua_con_media_type_movie_cuenta_como_existente(self):
        """El modal la reconoce por temporada y episodio; el servidor exigía
        `media_type == "series"`, que en las muy antiguas vale «movie» por
        defecto, y «Saltar» creaba otro proyecto encima."""
        from storage import save_session
        self.viejo.media_type = "movie"
        save_session(self.viejo)
        r = self._post(1, "add_only")
        self.assertEqual(r.status_code, 409, r.text)

    def test_el_mismo_mpls_con_otra_numeracion_es_el_mismo_episodio(self):
        """El modal identifica por el fichero del disco; el servidor buscaba
        solo la pareja nueva (temporada 1, episodio 3), no encontraba nada y
        «Reemplazar» no borraba el proyecto viejo."""
        r = self.client.post("/api/create-series-sessions", json={
            "source_type": "bdmv_folder", "source_path": "Serie (2024)",
            "series_name": "Serie", "season_number": 1, "mode": "replace",
            "episodes": [{"mpls_path": "00801.mpls", "episode_number": 3,
                          "episode_title": "Ep 3"}]})
        self.assertEqual(r.status_code, 200, r.text)
        [(_, _, datos, _)] = self.trabajos_encolados
        self.assertEqual(datos["reemplazos"], {"3": self.viejo.id})

    def _runner(self, analisis):
        from routers import tab1
        cuerpo = tab1.CreateSeriesSessionsRequest(
            source_type="bdmv_folder", source_path="Serie (2024)", series_name="Serie",
            season_number=1, episodes=[{"mpls_path": "00801.mpls", "episode_number": 1,
                                        "episode_title": "Ep 1"}])
        import asyncio
        with patch("phases.phase_a.run_full_analysis_for_mpls", side_effect=analisis):
            asyncio.run(tab1._ejecutar_creacion_de_serie(
                cuerpo, "bdmv_folder", "Serie (2024)", str(self.carpeta), cuerpo.episodes,
                [], [self.viejo.id], self.huella, reemplazos={"1": self.viejo.id}))
        return tab1._series_create_progress.get("resultado") or {}

    def test_el_runner_borra_el_viejo_cuando_el_nuevo_existe(self):
        from storage import load_session
        async def _ok(*a, **k):
            return _bdinfo(), []
        r = self._runner(_ok)
        self.assertEqual(len(r.get("created") or []), 1, r)
        self.assertIsNone(load_session(self.viejo.id), "el reemplazado sigue ahí")

    def test_si_el_analisis_falla_el_viejo_se_queda(self):
        from storage import load_session
        async def _falla(*a, **k):
            raise RuntimeError("mkvmerge no pudo leer el disco")
        r = self._runner(_falla)
        self.assertEqual(r.get("created"), [], r)
        self.assertIsNotNone(load_session(self.viejo.id), "se ha perdido el proyecto")


if __name__ == "__main__":
    unittest.main()
