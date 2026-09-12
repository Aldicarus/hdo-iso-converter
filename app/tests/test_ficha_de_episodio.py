"""Un MKV con nomenclatura de serie se busca en el índice de SERIES.

Caso real (2026-09-12). Al abrir en Tab 2 el fichero

    Juego de tronos (2011) - S03E01 - Valar Dohaeris [DV FEL].mkv

la ficha que salía era **«Juego de Tronos: Especial Reino Español (2015),
Documental, 43 min»**. El título se parseaba perfectamente —«Juego de tronos»,
2011— y aun así el resultado era basura: la ficha se pedía al índice de
PELÍCULAS, donde lo más parecido a una serie famosa es un documental sobre
ella. No fallaba el matcher, fallaba el índice.

`S03E01` es una señal **certera**, no una heurística: ninguna película la
lleva. Y el formato lo escribe la propia app (`build_series_mkv_name`), así que
el caso que no funcionaba era justo el más frecuente.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_harness import ApiTestCase  # noqa: E402
from services.cmv40_recommend import parse_series_filename  # noqa: E402

GOT = "Juego de tronos (2011) - S03E01 - Valar Dohaeris [DV FEL].mkv"


class TestElNombreDiceSiEsUnEpisodio(unittest.TestCase):

    def test_el_formato_que_escribe_la_app(self):
        self.assertEqual(parse_series_filename(GOT),
                         ("Juego de tronos", 2011, 3, 1, "Valar Dohaeris"))

    def test_un_nombre_de_release(self):
        serie, anio, t, e, titulo = parse_series_filename(
            "Game.of.Thrones.S03E01.2160p.UHD.BluRay.x265.mkv")
        self.assertEqual((serie, t, e), ("Game of Thrones", 3, 1))
        self.assertEqual(titulo, "", "«2160p UHD BluRay» no es el título de nada")

    def test_la_variante_3x01(self):
        self.assertEqual(parse_series_filename("Serie - 3x01 - Piloto.mkv"),
                         ("Serie", None, 3, 1, "Piloto"))

    def test_minusculas_y_un_digito(self):
        r = parse_series_filename("Serie (2011) - s3e7 - Algo.mkv")
        self.assertEqual((r[2], r[3]), (3, 7))

    def test_una_pelicula_no_lo_es(self):
        for n in ("Blade Runner 2049 (2017).mkv",
                  "Zootropolis 2 (2025) [DV FEL] [Audio DCP].mkv",
                  "The.Dark.Knight.2008.UHD.DV.mkv"):
            self.assertIsNone(parse_series_filename(n), n)

    def test_un_titulo_con_numeros_no_se_confunde_con_3x01(self):
        """`2049` o `1917` pegados no son «temporada x episodio»."""
        self.assertIsNone(parse_series_filename("1917 (2019).mkv"))
        self.assertIsNone(parse_series_filename("Blade Runner 2049 (2017).mkv"))


class _TmdbFalso:
    """Sustituye las cuatro funciones de red y apunta a quién se llamó."""

    def __init__(self, test):
        import services.tmdb as tmdb
        self.tmdb = tmdb
        self.peliculas_buscadas: list[tuple] = []
        self.series_buscadas: list[tuple] = []
        self._orig = {n: getattr(tmdb, n) for n in
                      ("is_configured", "search_movies", "fetch_details",
                       "search_tv_series", "fetch_tv_details", "fetch_tv_season")}
        test.addCleanup(self._restaurar)

        async def _search_movies(title, year, limit=1):
            self.peliculas_buscadas.append((title, year))
            return []

        async def _search_tv(query, year=None):
            self.series_buscadas.append((query, year))
            return [tmdb.TvSearchResult(tmdb_id=1399, name="Juego de Tronos",
                                        first_air_date="2011-04-17", year=2011)]

        async def _tv_details(tmdb_id):
            return tmdb.TvDetails(
                tmdb_id=tmdb_id, name="Juego de Tronos",
                original_name="Game of Thrones", first_air_date="2011-04-17",
                year=2011, overview="Nueve familias nobles…",
                poster_url="https://img/poster.jpg",
                backdrop_url="https://img/backdrop.jpg",
                number_of_seasons=8, vote_average=8.4,
                tmdb_url="https://www.themoviedb.org/tv/1399")

        async def _tv_season(tmdb_id, season_number):
            return [tmdb.TvEpisode(episode_number=1, name="Valar Dohaeris",
                                   overview="Jon conoce a Mance Rayder.",
                                   air_date="2013-03-31", runtime_minutes=55,
                                   still_url="https://img/still.jpg")]

        tmdb.is_configured = lambda: True
        tmdb.search_movies = _search_movies
        tmdb.search_tv_series = _search_tv
        tmdb.fetch_tv_details = _tv_details
        tmdb.fetch_tv_season = _tv_season

    def _restaurar(self):
        for n, f in self._orig.items():
            setattr(self.tmdb, n, f)


class TestLaFichaSaleDelIndiceCorrecto(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tmdb = _TmdbFalso(self)

    def _lookup(self, nombre):
        r = self.client.post("/api/cmv40/tmdb-lookup",
                             json={"source_mkv_name": nombre})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_un_episodio_no_toca_el_indice_de_peliculas(self):
        """El fallo, en una aserción."""
        self._lookup(GOT)
        self.assertEqual(self.tmdb.peliculas_buscadas, [],
                         "ha buscado el episodio entre las películas")
        self.assertEqual(self.tmdb.series_buscadas, [("Juego de tronos", 2011)])

    def test_la_ficha_es_la_de_la_serie(self):
        d = self._lookup(GOT)["details"]
        self.assertEqual(d["title"], "Juego de Tronos")
        self.assertEqual(d["year"], 2011)
        self.assertEqual(d["tmdb_id"], 1399)

    def test_dice_de_que_episodio_es(self):
        d = self._lookup(GOT)["details"]
        self.assertTrue(d["es_serie"])
        self.assertEqual((d["temporada"], d["episodio"]), (3, 1))
        self.assertEqual(d["episodio_titulo"], "Valar Dohaeris")

    def test_la_sinopsis_y_la_duracion_son_las_del_EPISODIO(self):
        """Es el fichero que el usuario acaba de abrir, no la serie entera."""
        d = self._lookup(GOT)["details"]
        self.assertEqual(d["overview"], "Jon conoce a Mance Rayder.")
        self.assertEqual(d["runtime_minutes"], 55)

    def test_el_poster_es_el_de_la_serie_y_el_still_va_de_fondo(self):
        """La tarjeta espera un retrato; el `still` del episodio es apaisado."""
        d = self._lookup(GOT)["details"]
        self.assertEqual(d["poster_url"], "https://img/poster.jpg")
        self.assertEqual(d["backdrop_url"], "https://img/still.jpg")

    def test_una_pelicula_sigue_yendo_al_indice_de_peliculas(self):
        self._lookup("Blade Runner 2049 (2017).mkv")
        self.assertEqual(self.tmdb.series_buscadas, [],
                         "ha buscado una película entre las series")
        # El título conserva el 2049 y el año es el 2017: el caso de los dos
        # años que `parse_mkv_filename` ya resolvía.
        self.assertEqual(self.tmdb.peliculas_buscadas,
                         [("Blade Runner 2049", 2017)])


class TestSinDatosNoSeInventaNada(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tmdb = _TmdbFalso(self)

    def test_sin_serie_encontrada_devuelve_ficha_vacia(self):
        import services.tmdb as tmdb
        async def _nada(query, year=None): return []
        tmdb.search_tv_series = _nada
        r = self.client.post("/api/cmv40/tmdb-lookup",
                             json={"source_mkv_name": GOT}).json()
        self.assertIsNone(r["details"])
        self.assertEqual((r["temporada"], r["episodio"]), (3, 1))

    def test_si_falla_la_temporada_queda_la_ficha_de_la_serie(self):
        """Perder el episodio es un detalle; perder la carátula, no."""
        import services.tmdb as tmdb
        async def _revienta(tmdb_id, season_number): raise RuntimeError("TMDb 500")
        tmdb.fetch_tv_season = _revienta
        d = self.client.post("/api/cmv40/tmdb-lookup",
                             json={"source_mkv_name": GOT}).json()["details"]
        self.assertEqual(d["title"], "Juego de Tronos")
        self.assertEqual(d["poster_url"], "https://img/poster.jpg")
        # Sin datos del episodio se cae al título que traía el fichero.
        self.assertEqual(d["episodio_titulo"], "Valar Dohaeris")


if __name__ == "__main__":
    unittest.main()
