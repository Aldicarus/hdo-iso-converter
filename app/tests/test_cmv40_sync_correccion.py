"""Las correcciones de sync se encadenan; no se recomponen sumando frames.

`dovi_tool editor` no edita in situ: lee un RPU y escribe otro. La Fase E
partía SIEMPRE de `RPU_target.bin`, así que para no perder las correcciones
anteriores el endpoint sumaba los frames de todas y las colapsaba en un único
rango en la cabecera (`remove: ["0-N"]` + `duplicate: N@0`).

Con pasos del mismo signo el resultado coincide. Alternando no: "quitar 10 y
después duplicar 5" no toca los mismos frames que "duplicar 5 y después quitar
10" — el segundo se lleva por delante los 5 duplicados. El Δ final es igual en
los dos casos, que es justo lo que hace que el criterio de avance (Δ = 0) no lo
note.

Ahora cada paso se aplica sobre el resultado del anterior, que es el sistema de
coordenadas en el que el usuario lo dibujó, y `sync_config` guarda el historial.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_sync_correccion -v
"""
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import PhaseTestCase, RpuProps, make_session  # noqa: E402

FRAMES = 1000


class TestFaseEEncadenada(PhaseTestCase):
    """`run_phase_e_correct_sync` ejecutada de verdad."""

    def setUp(self):
        super().setUp()
        self.session = make_session(self.wd, source_frame_count=FRAMES,
                                    target_frame_count=FRAMES + 40)
        (self.wd / "RPU_source.bin").write_bytes(b"\x00" * 64)
        (self.wd / "RPU_target.bin").write_bytes(b"\x00" * 64)
        self.tb.define_rpu("RPU_source.bin", profile=7, el_type="FEL",
                           cm_version="v2.9", frames=FRAMES)
        self.tb.define_rpu("RPU_target.bin", profile=7, el_type="FEL",
                           cm_version="v4.0", frames=FRAMES + 40, has_l8=True)

    async def _corregir(self, cfg):
        from phases.cmv40_pipeline import run_phase_e_correct_sync
        await run_phase_e_correct_sync(self.session, cfg, self.log)

    async def test_el_primer_paso_parte_del_target_original(self):
        await self._corregir({"remove": ["0-39"]})
        editor = self.tb.one("dovi_tool", "editor")
        self.assertEqual(editor.opt_name("-i"), "RPU_target.bin")
        self.assertEqual(self.session.target_frame_count, FRAMES)
        self.assertEqual(self.session.sync_delta, 0)

    async def test_el_segundo_paso_parte_del_rpu_ya_corregido(self):
        """EL BUG: antes el segundo `editor` volvía a leer RPU_target.bin, y
        por eso el endpoint tenía que recomponer los rangos."""
        await self._corregir({"remove": ["0-39"]})
        await self._corregir({"duplicate": [{"source": 0, "offset": 0, "length": 5}]})
        llamadas = self.tb.find("dovi_tool", "editor")
        self.assertEqual(len(llamadas), 2, llamadas)
        self.assertEqual(llamadas[0].opt_name("-i"), "RPU_target.bin")
        self.assertEqual(llamadas[1].opt_name("-i"), "RPU_synced.bin",
                         "el segundo paso debe encadenar, no volver al original")

    async def test_cada_paso_llega_al_editor_tal_cual(self):
        """El config que consume dovi_tool es el del usuario, sin aplanar."""
        paso = {"remove": ["120-129"]}
        await self._corregir(paso)
        self.assertEqual(self.tb.one("dovi_tool", "editor").json_args, paso)
        self.assertEqual(
            json.loads((self.wd / "editor_config.json").read_text()), paso)

    async def test_los_pasos_se_acumulan_en_el_frame_count(self):
        """Δ acumulado: -40 y luego +5 dejan el target 5 frames por encima."""
        await self._corregir({"remove": ["0-39"]})
        await self._corregir({"duplicate": [{"source": 0, "offset": 0, "length": 5}]})
        self.assertEqual(self.session.target_frame_count, FRAMES + 5)
        self.assertEqual(self.session.sync_delta, 5)

    async def test_un_editor_que_falla_no_destruye_la_correccion_previa(self):
        """Se escribe a un temporal y se sustituye al final: si la segunda
        pasada muere, RPU_synced.bin sigue siendo la corrección buena."""
        await self._corregir({"remove": ["0-39"]})
        bueno = (self.wd / "RPU_synced.bin").read_bytes()
        self.tb.fail_when_json("dovi_tool", "editor", {"remove": ["0-9"]},
                               stderr="boom")
        with self.assertRaises(RuntimeError):
            await self._corregir({"remove": ["0-9"]})
        self.assertTrue((self.wd / "RPU_synced.bin").exists())
        self.assertEqual((self.wd / "RPU_synced.bin").read_bytes(), bueno)
        self.assertFalse((self.wd / "RPU_synced.tmp.bin").exists(),
                         "el temporal de una pasada fallida no debe quedarse")

    async def test_la_fase_no_machaca_el_historial_de_pasos(self):
        """`sync_config` lo escribe el endpoint con todos los pasos; la fase
        no debe reducirlo al último."""
        historial = {"steps": [{"remove": ["0-39"]}], "total_removed": 40,
                     "total_duplicated": 0}
        self.session.sync_config = historial
        await self._corregir({"remove": ["0-9"]})
        self.assertEqual(self.session.sync_config, historial)


