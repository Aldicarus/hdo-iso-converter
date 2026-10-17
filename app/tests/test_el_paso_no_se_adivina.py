# -*- coding: utf-8 -*-
"""Nadie detecta un paso ni una fase buscando prosa en el log.

Es la clase de bug más silenciosa de la traducción, y salió DOS veces el
2026-09-17:

  * `routers/tab1.py` mapeaba once subcadenas castellanas —«paso 1/4»,
    «identificando mpls», «contando paquetes pgs»— contra el log que
    escribe `phase_a`. Desde que el log se traduce, con la app en catalán
    `phase_a` escribe «Pas 1/4» y **el modal «Analizando disco» se quedaba
    en el primer paso**: sin error, sin log, sin nada. Estaba vivo en
    producción.
  * `static/tab1.js` hacía lo mismo con `[Origen] ✓ ISO desmontado` para
    la píldora de la sub-pestaña. Ahí no se notaba porque las tres ramas
    llamaban a la misma función y `[Fase D]` sí casaba, pero el código
    estaba muerto.

Lo que SÍ se puede comparar es un MARCADOR, porque el servidor lo
concatena en el código —fuera de la cadena traducible— y llega igual en
los tres idiomas: `[Fase X]`, `━━━`, `📋 Plan`, `🎯 Resultado`,
`§§PROGRESS§§`. El paso, en cambio, lo anuncia quien lo ejecuta:
`analysis_progress.fijar(step=…)`.
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR / "tests") not in sys.path:
    sys.path.insert(0, str(APP_DIR / "tests"))
import captura_castellano as captura  # noqa: E402
from frontend_sources import rutas  # noqa: E402

# Comparar contra ESTO es correcto: son tokens del código, no prosa.
# La lista vive en `captura_castellano` porque la usan los DOS guards: este
# necesita saber qué no es prosa, y el de castellano suelto necesita lo mismo
# para no señalar `[Fase A]` desde que «fase» entró en el vocabulario.
MARCADORES = captura.MARCADORES

# El literal contra el que se compara, en un `in`/`startswith`/`includes`.
_COMPARA_PY = re.compile(
    r"""(?:['"]((?:[^'"\\\n]|\\.)+)['"]\s+in\b"""
    r"""|\.startswith\(\s*['"]((?:[^'"\\\n]|\\.)+)['"])""")
_COMPARA_JS = re.compile(
    r"""\.(?:includes|startsWith|indexOf)\(\s*['"]((?:[^'"\\\n]|\\.)+)['"]""")

# Una expresión regular con la que se PRUEBA una línea: `/…/.test(x)`,
# `x.match(/…/)`. El guard miraba solo `.includes('…')`, así que
# `/✗ Fase \w+ FALLÓ/.test(ev.data)` —el cierre de fase que el WS de CMv4.0
# leía en castellano y solo en castellano— se le escapaba por construcción.
_REGEX = r"/((?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\[\n])+)/[a-z]*"
_REGEX_JS = re.compile(
    r"(?<![\w)\]$])" + _REGEX + r"\s*\.(?:test|exec)\("
    + r"|\.(?:match|search|replace|split)\(\s*" + _REGEX)


def _clase(m: "re.Match") -> str:
    """Una clase de símbolos (`[✓✗]`) vale su primer símbolo, para que el
    marcador que forma siga reconociéndose; cualquier otra, nada."""
    dentro = m.group(0)[1:-1]
    if dentro and not re.search(r"[\w\\-]", dentro):
        return dentro[0]
    return " "


def _texto_de_la_regex(rx: str) -> str:
    r"""Lo que una regex exige LITERALMENTE: fuera los escapes de clase
    (`\w`, `\s`…), las clases `[…]` y los metacaracteres. Una alternativa
    detrás de un `\[` es una etiqueta de log (`\[(?:Fase|Pipeline)`) y se
    deja con su corchete delante de cada opción."""
    rx = re.sub(r"\\\[\(\?:([^()]*)\)",
                lambda m: " ".join("[" + o for o in m.group(1).split("|")), rx)
    rx = re.sub(r"\\[wWsSdDbB]", " ", rx)
    rx = re.sub(r"(?<!\\)\[(?:\\.|[^\]\\])*\]", _clase, rx)
    rx = re.sub(r"\\(.)", r"\1", rx)
    rx = re.sub(r"[()?:*+|^${}]", " ", rx)
    return " ".join(rx.split())


def _regex_con_prosa(rx: str) -> set[str]:
    r"""Las palabras castellanas que la regex exige, quitados los marcadores,
    las etiquetas de log (`[Algo`) y los identificadores (`detalle-vacio`)."""
    texto = _texto_de_la_regex(rx)
    for m in sorted(MARCADORES, key=len, reverse=True):
        texto = texto.replace(m, " ")
    texto = re.sub(r"\[[^\s\]]+", " ", texto)
    texto = re.sub(r"\w+(?:[-_]\w+)+", " ", texto)
    palabras = {w.lower() for w in re.findall(r"[A-Za-zÁÉÍÓÚáéíóúñÑüÜ]{4,}", texto)}
    return palabras & captura.vocabulario_solo_castellano()


def _es_prosa(s: str) -> bool:
    """¿Es castellano que el catálogo traduce, y no un marcador?"""
    t = " ".join(s.split())
    if not t or any(m in t for m in MARCADORES):
        return False
    return captura.es_frase(t) or captura.es_rotulo(t)


class TestNadieAdivinaElPasoLeyendoProsa(unittest.TestCase):

    def test_el_servidor_no_compara_contra_prosa_castellana(self):
        malos = []
        for f in sorted(APP_DIR.rglob("*.py")):
            if "tests" in f.parts or "__pycache__" in str(f):
                continue
            src = f.read_text(encoding="utf-8")
            try:
                arbol = ast.parse(src)
            except SyntaxError:
                continue
            exentos = captura._exentos_del_modulo(arbol)
            textos = {n.value for n in ast.walk(arbol)
                      if isinstance(n, ast.Constant)
                      and isinstance(n.value, str) and id(n) not in exentos}
            for i, linea in enumerate(src.splitlines(), 1):
                if linea.lstrip().startswith("#"):
                    continue
                for m in _COMPARA_PY.finditer(linea):
                    s = m.group(1) or m.group(2)
                    if s in textos and _es_prosa(s):
                        malos.append(f"{f.relative_to(APP_DIR.parent)}:{i}: "
                                     f"{s[:50]!r}")
        self.assertEqual(malos, [], (
            f"\n{len(malos)} comparación(es) contra prosa traducible. El log "
            f"llega en el idioma de la app, así que esto deja de casar sin "
            f"dar ningún error:\n  · " + "\n  · ".join(malos[:12])))

    def test_el_frontend_no_compara_contra_prosa_castellana(self):
        malos = []
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            for i, linea in enumerate(src.splitlines(), 1):
                if linea.lstrip().startswith(("//", "*", "/*")):
                    continue
                for m in _COMPARA_JS.finditer(linea):
                    if _es_prosa(m.group(1)):
                        malos.append(f"{Path(r).name}:{i}: "
                                     f"{m.group(1)[:50]!r}")
        self.assertEqual(malos, [], (
            f"\n{len(malos)} comparación(es) del JS contra prosa traducible:"
            f"\n  · " + "\n  · ".join(malos[:12])))

    def test_ninguna_regex_del_frontend_lee_prosa(self):
        malos, vistas = [], 0
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            for i, linea in enumerate(src.splitlines(), 1):
                if linea.lstrip().startswith(("//", "*", "/*")):
                    continue
                for m in _REGEX_JS.finditer(linea):
                    vistas += 1
                    palabras = _regex_con_prosa(m.group(1) or m.group(2))
                    if palabras:
                        malos.append(f"{Path(r).name}:{i}: {sorted(palabras)}")
        # Trinquete: medido el 2026-10-17, unas ochenta regex de prueba.
        self.assertGreater(vistas, 40, "el extractor de regex no ve nada")
        self.assertEqual(malos, [], (
            f"\n{len(malos)} regex del JS que exigen prosa traducible:"
            f"\n  · " + "\n  · ".join(malos[:12])))

    def test_el_extractor_de_regex_ve_las_dos_formas(self):
        linea = ("if (/✗ Fase \\w+ FALLÓ/.test(ev.data) || "
                 "ev.data.match(/━━━ Inicio fase:\\s*([a-z_]+)/i)) {}")
        rx = [m.group(1) or m.group(2) for m in _REGEX_JS.finditer(linea)]
        self.assertEqual(len(rx), 2, rx)
        self.assertEqual(_regex_con_prosa(rx[0]), {"falló"})
        self.assertIn("inicio", _regex_con_prosa(rx[1]))
        self.assertEqual(_regex_con_prosa(r"^\[(?:Fase|Montando|Pipeline)"), set())
        self.assertEqual(_regex_con_prosa("detalle-vacio"), set())
        # El marcador solo no es prosa: es lo que SÍ se puede comparar.
        self.assertEqual(_regex_con_prosa(r"(?:^|\] )[✓✗] Fase \S"), set())


if __name__ == "__main__":
    unittest.main()
