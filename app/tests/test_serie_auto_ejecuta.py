"""«Ejecutar al crear»: los episodios de un disco van solos a la cola.

Un disco de una temporada crea diez proyectos, y hasta ahora había que
abrirlos y lanzarlos de uno en uno: veinte clics para no cambiar nada, porque
los episodios de un mismo disco traen las mismas pistas y las mismas reglas.
La casilla del modal de serie los encola en cuanto se crean.

**Solo para series.** En una película el proyecto es uno y revisarlo antes de
gastar cuarenta minutos es justo lo que hay que hacer; lo que cansa es repetir
esa revisión diez veces sobre el mismo disco.

Lo que este fichero fija, ejecutando el runner de verdad —el mismo que
despacha la cola— con el análisis de disco sustituido:

- con la casilla marcada, cada sesión queda `queued` y se encola su rip;
- sin ella (el default) quedan `pending` y no se encola nada;
- el orden de la cola es el de los episodios, no el de creación al azar;
- el recuento viaja al modal en `resultado["encolados"]`;
- y el flag llega al runner, que es lo que hace que sobreviva a la espera:
  la cola puede despachar cuarenta minutos después.

El default es **desactivado**: encolar diez rips son horas de NAS, así que es
una decisión que se toma, no algo que pasa por no mirar.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_serie_auto_ejecuta -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
import queue_manager as qm  # noqa: E402
import workload  # noqa: E402


def _bdinfo():
    """Lo mínimo que `apply_rules` necesita para producir una selección."""
    from models import (BDInfoResult, RawAudioTrack, RawSubtitleTrack,
                        VideoTrack)
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
        duration_seconds=2_700.0,
        has_fel=False,
        vo_language="English",
        main_mpls="00801.mpls",
    )


class SerieCase(ApiTestCase):
    """Corre el runner real sobre una carpeta BDMV, sin analizar nada.

    `Source.open` sobre un `bdmv_folder` es un no-op —solo verifica la
    estructura—, así que el runner se ejecuta entero: el bucle de episodios,
    las reglas, el nombre Plex, el guardado y el encolado. Lo único
    sustituido es el análisis del disco, que lanzaría mkvmerge.
    """

    EPISODIOS = 3

    def setUp(self):
        super().setUp()
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        self.carpeta = self.isos_dir / "Serie (2024)"
        for sub in ("PLAYLIST", "STREAM"):
            (self.carpeta / "BDMV" / sub).mkdir(parents=True, exist_ok=True)
        for i in range(self.EPISODIOS):
            (self.carpeta / "BDMV" / "PLAYLIST" / f"0080{i}.mpls").write_bytes(b"\0" * 64)
            (self.carpeta / "BDMV" / "STREAM" / f"0080{i}.m2ts").write_bytes(b"\0" * 4096)

    async def _crear(self, auto: bool):
        """Ejecuta el runner y devuelve el resultado que ve el modal."""
        from routers import tab1

        cuerpo = tab1.CreateSeriesSessionsRequest(
            source_type="bdmv_folder", source_path="Serie (2024)",
            series_name="Serie", series_year=2024, season_number=1,
            auto_execute=auto,
            episodes=[{"mpls_path": f"0080{i}.mpls", "episode_number": i + 1,
                       "episode_title": f"Ep {i + 1}"}
                      for i in range(self.EPISODIOS)])
        with patch("phases.phase_a.run_full_analysis_for_mpls",
                   return_value=(_bdinfo(), [])):
            await tab1._ejecutar_creacion_de_serie(
                cuerpo, "bdmv_folder", "Serie (2024)", str(self.carpeta),
                cuerpo.episodes, [], [], "huella")
        return tab1._series_create_progress["resultado"]

    def rips_encolados(self):
        return [t for t in self.trabajos_encolados if t[0] == qm.TIPO_RIP]

    def estados(self):
        from storage import list_sessions
        return sorted((s.episode_number, s.status) for s in list_sessions())


class TestConLaCasillaMarcada(SerieCase, unittest.IsolatedAsyncioTestCase):

    async def test_los_episodios_se_crean_y_se_encolan(self):
        r = await self._crear(auto=True)
        self.assertEqual(len(r["created"]), self.EPISODIOS, r)
        self.assertEqual(len(self.rips_encolados()), self.EPISODIOS)

    async def test_y_quedan_esperando_turno_en_disco(self):
        # `pending` significaría que hay que abrirlos y lanzarlos, que es
        # justo lo que la casilla evita.
        await self._crear(auto=True)
        self.assertEqual(self.estados(),
                         [(i + 1, "queued") for i in range(self.EPISODIOS)])

    async def test_el_orden_de_la_cola_es_el_de_los_episodios(self):
        # Se encola al crear cada uno y no todos al final, que es lo que lo
        # garantiza: ver E03 antes que E01 no tiene ninguna explicación.
        await self._crear(auto=True)
        claves = [t[1] for t in self.rips_encolados()]
        self.assertEqual(claves, sorted(claves, key=lambda c: c.split("E")[-1]))

    async def test_el_modal_puede_decir_cuantos_quedaron_en_cola(self):
        r = await self._crear(auto=True)
        self.assertEqual(r["encolados"], self.EPISODIOS)


class TestSinLaCasilla(SerieCase, unittest.IsolatedAsyncioTestCase):
    """El default: crear no ejecuta."""

    async def test_se_crean_igual(self):
        r = await self._crear(auto=False)
        self.assertEqual(len(r["created"]), self.EPISODIOS)

    async def test_pero_no_se_encola_ni_uno(self):
        await self._crear(auto=False)
        self.assertEqual(self.rips_encolados(), [])

    async def test_y_esperan_a_que_el_usuario_los_lance(self):
        await self._crear(auto=False)
        self.assertEqual(self.estados(),
                         [(i + 1, "pending") for i in range(self.EPISODIOS)])

    async def test_el_recuento_es_cero_no_falta(self):
        # El modal lo lee sin comprobar si está: `undefined` se pintaría.
        r = await self._crear(auto=False)
        self.assertEqual(r["encolados"], 0)


class TestElOrdenDeLasDosEscriturasDelFinal(SerieCase,
                                             unittest.IsolatedAsyncioTestCase):
    """El resultado se escribe ANTES de bajar la bandera de «corriendo».

    El modal lee «ya no corre y no hay resultado» como un fallo, así que con
    el orden inverso un poll que cayera entre las dos líneas anunciaba que no
    se pudieron crear los proyectos **con los proyectos ya creados**. Era una
    ventana de microsegundos contra vueltas de 700 ms, pero el desenlace era
    decir lo contrario de lo que pasó.
    """

    async def test_primero_el_resultado_y_despues_la_bandera(self):
        from routers import tab1

        orden = []

        class Espia(dict):
            def __setitem__(self, k, v):
                if k == "resultado" or (k == "running" and v is False):
                    orden.append(k)
                super().__setitem__(k, v)

        with patch.object(tab1, "_series_create_progress", Espia()):
            r = await self._crear(auto=True)

        # Que el camino recorrido sea el bueno: sin esto el test pasaría con
        # un runner que falla antes de llegar al final.
        self.assertEqual(len(r["created"]), self.EPISODIOS)
        self.assertIn("resultado", orden)
        # La PRIMERA bajada de bandera, no la última: con una de más por
        # delante el modal ya ha visto «no corre y no hay resultado», y
        # comprobar sólo la última pasaría en verde.
        self.assertLess(orden.index("resultado"), orden.index("running"),
                        f"la bandera bajó antes del resultado: {orden}")


class TestElFlagSobreviveALaEspera(ApiTestCase):
    """La cola puede despachar cuarenta minutos después de encolar.

    El `body` viaja serializado porque un modelo de Pydantic no se persiste,
    así que un campo que no entre ahí se pierde en el camino y el runner
    crearía los episodios sin ejecutarlos — sin un error.
    """

    def setUp(self):
        super().setUp()
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        carpeta = self.isos_dir / "Serie (2024)"
        (carpeta / "BDMV" / "PLAYLIST").mkdir(parents=True, exist_ok=True)
        (carpeta / "BDMV" / "PLAYLIST" / "00801.mpls").write_bytes(b"\0" * 64)
        (carpeta / "BDMV" / "STREAM").mkdir(parents=True, exist_ok=True)
        (carpeta / "BDMV" / "STREAM" / "00801.m2ts").write_bytes(b"\0" * 4096)

    def _post(self, **extra):
        return self.client.post("/api/create-series-sessions", json={
            "source_type": "bdmv_folder", "source_path": "Serie (2024)",
            "series_name": "Serie", "season_number": 1,
            "episodes": [{"mpls_path": "00801.mpls", "episode_number": 1,
                          "episode_title": "Ep 1"}],
            **extra})

    def _body_encolado(self):
        return [t for t in self.trabajos_encolados
                if t[0] == qm.TIPO_SERIE][0][2]["body"]

    def test_viaja_hasta_el_runner(self):
        self.assertEqual(self._post(auto_execute=True).status_code, 200)
        self.assertTrue(self._body_encolado()["auto_execute"])

    def test_y_su_ausencia_significa_que_no(self):
        self.assertEqual(self._post().status_code, 200)
        self.assertFalse(self._body_encolado()["auto_execute"])


if __name__ == "__main__":
    unittest.main()
