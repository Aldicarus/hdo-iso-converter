# -*- coding: utf-8 -*-
"""La tabla de niveles de la card de recomendación se lee: medida en Chrome.

Con `grid-template-columns: auto 1fr auto auto` y el valor del bin en
`nowrap`, la columna del bin se quedaba con su ancho entero («2,251
adjustments · max strength 538 · across 84% of the film»): «qué aporta»
acababa en una palabra por renglón y, con menos sitio, el valor del bin se
montaba encima de «tu disco». Salió de mirar la captura de la portada
(2026-10-28), así que se mide pintando la card, no leyendo el CSS.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_tabla_de_niveles -v
"""
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import chrome, pagina_en_chrome  # noqa: E402
import test_las_tres_lenguas_en_pantalla as P  # noqa: E402  (fixture de CMv4.0)

#: Los números de El padrino, el bin con el valor más largo del corpus.
_CAMBIOS = {
    "target_l8_classification": "real", "recommended_action": "merge",
    "target_l8_unique_count": 2251, "target_l8_max_delta": 538,
    "target_l8_neutral_frames_pct": 0.16,
    "target_l8_has_mid_contrast": True, "target_l8_has_clip_trim": True,
    "target_l2_unique_count": 6061, "source_l2_unique_count": 6061,
    "target_l3_unique_count": 621,
}

_GUION = r"""
<script>
window.addEventListener('load', async () => {
  const out = {anchos: {}};
  const espera = (ms) => new Promise(r => setTimeout(r, ms));
  try {
    await espera(300);
    for (const ancho of [780, 620]) {
      const s = Object.assign(JSON.parse(JSON.stringify(window.__SES)), window.__CAMBIOS);
      const caja = document.createElement('div');
      caja.style.cssText = `position:absolute; left:0; top:0; width:${ancho}px`;
      caja.innerHTML = _renderCMv40RecommendationCard(s, s.id);
      document.body.appendChild(caja);
      await espera(300);
      const celdas = [...caja.querySelector('.cmv40-niveles').children].slice(4);
      const filas = [];
      for (let i = 0; i < celdas.length; i += 4) {
        const [, papel, bin, disco] = celdas.slice(i, i + 4);
        filas.push({
          papel: papel.getBoundingClientRect().width,
          binDesborda: bin.scrollWidth - bin.clientWidth,
          binDerecha: bin.getBoundingClientRect().right,
          discoIzquierda: disco.getBoundingClientRect().left,
        });
      }
      out.anchos[ancho] = filas;
      caja.remove();
    }
  } catch (e) { out.excepcion = String((e && e.stack) || e); }
  document.getElementById('__out').textContent = JSON.stringify(out);
});
</script>
"""


@unittest.skipUnless(chrome(), "Chrome/Chromium no disponible")
class TestLaTablaDeNivelesSeLee(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        semilla = ("<script>window.__SES = " + json.dumps(P.SESION_CMV40) + ";"
                   "window.__CAMBIOS = " + json.dumps(_CAMBIOS) + ";</script>\n")
        cls.m = pagina_en_chrome(semilla + _GUION, presupuesto_ms=60000)

    def _filas(self, ancho):
        self.assertNotIn("excepcion", self.m, self.m.get("excepcion"))
        filas = self.m["anchos"][str(ancho)]
        self.assertEqual(len(filas), 3, "L8, L2 y L3")
        return filas

    def test_que_aporta_no_queda_en_una_palabra_por_renglon(self):
        for ancho in (780, 620):
            for f in self._filas(ancho):
                self.assertGreaterEqual(f["papel"], 130, (ancho, f))

    def test_el_valor_del_bin_no_invade_tu_disco(self):
        for ancho in (780, 620):
            for f in self._filas(ancho):
                self.assertLessEqual(f["binDesborda"], 1, (ancho, f))
                self.assertLessEqual(f["binDerecha"], f["discoIzquierda"], (ancho, f))


if __name__ == "__main__":
    unittest.main()
