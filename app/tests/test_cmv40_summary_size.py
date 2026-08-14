"""
Guard del tamaño del summary de /api/cmv40 (sidebar de Tab 3).

Bug real (2026-08-14, 78 proyectos en producción): el summary solo vaciaba
`output_log` y `phase_history`, pero los campos de combos L2/L8 — añadidos
después — aportaban 14,0 MB de un payload de 14,8 MB. La respuesta tardaba
50 s con la caché fría y el `apiFetch` del frontend aborta a los 30 s, así
que el sidebar se quedaba **permanentemente vacío** tras cada reinicio del
contenedor: el fetch devolvía null y la lista no se pintaba nunca.

La causa de fondo no son esos tres campos concretos, sino que la lista de
campos a vaciar es manual y nadie la actualiza al añadir uno nuevo. Por eso
este test **puebla todos los campos de tipo lista del modelo** con datos
sintéticos: si alguien añade un campo pesado y no lo declara en
`_CMV40_SUMMARY_EMPTY_LIST_FIELDS`, el summary engorda y esto falla.
"""
import json
import sys
import tempfile
import typing
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage  # noqa: E402
from models import CMv40Session  # noqa: E402

# Presupuesto por sesión. Con ~80 proyectos deja el payload en pocos MB como
# mucho, muy por debajo del timeout del frontend.
MAX_SUMMARY_BYTES = 25 * 1024

ITEMS_PER_LIST = 2000

# Campos lista cuya longitud tiene tope natural: no engordan el summary y el
# frontend los necesita ahí (`phases_skipped` alimenta la phase-strip;
# `critical_gate_failures` el banner de gates críticos). Medido en producción
# con 78 proyectos: máximo 5 elementos y 111 B por sesión.
#
# Todo campo lista NUEVO que no esté aquí se puebla con datos masivos: el test
# obliga a clasificarlo — o es pesado (va a la tupla de storage) o es acotado
# (se declara aquí, con el porqué).
BOUNDED_LIST_FIELDS = {
    "phases_skipped": 8,             # nº de fases del pipeline
    "critical_gate_failures": 8,     # nº de trust gates
    "source_l2_target_pqs": 12,      # target displays declarados en el L2
    "target_l2_target_pqs": 12,
    "target_l8_target_indices": 12,  # target displays del L8
}


def _list_fields() -> list[str]:
    """Campos de CMv40Session declarados como lista."""
    out = []
    for name, field in CMv40Session.model_fields.items():
        ann = field.annotation
        if typing.get_origin(ann) is list:
            out.append(name)
    return out


def _unbounded_list_fields() -> list[str]:
    """Los que pueden crecer sin tope → deben vaciarse en el summary."""
    return [f for f in _list_fields() if f not in BOUNDED_LIST_FIELDS]


class TestCMv40SummarySize(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = storage.CMV40_DIR
        storage.CMV40_DIR = Path(self._tmp.name)
        storage._cmv40_summary_by_file.clear()

    def tearDown(self):
        storage.CMV40_DIR = self._orig_dir
        storage._cmv40_summary_by_file.clear()
        self._tmp.cleanup()

    def _write_fat_session(self, sid="cmv40_fat_2025_1") -> list[str]:
        """Sesión con TODOS los campos lista poblados. Se escribe como JSON
        crudo a propósito: el summary hace json.loads sin validar el modelo,
        así que no hace falta construir items válidos de cada tipo."""
        fields = _list_fields()
        data = {
            "id": sid,
            "source_mkv_path": "/mnt/library/Peli (2025) [DV FEL].mkv",
            "source_mkv_name": "Peli (2025) [DV FEL].mkv",
            "output_mkv_name": "Peli (2025) [DV FEL] [CMv4.0].mkv",
            "artifacts_dir": "/mnt/tmp/cmv40/" + sid,
            "phase": "done",
        }
        filler = {"a": 1234.5678, "b": "xxxxxxxxxxxxxxxxxxxx", "c": [1, 2, 3, 4]}
        for f in fields:
            n = BOUNDED_LIST_FIELDS.get(f, ITEMS_PER_LIST)
            data[f] = [dict(filler, i=i) for i in range(n)]
        (Path(self._tmp.name) / f"{sid}.json").write_text(
            json.dumps(data), encoding="utf-8")
        return fields

    def test_hay_campos_lista_que_vigilar(self):
        # Si esto falla, la introspección dejó de encontrar campos y el resto
        # del guard sería vacuo.
        self.assertGreaterEqual(len(_unbounded_list_fields()), 3)

    def test_summary_no_arrastra_los_campos_pesados(self):
        self._write_fat_session()
        summaries = storage.list_cmv40_sessions_summary()
        self.assertEqual(len(summaries), 1)
        s = summaries[0]

        gordos = [f for f in _unbounded_list_fields() if s.get(f)]
        self.assertEqual(
            gordos, [],
            f"campos lista que el summary NO vacía: {gordos} — si crecen sin "
            "tope añádelos a storage._CMV40_SUMMARY_EMPTY_LIST_FIELDS; si están "
            "acotados por diseño, declárralos en BOUNDED_LIST_FIELDS con el porqué",
        )

    def test_los_campos_acotados_siguen_llegando_al_frontend(self):
        # Vaciar de más rompería la phase-strip (fases omitidas) y el banner
        # de gates críticos, y sería un fallo mucho menos visible.
        self._write_fat_session()
        s = storage.list_cmv40_sessions_summary()[0]
        for f, n in BOUNDED_LIST_FIELDS.items():
            self.assertEqual(len(s.get(f) or []), n,
                             f"'{f}' está acotado por diseño y no debe vaciarse")

    def test_summary_dentro_del_presupuesto(self):
        self._write_fat_session()
        s = storage.list_cmv40_sessions_summary()[0]
        size = len(json.dumps(s))
        self.assertLess(
            size, MAX_SUMMARY_BYTES,
            f"el summary de una sesión pesa {size} B (límite {MAX_SUMMARY_BYTES} B). "
            "Con decenas de proyectos el payload de /api/cmv40 supera el timeout "
            "de 30 s del frontend y el sidebar se queda vacío.",
        )

    def test_los_campos_declarados_existen_en_el_modelo(self):
        # Un typo en la tupla dejaría el campo real sin vaciar y silenciosamente.
        for f in storage._CMV40_SUMMARY_EMPTY_LIST_FIELDS:
            self.assertIn(f, CMv40Session.model_fields,
                          f"'{f}' no existe en CMv40Session")

    def test_el_sidebar_conserva_lo_que_necesita(self):
        # Vaciar de más rompería el sidebar de forma menos visible.
        self._write_fat_session()
        s = storage.list_cmv40_sessions_summary()[0]
        for key in ("id", "source_mkv_name", "output_mkv_name", "phase"):
            self.assertTrue(s.get(key), f"el summary perdió '{key}'")

    def test_claves_presentes_aunque_vacias(self):
        # Se vacían, no se borran: hay consumidores que asumen el campo.
        self._write_fat_session()
        s = storage.list_cmv40_sessions_summary()[0]
        for f in storage._CMV40_SUMMARY_EMPTY_LIST_FIELDS:
            self.assertIn(f, s)
            self.assertEqual(s[f], [])


if __name__ == "__main__":
    unittest.main()
