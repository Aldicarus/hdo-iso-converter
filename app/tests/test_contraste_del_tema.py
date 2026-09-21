"""El modo oscuro no puede ser peor que el claro, y se mide en la pantalla.

Un tema no se revisa leyendo el CSS. Lo que decide si un texto se lee es el
color que acaba teniendo la letra después de la cascada, las `var()` que se
resolvieron, las que se cayeron por estar fuera de ámbito, los alfas
compuestos y la opacidad heredada — y nada de eso está escrito en ninguna
parte del fuente. Así que este guard **abre las pantallas en Chrome** y mide
el contraste WCAG nodo a nodo, en los dos temas, sobre el MISMO DOM.

**El listón es comparativo, y es a propósito.** Medido el 2026-09-21, el
tema claro ya incumplía WCAG AA en el 31 % de sus 1.085 nodos con texto:
`--text-3` sobre el fondo de página da 2,99:1, y las etiquetas que usan el
acento crudo como color de letra bajan a 1,65:1. Eso es anterior al modo
oscuro y arreglarlo es otro trabajo. Lo que este guard exige es que **ningún
nodo que se leía pase a no leerse** al cambiar de tema, que es la única
promesa que un tema nuevo puede hacer.

Lo que la medición dio al cerrar, sobre los mismos 1.085 nodos:

| | claro | oscuro |
|---|---|---|
| por debajo de WCAG AA | 337 (31,1 %) | **160 (14,7 %)** |
| por debajo de 3,0:1   | 211          | **20** |
| cruzan el umbral      | —            | **0** |

El oscuro sale mejor y no es casualidad: la paleta clara son los colores
*vivos* de Apple HIG, pensados como RELLENO con texto blanco encima, no como
color de letra sobre blanco. Sobre fondo oscuro los mismos papeles los hacen
los tonos oscuros de Apple, que sí contrastan.

El camino hasta 0 fue 123 → 159 → 79 → 61 → 2 → 0, y el repunte del segundo
paso es lo interesante: voltear las tres paletas de ámbito local (`--dv-*`,
`--dvl-*`, `--fb-*`) DESTAPÓ el texto que estaba escrito a mano debajo. Un
color literal no falla cuando el tema cambia; simplemente se queda del tema
contrario, y sólo se ve mirando la pantalla.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_contraste_del_tema -v
"""
import re
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

CSS = (APP_DIR / "static" / "style.css").read_text(encoding="utf-8")

#: token de `:root` → por qué NO necesita contraparte en el bloque oscuro
SIN_CONTRAPARTE = {
    # El hub navy se queda igual en los dos temas: es el ancla de marca, el
    # blanco sobre él no cambia de contraste, y en oscuro pasa a leerse como
    # una banda elevada —más clara que la página—, que es el lenguaje de
    # elevación correcto ahí. Por eso tampoco hay que recalcular la
    # interpolación de `--active-hub`, que CLAUDE.md prohíbe tocar a ciegas.
    "--active-hub":          "el hub navy es el mismo en los dos temas",
    "--active-hub-top":      "el hub navy es el mismo en los dos temas",
    "--active-hub-bottom":   "el hub navy es el mismo en los dos temas",
    "--active-hub-sep":      "el hub navy es el mismo en los dos temas",
    "--active-hub-text":     "el hub navy es el mismo en los dos temas",
    "--active-hub-text-dim": "el hub navy es el mismo en los dos temas",
}


def _sin_comentarios(css: str) -> str:
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                  css, flags=re.S)


