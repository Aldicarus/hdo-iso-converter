"""`git describe` solo puede anclarse en un tag de RELEASE.

Un tag auxiliar —`pre-i18n`, el punto de retorno de una rama— es lo más
cercano para `git describe --tags`, así que la app pasa a reportar
`pre-i18n-8-g811d7fe` como versión. Y eso no es cosmético: `_semver_tuple`
no puede parsearlo, lo trata como inválido —«menor que cualquier tag»— y
`check-updates` anuncia una actualización a la última release **estando por
delante de ella**. Visto en el NAS el 2026-09-15: el pill de la cabecera
invitaba a bajar de v2.8.1 a v2.8.1.

El arreglo es `--match "v*"` en los dos sitios que resuelven la versión (el
stage `version-detector` del Dockerfile y el fallback de dev local). Este
guard existe porque **el síntoma aparece en el NAS, no en la suite**: la
versión se resuelve al construir la imagen, y hasta que alguien crea un tag
que no empiece por `v` no hay nada que falle.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_version_solo_tags_de_release -v
"""
import re
import subprocess
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]


class TestLosDosSitiosFiltranPorVersion(unittest.TestCase):

    def test_el_dockerfile_solo_mira_los_tags_de_release(self):
        src = (RAIZ / "docker" / "Dockerfile").read_text(encoding="utf-8")
        # Los comentarios también dicen «git describe», y hablan de esto.
        describes = [l for l in src.splitlines()
                     if "git describe" in l and not l.lstrip().startswith("#")]
        self.assertTrue(describes, "el stage version-detector ha desaparecido")
        for l in describes:
            self.assertIn('--match "v*"', l, (
                f"\n`git describe` sin filtro de tag:\n  {l.strip()}\n"
                "Un tag auxiliar se convierte en la versión que reporta la app."))

    def test_el_fallback_de_dev_local_tambien(self):
        src = (RAIZ / "app" / "main.py").read_text(encoding="utf-8")
        i = src.index('"git", "describe"')
        # la llamada entera, hasta el cierre de la lista de argumentos
        llamada = src[i:src.index("]", i)]
        self.assertIn('"--match", "v*"', llamada, (
            f"\n`git describe` sin filtro en el fallback de dev:\n  {llamada}"))


class TestElComportamientoConEsteRepo(unittest.TestCase):
    """Sobre el repo de verdad, que es donde el tag existe."""

    def _describe(self, *extra: str) -> str:
        r = subprocess.run(["git", "describe", "--tags", *extra, "--always"],
                           cwd=RAIZ, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            self.skipTest("git no disponible o repo sin tags")
        return r.stdout.strip()

    def test_con_el_filtro_la_version_es_parseable_como_semver(self):
        v = self._describe("--match", "v*")
        self.assertRegex(v, r"^v\d+\.\d+\.\d+", (
            f"\n`git describe --match v*` da {v!r}, que no empieza por un "
            "tag de versión"))

    def test_sin_el_filtro_este_repo_daria_una_version_no_parseable(self):
        """El contraste que justifica el filtro. Si algún día no hay ningún
        tag auxiliar, este test se salta en vez de fallar: lo que se afirma
        es que el filtro HACE algo cuando hay uno."""
        sin = self._describe()
        if re.match(r"^v\d+\.\d+\.\d+", sin):
            self.skipTest("ahora mismo no hay ningún tag auxiliar por delante")
        self.assertNotRegex(sin, r"^v\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
