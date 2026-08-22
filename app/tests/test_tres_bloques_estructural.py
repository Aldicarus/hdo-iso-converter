"""Tres bloques de subtítulos: los DOS extremos, alineados.

Un disco con `normal + SDH + forzado` por idioma (The Mandalorian and Grogu:
15 subs = 3 × 5 idiomas) rompía las dos señales estructurales, que asumían
exactamente DOS bloques:

  · `phase_a._build_subtitle_tracks` partía en la primera repetición de idioma
    y marcaba forzado (bitrate sintético 1.0) **todo lo posterior**, así que la
    SDH del bloque 2 salía como forzada;
  · `phase_e._classify_sub_source_ids` hacía lo mismo con los source ids, así
    que la categoría "forzados" de cada idioma contenía [SDH, forzado] y el
    matcher, que consume 1:1 y en orden, entregaba la **SDH** a la pista
    etiquetada "Forzados".

Arreglar solo una de las dos es peor que no arreglar ninguna: la selección
saldría bien y el contenido cruzado, que es el bug de Obsession 2025 otra vez.
La regla del proyecto lo dice: las dos tienen que usar la MISMA base.

Generalización: **el forzado es la ÚLTIMA aparición de un idioma que se
repite**. Con dos bloques da exactamente lo de antes; con tres, coloca la SDH
donde toca. Un idioma que solo aparece una vez sigue siendo completo.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_tres_bloques_estructural -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from phases.phase_a import _build_subtitle_tracks  # noqa: E402
from phases.phase_e import _classify_sub_source_ids  # noqa: E402

_LANGS3 = ("eng", "fre", "spa", "ger", "ita")


def _mkvmerge_subs(bloques):
    """La LISTA de tracks de `mkvmerge -J`, con los subs en orden de bloques."""
    tracks = [{"type": "video", "properties": {}}]
    for bloque in bloques:
        for lang in bloque:
            tracks.append({"type": "subtitles", "codec": "HDMV PGS",
                           "properties": {"language": lang}})
    return tracks


def _track_map(bloques):
    """El `track_map` de phase_e: source id → idioma en inglés."""
    tm, sid = {}, 0
    for bloque in bloques:
        for lang in bloque:
            tm[sid] = {"language": lang, "type": "subtitles"}
            sid += 1
    return tm, list(range(sid))


class TestPhaseA(unittest.TestCase):

    def test_con_tres_bloques_solo_el_tercero_es_forzado(self):
        subs = _build_subtitle_tracks(_mkvmerge_subs([_LANGS3] * 3))
        self.assertEqual(len(subs), 15)
        bitrates = [t.bitrate_kbps for t in subs]
        self.assertEqual(bitrates[:5], [30.0] * 5, "bloque 1 = normal")
        self.assertEqual(bitrates[5:10], [30.0] * 5,
                         "bloque 2 = SDH, es una COMPLETA")
        self.assertEqual(bitrates[10:], [1.0] * 5, "bloque 3 = forzado")

    def test_con_dos_bloques_el_resultado_no_cambia(self):
        """La garantía de no-regresión: el caso de siempre da lo de siempre."""
        subs = _build_subtitle_tracks(_mkvmerge_subs([_LANGS3] * 2))
        bitrates = [t.bitrate_kbps for t in subs]
        self.assertEqual(bitrates[:5], [30.0] * 5)
        self.assertEqual(bitrates[5:], [1.0] * 5)

    def test_un_idioma_que_solo_aparece_en_el_segundo_bloque_es_completo(self):
        """Sigue valiendo la regla vieja: idioma nuevo en el bloque 2 no es un
        forzado, es la única pista de ese idioma."""
        subs = _build_subtitle_tracks(
            _mkvmerge_subs([("eng", "spa"), ("eng", "spa", "cat")]))
        porb = {(t.language, i): t.bitrate_kbps for i, t in enumerate(subs)}
        catalan = [t for t in subs if t.language.lower().startswith("catal")]
        self.assertEqual(len(catalan), 1, porb)
        self.assertEqual(catalan[0].bitrate_kbps, 30.0)

    def test_una_sola_pista_por_idioma_es_completa(self):
        subs = _build_subtitle_tracks(_mkvmerge_subs([_LANGS3]))
        self.assertEqual([t.bitrate_kbps for t in subs], [30.0] * 5)


class TestPhaseE(unittest.TestCase):

    def test_con_tres_bloques_la_categoria_forzados_solo_lleva_el_tercero(self):
        tm, sids = _track_map([_LANGS3] * 3)
        forced, complete = _classify_sub_source_ids(sids, tm)
        # spa: ids 2 (normal), 7 (SDH), 12 (forzado)
        self.assertEqual(forced.get("spanish"), [12])
        self.assertEqual(complete.get("spanish"), [2, 7])

    def test_con_dos_bloques_el_resultado_no_cambia(self):
        tm, sids = _track_map([_LANGS3] * 2)
        forced, complete = _classify_sub_source_ids(sids, tm)
        self.assertEqual(forced.get("spanish"), [7])
        self.assertEqual(complete.get("spanish"), [2])

    def test_un_idioma_unico_va_a_completos(self):
        tm, sids = _track_map([("eng", "spa"), ("eng", "spa", "cat")])
        forced, complete = _classify_sub_source_ids(sids, tm)
        self.assertEqual(complete.get("catalan"), [4])
        self.assertNotIn("catalan", forced)


class TestLosDosExtremosCoinciden(unittest.TestCase):
    """Lo que de verdad importa: que las dos señales digan lo MISMO.

    Si divergen, la selección de phase_b sale bien y el matcher de phase_e
    entrega el contenido de otra pista — un MKV en el que "Castellano
    Forzados" lleva la SDH completa, sin que nada falle.
    """

    def _comparar(self, bloques):
        subs = _build_subtitle_tracks(_mkvmerge_subs(bloques))
        tm, sids = _track_map(bloques)
        forced, _ = _classify_sub_source_ids(sids, tm)
        ids_forzados = {i for ids in forced.values() for i in ids}
        # phase_a marca forzado con bitrate < 3.0; phase_e, metiendo el id en
        # `forced`. La posición i-ésima de los subs es el source id i.
        segun_a = {i for i, t in enumerate(subs) if t.bitrate_kbps < 3.0}
        self.assertEqual(segun_a, ids_forzados,
                         f"las dos señales discrepan con {bloques}")

    def test_coinciden_con_uno_dos_y_tres_bloques(self):
        for bloques in ([_LANGS3], [_LANGS3] * 2, [_LANGS3] * 3):
            with self.subTest(bloques=len(bloques)):
                self._comparar(bloques)

    def test_coinciden_con_cuatro_bloques(self):
        """No hay disco conocido con cuatro, pero la generalización tiene que
        aguantarlo sin que las dos partes se separen."""
        self._comparar([_LANGS3] * 4)

    def test_coinciden_con_un_idioma_extra_en_el_ultimo_bloque(self):
        self._comparar([("eng", "spa"), ("eng", "spa"), ("eng", "spa", "cat")])


if __name__ == "__main__":
    unittest.main()