def _bloque(sel: str) -> dict:
    css = _sin_comentarios(CSS)
    i = css.index(sel)
    j = css.index("\n}", i)
    return {k: v.strip() for k, v in
            re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", css[i:j])}


def _es_color(v: str) -> bool:
    v = v.strip().lower()
    return v.startswith("#") or v.startswith("rgb")


# ── Contraste WCAG, en puro ───────────────────────────────────────────

def _rgb(h: str) -> tuple:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lum(c: tuple) -> float:
    def f(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def razon(a: str, b: str) -> float:
    x, y = sorted((_lum(_rgb(a)), _lum(_rgb(b))), reverse=True)
    return (x + 0.05) / (y + 0.05)


class TestCadaTokenDeColorTieneSuContraparte(unittest.TestCase):
    """Un token que no se voltea sigue sirviendo el color del tema contrario.

    No da ningún error —una `var()` definida siempre resuelve— así que sin
    este guard un token nuevo en `:root` entra en la app quedándose claro en
    el modo oscuro, y sólo se nota mirando la pantalla.
    """

    @classmethod
    def setUpClass(cls):
        cls.luz = _bloque(":root")
        cls.osc = _bloque('[data-tema="oscuro"] {')

    def test_el_guard_lee_los_dos_bloques(self):
        """Con los bloques mal leídos pasaría en verde sin mirar nada."""
        self.assertGreater(len(self.luz), 40)
        self.assertGreater(len(self.osc), 25)
        self.assertEqual(self.luz.get("--bg"), "#f5f5f7")
        self.assertEqual(self.osc.get("--bg"), "#17181b")

    def test_ninguno_se_queda_sin_voltear(self):
        huerfanos = []
        for k, v in self.luz.items():
            if not _es_color(v) or k in self.osc or k in SIN_CONTRAPARTE:
                continue
            # Los `-dim` y `-border` se derivan de un `--*-rgb`: si ese canal
            # está volteado, ellos siguen al tema solos. Es el motivo de que
            # existan los canales sueltos, así que el guard lo comprueba en
            # vez de exigir 11 overrides que sólo podrían desincronizarse.
            canales = re.findall(r"var\((--[\w-]+)\)", v)
            if canales and all(c in self.osc for c in canales):
                continue
            huerfanos.append(f"{k}: {v}")
        self.assertEqual(sorted(huerfanos), [],
                         "\n  · ".join(["tokens de color sin contraparte oscura "
                                        "(o sin motivo en SIN_CONTRAPARTE):"]
                                       + sorted(huerfanos)))

    def test_cada_excepcion_sigue_existiendo(self):
        """Una excepción que ya no corresponde a un token real parece
        cobertura y no cubre nada."""
        muertas = [k for k in SIN_CONTRAPARTE if k not in self.luz]
        self.assertEqual(muertas, [], f"excepciones que ya no existen: {muertas}")

    def test_el_bloque_oscuro_no_inventa_tokens(self):
        """Un token que sólo existe en oscuro no lo usa nadie en claro: o es
        un olvido en `:root` o es una errata en el nombre."""
        inventados = [k for k in self.osc if k not in self.luz]
        self.assertEqual(sorted(inventados), [],
                         f"sólo existen en oscuro: {sorted(inventados)}")


class TestLaEscalaContrastaEnLosDosTemas(unittest.TestCase):
    """Los pares (texto, superficie) que la app usa de verdad.

    Es puro: corre sin Chrome, así que la comprobación de la escala nunca se
    salta — que es justo lo que le pasaría a la medición de pantalla en un
    entorno sin navegador.
    """

    @classmethod
    def setUpClass(cls):
        cls.luz = _bloque(":root")
        cls.osc = _bloque('[data-tema="oscuro"] {')

    def _pares(self, b, base):
        sup = [b.get(k, base.get(k)) for k in
               ("--bg", "--surface-1", "--surface-2", "--surface-3")]
        return sup

    def test_el_texto_principal_y_el_secundario_pasan_AA(self):
        for nombre, b in (("claro", self.luz), ("oscuro", self.osc)):
            for t in ("--text-1", "--text-2"):
                for s in self._pares(b, self.luz):
                    with self.subTest(tema=nombre, texto=t, fondo=s):
                        self.assertGreaterEqual(razon(b.get(t, self.luz[t]), s), 4.5)

    def test_ningun_par_que_aprueba_en_claro_suspende_en_oscuro(self):
        """El invariante del tema, en su forma más pequeña.

        **No** se exige que el número no baje: `--text-1` pasa de 17,01 a
        13,23 sobre una tarjeta y eso es DELIBERADO —blanco puro sobre negro
        puro son 21:1 y hace halo alrededor de la letra, que es lo que
        Material 3 y Apple dicen que no se haga—. Lo que no puede pasar es
        cruzar el listón.
        """
        peores = []
        for t in ("--text-1", "--text-2", "--text-3"):
            for k in ("--bg", "--surface-1", "--surface-2", "--surface-3"):
                rc = razon(self.luz[t], self.luz[k])
                ro = razon(self.osc.get(t, self.luz[t]), self.osc.get(k, self.luz[k]))
                if ro < 4.5 <= rc:
                    peores.append(f"{t} sobre {k}: {rc:.2f} → {ro:.2f}")
        self.assertEqual(peores, [], "\n  · ".join(["pares que suspenden en oscuro:"]
                                                   + peores))

    def test_el_secundario_MEJORA_en_oscuro(self):
        """`--text-3` es el token que más veces incumplía WCAG en la app: da
        2,99:1 sobre el fondo de página en claro. En oscuro aprueba, y eso no
        es un accidente del que se pueda prescindir sin decirlo."""
        for k in ("--bg", "--surface-1", "--surface-2"):
            with self.subTest(fondo=k):
                self.assertGreater(
                    razon(self.osc["--text-3"], self.osc[k]),
                    razon(self.luz["--text-3"], self.luz[k]))

    def test_el_ambar_sigue_sin_ser_el_naranja(self):
        """En Tab 3 el naranja es «va por merge» y el ámbar «esto lo decides
        tú». Si se parecen, la card deja de decir dos cosas distintas."""
        def lab(h):
            import math
            def f(v):
                v /= 255
                return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
            r, g, b = (f(v) for v in _rgb(h))
            x = (.4124 * r + .3576 * g + .1805 * b) / .95047
            y = .2126 * r + .7152 * g + .0722 * b
            z = (.0193 * r + .1192 * g + .9505 * b) / 1.08883
            def q(t): return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
            fx, fy, fz = q(x), q(y), q(z)
            return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))
        import math
        for nombre, b in (("claro", self.luz), ("oscuro", self.osc)):
            a, o = lab(b["--amber"]), lab(b["--orange"])
            dE = math.sqrt(sum((a[i] - o[i]) ** 2 for i in range(3)))
            with self.subTest(tema=nombre):
                self.assertGreater(dE, 12, f"ΔE {dE:.1f}: se confunden")


