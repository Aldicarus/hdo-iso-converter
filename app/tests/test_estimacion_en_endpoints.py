"""El chip «💾 Ocupará ~N GB» tiene que llegar por TODAS las vías de apertura.

`estimated_size_bytes` es un campo **calculado al servir**, no persistido: sale
de restar al m2ts los audios que se descartan, y eso cambia en cuanto el
usuario toca la selección. Vivía SOLO en `GET /api/sessions/{id}`, pero el
panel de Tab 1 se pinta con lo que devuelvan cinco endpoints — y el frontend
esconde el chip cuando el campo falta, exactamente igual que cuando vale
`null` porque el dato no es fiable. Desde fuera las dos cosas se ven idénticas:
un hueco.

El síntoma real (2026-09-04, «El día de la revelación»): analizar un ISO y no
ver el tamaño. La estimación del backend era correcta —80,1 GB, comprobada
contra la sesión del NAS— pero `POST /api/analyze` devolvía el `model_dump()`
pelado. Reabrir el proyecto desde el sidebar sí lo enseñaba, que es justo el
tipo de diferencia que hace pensar en un fallo del cálculo.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_estimacion_en_endpoints -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402


# ── Un disco realista en miniatura ────────────────────────────────────────
#
# Los números salen de Obsession 2025 (el mismo caso que fija
# `test_estimacion_tamano`): m2ts de 65,39 GB y dos TrueHD Atmos.

FILE_SIZE = 65_394_561_024
DURACION = 6_500.0
SS_EN = 372_623_944       # StreamSize del TrueHD inglés
SS_ES = 532_310_000       # StreamSize del TrueHD castellano


def _bdinfo():
    from models import (
        BDInfoResult, MediaInfoResult, RawAudioTrack, RawSubtitleTrack,
        VideoTrack,
    )

    raw_json = {"media": {"track": [
        {"@type": "General", "FileSize": str(FILE_SIZE)},
        {"@type": "Video", "Format": "HEVC"},
        {"@type": "Audio", "Format": "MLP FBA", "StreamSize": str(SS_EN)},
        {"@type": "Audio", "Format": "MLP FBA", "StreamSize": str(SS_ES)},
    ]}}
    return BDInfoResult(
        video_tracks=[VideoTrack(codec="HEVC Video", bitrate_kbps=60_000,
                                 description="2160p / 23,976 fps")],
        audio_tracks=[
            RawAudioTrack(codec="Dolby TrueHD/Atmos Audio", language="English",
                          bitrate_kbps=4_500, description="7.1 / 48 kHz"),
            RawAudioTrack(codec="Dolby TrueHD/Atmos Audio", language="Spanish",
                          bitrate_kbps=4_500, description="7.1 / 48 kHz"),
        ],
        subtitle_tracks=[
            RawSubtitleTrack(language="Spanish", bitrate_kbps=20.0,
                             description="1920x1080", packet_count=4_500),
        ],
        duration_seconds=DURACION,
        has_fel=False,
        vo_language="English",
        main_mpls="00800.mpls",
        mediainfo_result=MediaInfoResult(raw_json=raw_json),
    )


def _incluidas():
    """Las dos pistas de audio, ya emparejables con las del bdinfo."""
    from models import IncludedAudioTrack

    def _uno(pos, lang_literal, raw):
        return IncludedAudioTrack(
            position=pos, raw=raw, language_literal=lang_literal,
            codec_literal="TrueHD Atmos 7.1",
            label=f"{lang_literal} TrueHD Atmos 7.1",
            flag_default=(pos == 0), selection_reason="test",
        )

    bd = _bdinfo()
    return [_uno(0, "Castellano", bd.audio_tracks[1]),
            _uno(1, "Inglés", bd.audio_tracks[0])]


class _Base(ApiTestCase):

    def crear(self, sid="Peli_2024_1700000000", **extra):
        return self.crear_sesion_tab1(
            sid, bdinfo_result=_bdinfo(), included_tracks=_incluidas(), **extra)

    def assertEstimado(self, payload, msg=""):
        """El campo está Y trae un número creíble (no `null`, no cero)."""
        self.assertIn("estimated_size_bytes", payload,
                      f"falta el campo calculado {msg}")
        v = payload["estimated_size_bytes"]
        self.assertIsNotNone(v, f"el campo llegó a null {msg}")
        self.assertGreater(v, 40_000_000_000, msg)
        self.assertLess(v, FILE_SIZE, msg)
        return v


class TestLasCincoViasDeApertura(_Base):
    """Cada una de estas respuestas acaba en `renderProjectPanel`."""

    def test_el_detalle_lo_trae(self):
        sid = self.crear()
        self.assertEstimado(self.client.get(f"/api/sessions/{sid}").json())

    def test_check_duplicate_lo_trae(self):
        """El botón «Abrir» del diálogo de duplicado pinta ESTA sesión."""
        import storage

        iso = self.isos_dir / "Peli (2024).iso"
        iso.write_bytes(b"\0" * 4096)
        fp = storage.compute_iso_fingerprint(str(iso))
        sid = self.crear(iso_fingerprint=fp, iso_path=str(iso))

        r = self.client.post("/api/check-duplicate",
                             json={"source_type": "iso",
                                   "source_path": "Peli (2024).iso"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["duplicate"])
        self.assertEqual([s["id"] for s in d["sessions"]], [sid])
        self.assertEstimado(d["sessions"][0], "(en sessions[])")
        self.assertEstimado(d["session"], "(en el campo legacy)")

    def test_reapply_rules_lo_trae(self):
        """Los toggles de modo audio/subs re-pintan el panel entero."""
        sid = self.crear()
        r = self.client.post(f"/api/sessions/{sid}/reapply-rules",
                             json={"audio_mode": "keep_all"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEstimado(r.json())

    def test_analyze_lo_trae(self):
        """El caso del usuario: analizar un origen y abrir el proyecto."""
        payload = self._analizar()
        self.assertEstimado(payload)

    def test_analyze_deja_la_misma_cifra_que_el_detalle(self):
        """Las dos vías tienen que contar lo mismo — si no, el chip cambiaría
        de valor al cerrar y reabrir el proyecto."""
        payload = self._analizar()
        detalle = self.client.get(f"/api/sessions/{payload['id']}").json()
        self.assertEqual(payload["estimated_size_bytes"],
                         detalle["estimated_size_bytes"])

    def test_create_series_sessions_lo_trae(self):
        """Al crear N episodios el frontend abre las pestañas con ESTOS
        objetos, sin volver a pedir el detalle de cada uno."""
        from phases import phase_a

        carpeta = self.isos_dir / "Serie (2024)"
        (carpeta / "BDMV" / "PLAYLIST").mkdir(parents=True, exist_ok=True)
        (carpeta / "BDMV" / "PLAYLIST" / "00801.mpls").write_bytes(b"\0" * 64)
        (carpeta / "BDMV" / "STREAM").mkdir(parents=True, exist_ok=True)
        (carpeta / "BDMV" / "STREAM" / "00801.m2ts").write_bytes(b"\0" * 4096)

        async def _fase_a_falsa(bdmv_root, mpls_path, **kw):
            return _bdinfo(), []

        original = phase_a.run_full_analysis_for_mpls
        phase_a.run_full_analysis_for_mpls = _fase_a_falsa
        self.addCleanup(
            setattr, phase_a, "run_full_analysis_for_mpls", original)

        r = self.client.post("/api/create-series-sessions", json={
            "source_type": "bdmv_folder",
            "source_path": "Serie (2024)",
            "series_name": "Serie",
            "season_number": 1,
            "episodes": [{"mpls_path": "00801.mpls", "episode_number": 1,
                          "episode_title": "Piloto"}],
        })
        self.assertEqual(r.status_code, 200, r.text)
        creados = r.json()["created"]
        self.assertEqual(len(creados), 1, r.json().get("failed"))
        self.assertEstimado(creados[0], "(en created[])")

    # ── el análisis, con Fase A sustituida ────────────────────────────
    def _analizar(self) -> dict:
        """`POST /api/analyze` sobre una carpeta BDMV de mentira.

        `bdmv_folder` es un no-op en `Source` (no monta nada), así que el
        endpoint corre entero: reglas de Fase B reales sobre el bdinfo
        canónico, persistencia y respuesta. Solo se sustituye Fase A, que
        es la que necesitaría un disco.
        """
        from routers import tab1 as rt

        carpeta = self.isos_dir / "Peli (2024)"
        (carpeta / "BDMV" / "PLAYLIST").mkdir(parents=True, exist_ok=True)
        (carpeta / "BDMV" / "STREAM").mkdir(parents=True, exist_ok=True)
        (carpeta / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"\0" * 4096)

        async def _fase_a_falsa(bdmv_root, **kw):
            return _bdinfo(), "00800.mpls", []

        async def _sin_tmdb(session_id):
            return None

        for mod, attr, nuevo in ((rt, "run_full_analysis", _fase_a_falsa),
                                 (rt, "_hydrate_session_tmdb", _sin_tmdb)):
            original = getattr(mod, attr)
            setattr(mod, attr, nuevo)
            self.addCleanup(setattr, mod, attr, original)

        r = self.client.post("/api/analyze",
                             json={"source_type": "bdmv_folder",
                                   "source_path": "Peli (2024)"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TestSeRecalcula(_Base):
    """No basta con que el campo viaje: tiene que reflejar la selección."""

    def test_el_put_baja_la_cifra_al_quitar_una_pista(self):
        sid = self.crear()
        antes = self.assertEstimado(
            self.client.get(f"/api/sessions/{sid}").json())

        solo_una = [t.model_dump() for t in _incluidas()[:1]]
        r = self.client.put(f"/api/sessions/{sid}",
                            json={"included_tracks": solo_una})
        self.assertEqual(r.status_code, 200, r.text)
        despues = self.assertEstimado(r.json(), "(tras el PUT)")

        # El inglés que se quitó son sus 372,6 MB exactos: es contabilidad.
        self.assertEqual(antes - despues, SS_EN)


if __name__ == "__main__":
    unittest.main()