class TestAcumulacionDePasos(unittest.TestCase):
    """La aritmética del endpoint, sin HTTP: qué historial se construye."""

    @staticmethod
    def _acumular(prev, nuevo):
        """Réplica de la acumulación de `cmv40_apply_sync` (misma fuente)."""
        import re
        src = (APP_DIR / "routers" / "cmv40.py").read_text()
        ini = src.index("    def _count_remove(cfg: dict) -> int:")
        fin = src.index("    } if steps else {}", ini) + len("    } if steps else {}")
        cuerpo = "\n".join(l[4:] if l.startswith("    ") else l
                           for l in src[ini:fin].split("\n"))
        ns = {"session": type("S", (), {"sync_config": prev})(),
              "body": type("B", (), {"editor_config": nuevo})()}
        exec(cuerpo, ns)
        return ns["combined_cfg"]

    def test_primer_paso(self):
        r = self._acumular(None, {"remove": ["0-39"]})
        self.assertEqual(r["steps"], [{"remove": ["0-39"]}])
        self.assertEqual(r["total_removed"], 40)

    def test_segundo_paso_se_anade_no_reemplaza(self):
        prev = {"steps": [{"remove": ["0-39"]}], "total_removed": 40, "total_duplicated": 0}
        r = self._acumular(prev, {"duplicate": [{"source": 0, "offset": 0, "length": 5}]})
        self.assertEqual(len(r["steps"]), 2)
        self.assertEqual(r["total_removed"], 40)
        self.assertEqual(r["total_duplicated"], 5)

    def test_orden_de_los_pasos_preservado(self):
        """Quitar-luego-duplicar y duplicar-luego-quitar dan historiales
        distintos, aunque los totales coincidan. Antes ambos colapsaban al
        mismo `remove 0-44` + `duplicate 45@0`."""
        dup = {"duplicate": [{"source": 0, "offset": 0, "length": 5}]}
        rem = {"remove": ["0-39"]}
        a = self._acumular(self._acumular(None, rem), dup)
        b = self._acumular(self._acumular(None, dup), rem)
        self.assertEqual(a["total_removed"], b["total_removed"])
        self.assertEqual(a["total_duplicated"], b["total_duplicated"])
        self.assertNotEqual(a["steps"], b["steps"])

    def test_sesion_antigua_con_config_aplanado_se_conserva(self):
        """Compat: un proyecto guardado antes de los pasos tiene el config
        aplanado directamente en `sync_config`; no se pierde."""
        r = self._acumular({"remove": ["0-39"]}, {"remove": ["0-9"]})
        self.assertEqual(r["steps"], [{"remove": ["0-39"]}, {"remove": ["0-9"]}])
        self.assertEqual(r["total_removed"], 50)


if __name__ == "__main__":
    unittest.main()
