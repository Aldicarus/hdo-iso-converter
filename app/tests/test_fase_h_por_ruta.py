# -*- coding: utf-8 -*-
"""La Fase H cuenta QUÉ comprueba, y eso depende de la ruta.

El usuario preguntó por qué la fase se llama «validar y guardar» si «sólo
mueve el MKV y borra los temporales». Es una lectura razonable de lo que
había en pantalla, y la respuesta depende de por dónde vaya el job:

  · **drop-in** — el RPU se copió entero del bin, así que la Fase H lee el
    frame count con `ffprobe`, comprueba el contenedor con `mkvmerge -J` y
    mueve. Son segundos, y de ahí la impresión de que no comprueba nada.
  · **merge** — el RPU se recompuso frame a frame, así que se hace un
    `extract-rpu` COMPLETO del HEVC y se verifican frame count, `cm_version`,
    `el_type` y la presencia de L8. En un UHD son minutos.

Dos órdenes de magnitud y dos cosas distintas, contadas con el MISMO texto.
Estos tests ejecutan el resolutor del relato y la card real, porque el bug no
estaba en la forma del código sino en que un texto único servía para dos
ramas — y eso se lee perfectamente bien.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import i18n as i18n_srv  # noqa: E402
from models import CMv40Session, DoviInfo  # noqa: E402
from phases.cmv40_relato import porque_de_fase  # noqa: E402
from phases.cmv40_strategy import resolve_plan  # noqa: E402
from frontend_sources import (argv_node, catalogo_es,  # noqa: E402
                              js_en_disco, motor_en_disco)

NODE = shutil.which("node")


def _sesion(**campos) -> CMv40Session:
    base = dict(id="cmv40_h_1700000000", source_mkv_path="/x/a.mkv",
                source_mkv_name="a.mkv", output_mkv_name="Peli (2026).mkv")
    base.update(campos)
    return CMv40Session(**base)


DROP_IN = dict(source_workflow="p7_fel", target_type="trusted_p7_fel_final",
               target_trust_ok=True)
MERGE = dict(source_workflow="p7_mel", target_type="trusted_p7_mel_final",
             target_trust_ok=True)


class TestElRelatoDiceQueSeComprueba(unittest.TestCase):

    def _porque(self, **campos):
        s = _sesion(**campos)
        return porque_de_fase(s, "validate", plan=resolve_plan(s))

    def test_las_dos_rutas_cuentan_cosas_distintas(self):
        rapido, completo = self._porque(**DROP_IN), self._porque(**MERGE)
        self.assertNotEqual(rapido, completo)
        # Y cada una la SUYA, contra la clave y no contra una palabra suelta:
        # la redacción se reescribe y el anclaje tiene que sobrevivir.
        self.assertEqual(rapido, i18n_srv.t(
            'relato.porque_fase_h_rapido', nombre="Peli (2026).mkv"))
        self.assertEqual(completo, i18n_srv.t(
            'relato.porque_fase_h_completo', nombre="Peli (2026).mkv"))

    def test_se_ancla_en_fast_path_que_es_lo_que_la_fase_ramifica(self):
        """No en `drop_in`: hoy coinciden, y si divergen manda la fase."""
        for wf, tipo, trust in (("p7_fel", "trusted_p7_fel_final", True),
                                ("p7_fel", "trusted_p7_fel_final", False),
                                ("p7_fel", "generic", False),
                                ("p7_mel", "trusted_p7_mel_final", True),
                                ("p8", "trusted_p8_source", True),
                                ("p8", "generic", False)):
            s = _sesion(source_workflow=wf, target_type=tipo,
                        target_trust_ok=trust)
            plan = resolve_plan(s)
            esperada = i18n_srv.t(
                'relato.porque_fase_h_rapido' if plan.validate.fast_path
                else 'relato.porque_fase_h_completo',
                nombre="Peli (2026).mkv")
            with self.subTest(wf=wf, rapido=plan.validate.fast_path):
                self.assertEqual(porque_de_fase(s, "validate", plan=plan),
                                 esperada)

    def test_sin_plan_no_se_inventa_una_rama(self):
        """Como la Fase C: antes que adivinar la ruta, no se dice nada."""
        self.assertEqual(porque_de_fase(_sesion(**DROP_IN), "validate"), "")

    def test_el_nombre_del_mkv_sigue_viajando(self):
        self.assertIn("Peli (2026).mkv", self._porque(**DROP_IN))
        self.assertIn("Peli (2026).mkv", self._porque(**MERGE))


class TestLaFaseNombraLoQueHace(unittest.TestCase):
    """El nombre se conserva la comprobación porque la fase comprueba.

    Lo que cambia es que nombra su OBJETO, como las otras siete etapas, y usa
    «comprobar» para no chocar con la validación PREVIA ni con la card 🛡️
    Validaciones, que son otras dos cosas distintas.
    """

    def test_la_etapa_nombra_el_mkv(self):
        for idioma, trozo in (("es", "MKV"), ("en", "MKV"), ("ca", "MKV")):
            with self.subTest(idioma):
                self.assertIn(trozo, i18n_srv._catalogo(idioma)['relato.etapa_validate'])

    def test_ninguna_etapa_promete_solo_mover(self):
        """«Mover el MKV» sería falso por la rama merge: son dos
        `extract-rpu` completos antes de mover nada."""
        for idioma in ("es", "en", "ca"):
            etapa = i18n_srv._catalogo(idioma)['relato.etapa_validate'].lower()
            self.assertTrue(
                # «comprovar» en catalán va con v.
                any(v in etapa for v in ("comprob", "comprov", "check")),
                f"{idioma}: la etapa dejó de decir que comprueba → {etapa}")


@unittest.skipUnless(NODE, "node no disponible")
class TestLaCardDeLaFaseHSigueLaRuta(unittest.TestCase):

    _DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.JS_CONCAT, 'utf8');
function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('no encontrada: ' + name);
  let d = 0, ab = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { d++; ab = true; }
    else if (src[j] === '}') { d--; if (ab && d === 0) return src.slice(i, j + 1); }
  }
  throw new Error('sin cerrar: ' + name);
}
const bundle = [
  fs.readFileSync(process.env.MOTOR_I18N, 'utf8'),
  grab('escHtml'), grab('_cmv40Plan'), grab('_cmv40Trust'),
  grab('_cmv40DropIn'), grab('_cmv40DosCapas'), grab('_cmv40FaseHBody'),
  'return { _cmv40FaseHBody };',
].join('\n');
const api = new Function(bundle)();
const casos = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(
  casos.map(s => api._cmv40FaseHBody('p1', s))));
"""

    def _cards(self, *sesiones):
        r = subprocess.run(
            argv_node(self._DRIVER),
            env={**os.environ, "JS_CONCAT": js_en_disco(),
                 "MOTOR_I18N": motor_en_disco()},
            input=json.dumps(list(sesiones)), capture_output=True,
            text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        return json.loads(r.stdout)

    def test_la_card_lee_el_plan_no_lo_re_deriva(self):
        cat = catalogo_es()
        rapida, completa = self._cards(
            {"plan": {"validate": {"fast_path": True}}},
            {"plan": {"validate": {"fast_path": False}}})
        self.assertIn(cat['tab3.que_pasa_fase_h_rapido'], rapida)
        self.assertIn(cat['tab3.que_pasa_fase_h_completo'], completa)
        self.assertNotIn(cat['tab3.que_pasa_fase_h_completo'], rapida)

    def test_la_rapida_dice_que_son_segundos_y_la_larga_que_son_minutos(self):
        """El dato que le faltaba al usuario: cuánto cuesta cada una."""
        rapida, completa = self._cards(
            {"plan": {"validate": {"fast_path": True}}},
            {"plan": {"validate": {"fast_path": False}}})
        self.assertIn("segundos", rapida)
        self.assertIn("minutos", completa)

    def test_sin_plan_cae_al_respaldo_local(self):
        """Sesiones cacheadas de antes del cambio y el summary del sidebar.

        Sin respaldo la card se quedaría siempre con la rama larga, que en un
        drop-in anuncia unos minutos de trabajo que no van a ocurrir.
        """
        cat = catalogo_es()
        [card] = self._cards({"target_type": "trusted_p7_fel_final",
                              "source_workflow": "p7_fel",
                              "target_trust_ok": True})
        self.assertIn(cat['tab3.que_pasa_fase_h_rapido'], card)

    def test_el_boton_sigue_disparando_la_fase(self):
        [card] = self._cards({"plan": {"validate": {"fast_path": True}}})
        self.assertIn("cmv40DoValidate('p1')", card)


if __name__ == "__main__":
    unittest.main()
