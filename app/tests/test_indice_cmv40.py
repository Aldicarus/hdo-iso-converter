"""El índice del sidebar de CMv4.0 sobrevive al reinicio.

El cache del summary vivía SOLO en memoria, así que cada reinicio del
contenedor lo vaciaba y la primera petición volvía a leer las sesiones
enteras. Medido sobre el NAS con 117 proyectos: **59,0 MB leídos y 0,61 s de
parseo para producir 0,79 MB**, con el 82 % de esos bytes en los cinco campos
que se vacían acto seguido. Leerlos son **1,55 s con la ARC de ZFS caliente**,
y ese tramo es el que se dispara cuando un rip escribe en el mismo vdev — que
es el síntoma reportado: «tarda, pero solo la primera vez».

Estos tests miden lo que de verdad importa, que es **cuántos ficheros se
leen**, no si existe un fichero de índice.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage  # noqa: E402


def _sesion(i: int) -> dict:
    """Una sesión con los campos pesados poblados, como las del NAS."""
    return {
        "id": f"peli_{i}_1700000000",
        "output_mkv_name": f"Peli {i}.mkv",
        "phase": "done",
        "running_phase": None,
        # Los cinco que el summary vacía: aquí es donde está el 82 %.
        "output_log": [f"línea {n}" for n in range(200)],
        "phase_history": [{"phase": "extract", "status": "done"}] * 20,
        "source_l2_combos": [{"a": n} for n in range(300)],
        "target_l2_combos": [{"b": n} for n in range(300)],
        "target_l8_combos": [{"c": n} for n in range(300)],
    }


class _Espia:
    """Cuenta qué ficheros se leen de verdad, envolviendo `Path.read_text`."""

    def __init__(self):
        self.leidos: list[str] = []
        self._orig = Path.read_text

    def __enter__(self):
        espia = self

        def _read_text(self, *a, **kw):          # noqa: N805
            espia.leidos.append(self.name)
            return espia._orig(self, *a, **kw)

        Path.read_text = _read_text
        return self

    def __exit__(self, *e):
        Path.read_text = self._orig

    def sesiones(self) -> list[str]:
        return [n for n in self.leidos if n.endswith(".json")]

    def indices(self) -> list[str]:
        return [n for n in self.leidos if n.endswith(".idx")]


class BaseIndice(unittest.TestCase):
    N = 12

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self.dir_cmv40 = raiz / "cmv40"
        self.dir_cmv40.mkdir()
        for i in range(self.N):
            (self.dir_cmv40 / f"peli_{i}.json").write_text(
                json.dumps(_sesion(i)), encoding="utf-8")

        self._orig_dir = storage.CMV40_DIR
        self._orig_idx = storage._CMV40_INDICE
        storage.CMV40_DIR = self.dir_cmv40
        storage._CMV40_INDICE = raiz / "cmv40_summary.idx"
        self.addCleanup(self._restaurar)
        self._reiniciar(borrar_indice=True)

    def _restaurar(self):
        storage.CMV40_DIR = self._orig_dir
        storage._CMV40_INDICE = self._orig_idx
        storage._cmv40_summary_by_file.clear()
        storage._cmv40_indice_cargado = False
        storage._cmv40_indice_volcado_en = 0.0
        self.tmp.cleanup()

    def _reiniciar(self, borrar_indice: bool = False):
        """Lo que hace un reinicio del contenedor: la memoria se va, el disco no."""
        storage._cmv40_summary_by_file.clear()
        storage._cmv40_indice_cargado = False
        storage._cmv40_indice_volcado_en = 0.0
        if borrar_indice:
            storage._CMV40_INDICE.unlink(missing_ok=True)


class TestElArranqueEnFrioNoReleeLasSesiones(BaseIndice):

    def test_tras_un_reinicio_solo_se_lee_el_indice(self):
        """El caso del NAS: 59,0 MB en 117 ficheros → un fichero de 0,79 MB."""
        primera = storage.list_cmv40_sessions_summary()
        self.assertEqual(len(primera), self.N)

        self._reiniciar()
        with _Espia() as e:
            segunda = storage.list_cmv40_sessions_summary()

        self.assertEqual(e.sesiones(), [],
                         "el arranque en frío ha vuelto a leer sesiones")
        self.assertEqual(len(e.indices()), 1, "debería leer el índice, una vez")
        self.assertEqual({s["id"] for s in segunda},
                         {s["id"] for s in primera})

    def test_sin_indice_se_leen_todas(self):
        """La vía de siempre sigue ahí: el índice acelera, no es un requisito."""
        with _Espia() as e:
            storage.list_cmv40_sessions_summary()
        self.assertEqual(len(e.sesiones()), self.N)

    def test_la_sesion_que_cambio_con_el_contenedor_parado_se_relee(self):
        """El `stat()` sigue mandando — 1 ms para los 117, y no hay modo de
        fallo nuevo: un índice viejo se corrige solo, fichero a fichero."""
        storage.list_cmv40_sessions_summary()
        self._reiniciar()

        cambiada = self.dir_cmv40 / "peli_3.json"
        d = _sesion(3)
        d["output_mkv_name"] = "Peli 3 renombrada.mkv"
        cambiada.write_text(json.dumps(d), encoding="utf-8")

        with _Espia() as e:
            out = storage.list_cmv40_sessions_summary()

        self.assertEqual(e.sesiones(), ["peli_3.json"],
                         "debería releer SOLO la que cambió")
        nombre = {s["id"]: s["output_mkv_name"] for s in out}["peli_3_1700000000"]
        self.assertEqual(nombre, "Peli 3 renombrada.mkv")

    def test_una_sesion_borrada_desaparece_del_listado(self):
        storage.list_cmv40_sessions_summary()
        self._reiniciar()
        (self.dir_cmv40 / "peli_5.json").unlink()

        out = storage.list_cmv40_sessions_summary()
        self.assertEqual(len(out), self.N - 1)
        self.assertNotIn("peli_5_1700000000", {s["id"] for s in out})


class TestElIndiceGuardaElSummary(BaseIndice):

    def test_los_campos_pesados_no_entran_en_el_indice(self):
        """Si entraran, el índice pesaría lo que las sesiones y no serviría de
        nada — son el 82 % de los bytes medidos en el NAS."""
        storage.list_cmv40_sessions_summary()
        crudo = json.loads(storage._CMV40_INDICE.read_text(encoding="utf-8"))
        self.assertEqual(len(crudo), self.N)
        for nombre, (_mtime, _size, summary) in crudo.items():
            for campo in storage._CMV40_SUMMARY_EMPTY_LIST_FIELDS:
                self.assertEqual(summary[campo], [],
                                 f"{nombre}: {campo} viaja en el índice")

    def test_el_indice_es_mucho_mas_pequeno_que_las_sesiones(self):
        storage.list_cmv40_sessions_summary()
        sesiones = sum(p.stat().st_size for p in self.dir_cmv40.glob("*.json"))
        indice = storage._CMV40_INDICE.stat().st_size
        self.assertLess(indice * 4, sesiones,
                        f"índice {indice} B contra {sesiones} B de sesiones")

    def test_el_summary_que_sale_del_indice_es_el_mismo(self):
        """Un índice que devolviera otra cosa sería peor que no tenerlo."""
        primera = storage.list_cmv40_sessions_summary()
        self._reiniciar()
        segunda = storage.list_cmv40_sessions_summary()
        self.assertEqual(
            sorted(json.dumps(s, sort_keys=True) for s in primera),
            sorted(json.dumps(s, sort_keys=True) for s in segunda))


class TestUnIndiceRotoNoRompeNada(BaseIndice):

    def test_indice_corrupto_cae_a_leer_las_sesiones(self):
        storage.list_cmv40_sessions_summary()
        self._reiniciar()
        storage._CMV40_INDICE.write_text("{esto no es json", encoding="utf-8")

        with _Espia() as e:
            out = storage.list_cmv40_sessions_summary()

        self.assertEqual(len(out), self.N)
        self.assertEqual(len(e.sesiones()), self.N,
                         "sin índice válido hay que leer las sesiones")

    def test_una_entrada_sin_id_se_ignora(self):
        """El mismo guard que al leer una sesión: sin `id` no es una."""
        storage.list_cmv40_sessions_summary()
        crudo = json.loads(storage._CMV40_INDICE.read_text(encoding="utf-8"))
        crudo["peli_0.json"] = [1, 1, {"no_soy": "una sesión"}]
        storage._CMV40_INDICE.write_text(json.dumps(crudo), encoding="utf-8")
        self._reiniciar()

        out = storage.list_cmv40_sessions_summary()
        self.assertEqual(len(out), self.N)   # se relee del disco, no se pierde


class TestLaExtensionNoEsJson(unittest.TestCase):
    """`.idx`, y no es cosmético.

    `list_sessions` hace `glob("*.json")` sobre `/config` y el listado de
    CMv4.0 sobre `/config/cmv40`. Con extensión `.json` el índice se colaría
    como si fuera una sesión: lo pararía el guard del `id`, pero después de
    leer y parsear sus 0,79 MB en CADA listado — o sea que el fichero puesto
    para ahorrar I/O lo añadiría. Es la trampa del sidecar `.progress`.
    """

    def test_el_indice_no_acaba_en_json(self):
        self.assertFalse(storage._CMV40_INDICE.name.endswith(".json"),
                         storage._CMV40_INDICE.name)

    def test_no_lo_recoge_el_glob_de_ninguno_de_los_dos_listados(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            (raiz / "cmv40").mkdir()
            (raiz / storage._CMV40_INDICE.name).write_text("{}", encoding="utf-8")
            self.assertEqual(list(raiz.glob("*.json")), [])
            self.assertEqual(list((raiz / "cmv40").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
