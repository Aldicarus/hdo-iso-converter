"""Fallback al M2TS principal cuando mkvmerge no puede procesar el playlist.

Cubre el bug real de Avatar Fuego y Ceniza (2025): mkvmerge v88 aborta con
``Assertion 'file_names.size() == play_items.size()' failed`` en
``add_filelists_for_playlists`` al muxear desde el .mpls de un disco UHD
multi-segmento. El crash (SIGABRT → returncode -6) NO lo capturaba el viejo
check ``returncode >= 2`` y se enmascaraba aguas abajo como un críptico
``[Errno 2] No such file or directory``.

Estos tests fijan las dos piezas puras del fix:
  1. `is_playlist_assertion_line` — detección de la línea del assertion.
  2. `m2ts_covers_title` — gate de duración que evita producir un MKV truncado
     en silencio cuando el disco usa seamless branching real.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_playlist_fallback -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.phase_d import (  # noqa: E402
    MkvmergePlaylistError,
    is_playlist_assertion_line,
    m2ts_covers_title,
)


class TestPlaylistAssertionDetection(unittest.TestCase):
    def test_detecta_linea_real_del_assertion(self):
        # La línea exacta que emitió mkvmerge v88 con la ISO de Avatar.
        line = (
            "mkvmerge: src/merge/mkvmerge.cpp:3069: void "
            "add_filelists_for_playlists(): Assertion "
            "`file_names.size() == play_items.size()' failed."
        )
        self.assertTrue(is_playlist_assertion_line(line))

    def test_detecta_por_nombre_de_funcion(self):
        self.assertTrue(
            is_playlist_assertion_line("... add_filelists_for_playlists() ...")
        )

    def test_detecta_por_expresion_del_assertion(self):
        self.assertTrue(
            is_playlist_assertion_line("Assertion play_items.size() failed")
        )

    def test_no_dispara_con_lineas_normales(self):
        for ok in (
            "Scanning 11 files in 1 playlist.",
            "Progress: 0%",
            "mkvmerge v88.0 ('All I Know') 64-bit",
            "The file '/mnt/output/movie.mkv' has been opened for writing.",
            "",
        ):
            self.assertFalse(is_playlist_assertion_line(ok), ok)


class TestM2tsCoversTitle(unittest.TestCase):
    def test_duraciones_identicas_cubre(self):
        self.assertTrue(m2ts_covers_title(11840.7, 11840.7))

    def test_dentro_de_tolerancia_cubre(self):
        # M2TS 1% más corto que el playlist → dentro del 2% → cubre.
        self.assertTrue(m2ts_covers_title(11840.0, 11722.0))

    def test_m2ts_mas_largo_cubre(self):
        # Un M2TS más largo que el playlist (incluye algo extra) cubre el título.
        self.assertTrue(m2ts_covers_title(11840.0, 12000.0))

    def test_m2ts_claramente_corto_no_cubre(self):
        # Seamless branching real: el M2TS principal es la mitad del título.
        self.assertFalse(m2ts_covers_title(11840.0, 5900.0))

    def test_justo_en_el_limite(self):
        # Exactamente 2% más corto → cubre (<=). Justo por encima → no cubre.
        self.assertTrue(m2ts_covers_title(10000.0, 9800.0))   # 2.0 %
        self.assertFalse(m2ts_covers_title(10000.0, 9799.0))  # 2.01 %

    def test_duracion_desconocida_asume_que_cubre(self):
        # Si no se puede medir alguna duración (0), se procede con el fallback:
        # el caso dominante es un único M2TS que contiene toda la película.
        self.assertTrue(m2ts_covers_title(0.0, 11840.0))
        self.assertTrue(m2ts_covers_title(11840.0, 0.0))
        self.assertTrue(m2ts_covers_title(0.0, 0.0))


class TestExceptionType(unittest.TestCase):
    def test_es_subclase_de_runtimeerror(self):
        # El orquestador captura MkvmergePlaylistError específicamente, pero el
        # except general de _run_pipeline (Exception) debe seguir cogiéndola si
        # escapara sin reintento.
        self.assertTrue(issubclass(MkvmergePlaylistError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