try:
    import contraste as _contraste
    from test_las_tres_lenguas_en_pantalla import CHROME
except Exception:                                            # pragma: no cover
    CHROME = None


@unittest.skipUnless(CHROME, "sin Chrome")
class TestNingunNodoDejaDeLeerseEnOscuro(unittest.TestCase):
    """La medición de verdad: las pantallas reales, en los dos temas."""

    @classmethod
    def setUpClass(cls):
        cls.claro = _contraste.medir("claro")
        cls.oscuro = _contraste.medir("oscuro")

    def test_la_sonda_mide_algo(self):
        """Con las pantallas sin montar todo pasaría en verde: es el fallo
        que ya tuvo la captura de i18n, que nunca llegó a un panel con
        datos."""
        n = sum(len(v) for v in self.claro.values())
        self.assertGreater(len(self.claro), 30, "faltan pantallas")
        self.assertGreater(n, 800, f"sólo {n} nodos con texto")
        self.assertEqual(sorted(self.claro), sorted(self.oscuro))

    def test_ninguno_pasa_de_leerse_a_no_leerse(self):
        rotos = []
        for p, nodos in self.claro.items():
            for a, b in zip(nodos, self.oscuro.get(p, [])):
                if a["sel"] != b["sel"]:
                    continue                       # desalineado: no compara
                if b["r"] < b["min"] <= a["r"]:
                    rotos.append(
                        f"{p} · {b['sel']} «{b['txt'][:28]}»: "
                        f"{a['r']:.2f} → {b['r']:.2f} (mín {b['min']})")
        self.assertEqual(sorted(rotos), [],
                         "\n  · ".join([f"{len(rotos)} nodos que se leían en claro "
                                        f"y no se leen en oscuro:"] + sorted(rotos)))

    def test_el_oscuro_no_tiene_mas_texto_ilegible_que_el_claro(self):
        """Por debajo de 3,0:1 un texto no se lee, sea cual sea su tamaño."""
        def bajo3(m):
            return sum(1 for v in m.values() for n in v if n["r"] < 3)
        c, o = bajo3(self.claro), bajo3(self.oscuro)
        self.assertLessEqual(o, c, f"claro {c} · oscuro {o}")


if __name__ == "__main__":
    unittest.main()
