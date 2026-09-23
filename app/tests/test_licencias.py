"""La imagen distribuye binarios de terceros: sus avisos tienen que viajar.

La app **invoca** ffmpeg, mkvmerge y dovi_tool como procesos separados, así
que su copyleft no alcanza al código —eso está razonado en
`THIRD-PARTY-NOTICES.md`—, pero publicar la imagen en GHCR sí es distribuir
esos binarios, y eso obliga a acompañarlos de sus avisos y de una oferta de
código fuente.

Nada de eso da error si falta. Una imagen sin avisos arranca igual, pasa el
healthcheck y sirve la aplicación entera: el incumplimiento es invisible
desde dentro. De ahí estos guards.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_licencias -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
APP = RAIZ / "app"
LICENSE = RAIZ / "LICENSE"
AVISOS = RAIZ / "THIRD-PARTY-NOTICES.md"
README = RAIZ / "README.md"
DOCKERFILE = RAIZ / "docker" / "Dockerfile"
LICENCIAS_DIR = APP / "static" / "licenses"

# Qué componente aporta cada binario. La CLAVE se descubre sola (ver
# `_binarios_invocados`); esto solo dice bajo qué nombre buscarlo en los
# avisos cuando el del binario no aparece tal cual.
PROVEEDOR = {
    "ffmpeg": "FFmpeg",
    "ffprobe": "FFmpeg",
    "mkvmerge": "MKVToolNix",
    "mkvpropedit": "MKVToolNix",
    "mkvextract": "MKVToolNix",
    "mediainfo": "MediaInfo",
    "dovi_tool": "dovi_tool",
}

_BIN_CONST = re.compile(r"^[A-Z0-9_]*BIN\s*=\s*[\"']([a-z0-9_]+)[\"']", re.MULTILINE)


def _binarios_invocados() -> set[str]:
    """Los binarios externos que el código llama, LEÍDOS DEL CÓDIGO.

    Una lista escrita a mano se queda corta en cuanto alguien añada una
    herramienta, y el guard seguiría en verde vigilando las de ayer. El
    patrón del repo es `MKVMERGE_BIN = "mkvmerge"`, así que la fuente de
    verdad ya existe.
    """
    encontrados: set[str] = set()
    for py in APP.rglob("*.py"):
        if "/tests/" in str(py):
            continue
        encontrados.update(_BIN_CONST.findall(py.read_text(encoding="utf-8")))
    return encontrados


class TestElRepoDeclaraSuLicencia(unittest.TestCase):
    """El README decía «Licencia: MIT» y no había fichero. MIT sin el texto
    ni la línea de copyright no es una concesión: es una intención."""

    def test_existe_el_license_con_su_copyright(self):
        self.assertTrue(LICENSE.is_file(), "falta LICENSE en la raíz")
        t = LICENSE.read_text(encoding="utf-8")
        self.assertIn("MIT License", t)
        self.assertIn("Copyright (c) 2026 Aldicarus", t)
        self.assertIn("THE SOFTWARE IS PROVIDED \"AS IS\"", t)

    def test_el_readme_cuenta_lo_mismo(self):
        t = README.read_text(encoding="utf-8")
        self.assertIn("(LICENSE)", t, "el README no enlaza el LICENSE")
        self.assertIn("THIRD-PARTY-NOTICES.md", t)
        self.assertIn("MIT", t)


class TestCadaHerramientaQueSeInvocaTieneAviso(unittest.TestCase):

    def test_ninguna_se_queda_fuera(self):
        avisos = AVISOS.read_text(encoding="utf-8").lower()
        faltan = []
        for binario in sorted(_binarios_invocados()):
            proveedor = PROVEEDOR.get(binario, binario)
            if binario not in avisos and proveedor.lower() not in avisos:
                faltan.append(f"{binario} (lo aporta {proveedor})")
        self.assertEqual(faltan, [], (
            "\nel código invoca binarios que no están en THIRD-PARTY-NOTICES.md:"
            "\n  · " + "\n  · ".join(faltan)))

    def test_la_tabla_de_proveedores_no_se_queda_vieja(self):
        """Una entrada que ya no corresponde a nada parece cobertura."""
        invocados = _binarios_invocados()
        sobran = sorted(set(PROVEEDOR) - invocados)
        self.assertEqual(sobran, [], (
            f"\nPROVEEDOR cita binarios que el código ya no invoca: {sobran}"))


class TestLasVersionesDelDockerfileEstanEnLosAvisos(unittest.TestCase):
    """Subir `DOVI_TOOL_VERSION` y dejar los avisos en la versión vieja es
    el fallo clásico de una lista escrita dos veces. La fuente que hay que
    poder entregar es la del binario que se distribuye, no la de otro día."""

    @classmethod
    def setUpClass(cls):
        cls.docker = DOCKERFILE.read_text(encoding="utf-8")
        cls.avisos = AVISOS.read_text(encoding="utf-8")

    def _arg(self, nombre: str) -> str:
        m = re.search(rf"^ARG {nombre}=(\S+)", self.docker, re.MULTILINE)
        self.assertIsNotNone(m, f"no está ARG {nombre} en el Dockerfile")
        return m.group(1)

    def test_la_version_de_dovi_tool(self):
        self.assertIn(self._arg("DOVI_TOOL_VERSION"), self.avisos)

    def test_el_build_exacto_de_ffmpeg(self):
        build = self._arg("FFMPEG_BUILD")
        # `ffmpeg-n7.1.5-12-g1fdbca85aa-linux64-gpl-7.1` → el commit es lo
        # que identifica la fuente de forma permanente; el nombre del asset
        # de BtbN caduca (borran los autobuilds que no son de fin de mes).
        m = re.search(r"-(g[0-9a-f]{7,})-", build)
        self.assertIsNotNone(m, f"no se reconoce el commit en {build}")
        commit = m.group(1).lstrip("g")
        self.assertIn(commit, self.avisos, (
            f"\nel Dockerfile construye con ffmpeg @ {commit} y los avisos "
            f"no lo mencionan: la oferta de fuentes apuntaría a otra cosa"))

    def test_la_base_de_ubuntu(self):
        m = re.search(r"^FROM ubuntu:(\S+)", self.docker, re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertIn(m.group(1), self.avisos)

    def test_el_variant_declarado_coincide_con_el_que_se_baja(self):
        """El ffmpeg de BtbN es GPLv3 en el variant `gpl` y LGPLv3 en el
        `lgpl` (sus `variants/defaults-*.sh`: el primero añade
        `--enable-gpl` y apunta a COPYING.GPLv3, el segundo no). Cambiar el
        ARG sin tocar los avisos haría que el documento anunciara una
        licencia que el binario no tiene.

        Se mira LA FILA de ffmpeg, no el documento entero: «LGPL-3.0»
        aparece de todos modos en la lista de textos completos y en la
        oferta escrita, así que un `assertIn` global pasa con cualquiera de
        los dos variants y no comprueba nada. Lo destapó una mutación.
        """
        build = self._arg("FFMPEG_BUILD")
        es_gpl = "-gpl" in build and "-lgpl" not in build

        # Ancla estrecha: la FILA DEL COMPONENTE de la tabla, no cualquier
        # línea que nombre a BtbN — el espejo de las fuentes lo menciona
        # también y el ancla dejó de ser única en cuanto se añadió.
        filas = [l for l in self.avisos.splitlines()
                 if l.startswith("| **FFmpeg**") and "BtbN" in l]
        self.assertEqual(len(filas), 1, (
            "\nno se identifica la fila del build de BtbN en la tabla de "
            "componentes"))
        fila = filas[0]

        if es_gpl:
            self.assertIn("GPL-3.0-or-later", fila)
            self.assertNotIn("LGPL", fila, (
                "\nel Dockerfile baja el variant `gpl` (GPLv3) y la fila "
                "dice LGPL"))
            self.assertIn("--enable-gpl", fila)
        else:
            self.assertIn("LGPL-3.0", fila, (
                "\nel Dockerfile baja el variant `lgpl` y la fila sigue "
                "anunciando la GPL completa"))
            self.assertNotIn("--enable-gpl ", fila)


class TestLasDependenciasPythonTienenAviso(unittest.TestCase):

    def test_las_directas_estan_en_el_apendice(self):
        req = (APP / "requirements.txt").read_text(encoding="utf-8")
        paquetes = [
            re.split(r"[=<>\[]", l.strip())[0]
            for l in req.splitlines() if l.strip() and not l.startswith("#")
        ]
        self.assertTrue(paquetes, "requirements.txt vacío?")
        apendice = (LICENCIAS_DIR / "PYTHON-DEPENDENCIES.md").read_text(
            encoding="utf-8").lower()
        faltan = [p for p in paquetes if p.lower() not in apendice]
        self.assertEqual(faltan, [], (
            f"\nno están en el apéndice de licencias: {faltan}"
            "\nregenéralo con app/tools/generar_avisos_python.py"))


class TestLosTextosDeLicenciaEstan(unittest.TestCase):
    """La GPL exige que su texto acompañe al binario. Enlazar a gnu.org no
    vale: el usuario puede no tener red, y la imagen es el objeto."""

    def test_los_cuatro_textos_completos(self):
        for nombre in ("GPL-2.0.txt", "GPL-3.0.txt", "LGPL-3.0.txt",
                       "Apache-2.0.txt"):
            with self.subTest(licencia=nombre):
                f = LICENCIAS_DIR / nombre
                self.assertTrue(f.is_file(), f"falta {nombre}")
                self.assertGreater(f.stat().st_size, 5000,
                                   f"{nombre} parece truncado")

    def test_el_gpl3_es_el_de_verdad(self):
        t = (LICENCIAS_DIR / "GPL-3.0.txt").read_text(encoding="utf-8")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", t)
        self.assertIn("Version 3, 29 June 2007", t)


class TestLosAvisosViajanEnLaImagen(unittest.TestCase):
    """El objeto que se distribuye es la imagen, no el repositorio. Un
    THIRD-PARTY-NOTICES.md que solo esté en GitHub no acompaña a nada."""

    @classmethod
    def setUpClass(cls):
        cls.docker = DOCKERFILE.read_text(encoding="utf-8")

    def test_se_copian_dentro(self):
        self.assertRegex(
            self.docker, r"COPY\s+LICENSE\s+THIRD-PARTY-NOTICES\.md",
            "el Dockerfile no mete el LICENSE ni los avisos en la imagen")

    def test_el_inventario_exacto_se_genera_en_el_build(self):
        """Los paquetes de apt NO están fijados: cada build puede traer otra
        versión, y la fuente que hay que poder entregar es la de ESA."""
        self.assertIn("dpkg-query -W", self.docker)
        self.assertIn("INSTALLED-PACKAGES.txt", self.docker)
        self.assertIn("generar_avisos_python.py", self.docker)

    def test_las_etiquetas_oci_dicen_la_verdad(self):
        """La imagen no es solo MIT: lleva binarios GPL. Un escáner de
        cumplimiento lee esta etiqueta."""
        m = re.search(r'org\.opencontainers\.image\.licenses="([^"]+)"',
                      self.docker)
        self.assertIsNotNone(m, "falta la etiqueta OCI de licencias")
        spdx = m.group(1)
        for esperado in ("MIT", "GPL-2.0", "GPL-3.0-or-later"):
            self.assertIn(esperado, spdx)

    def test_el_generador_vive_donde_el_dockerfile_lo_busca(self):
        self.assertTrue((APP / "tools" / "generar_avisos_python.py").is_file())


class TestLaReleaseDeFuentesNoRepublicaLaImagen(unittest.TestCase):
    """Los avisos prometen las fuentes en una release `sources-*`, y
    publicarla no puede tocar la imagen.

    `publish-docker.yml` dispara con `release: [published]` sin filtrar el
    tag, y `metadata-action` añade SIEMPRE `type=raw,value=latest`: sin el
    filtro, crear la release de fuentes reconstruye la imagen y reescribe
    `latest` con un tag que no es una versión. Y termina en verde, así que
    no hay señal de que haya pasado.
    """

    @classmethod
    def setUpClass(cls):
        cls.wf = (RAIZ / ".github" / "workflows"
                  / "publish-docker.yml").read_text(encoding="utf-8")

    def test_el_job_solo_corre_con_tags_de_version(self):
        self.assertIn("startsWith(github.event.release.tag_name, 'v')",
                      self.wf, (
            "\nel workflow publicaría imagen con CUALQUIER release, "
            "incluida la de fuentes GPL: reescribiría `latest`"))

    def test_y_el_disparo_a_mano_sigue_funcionando(self):
        """El filtro no puede cargarse `workflow_dispatch`, que es la
        salida cuando una release falla al publicar (pasó con v2.8.0)."""
        self.assertIn("github.event_name == 'workflow_dispatch'", self.wf)


class TestLaOfertaDeFuentesEstaCompleta(unittest.TestCase):
    """La GPLv2 §3(b) no admite «apunta a un tercero»: pide una oferta
    escrita válida tres años y extensible a CUALQUIER tercero."""

    @classmethod
    def setUpClass(cls):
        # El documento va con la prosa envuelta a 76 columnas, así que una
        # frase cae partida por un salto y un `assertIn` crudo no la ve. Se
        # busca sobre el texto con los espacios normalizados — la misma
        # equivalencia que aplica el golden del castellano.
        cls.t = " ".join(AVISOS.read_text(encoding="utf-8").split())

    def test_dice_tres_anos_y_a_cualquiera(self):
        self.assertIn("three (3) years", self.t)
        self.assertIn("any third party", self.t)

    def test_trae_un_contacto_alcanzable(self):
        self.assertIn("github.com/Aldicarus/hdo-iso-converter/issues", self.t)

    def test_avisa_de_que_los_autobuilds_de_btbn_caducan(self):
        """Es el modo de fallo propio de este proyecto y ya mordió una vez:
        una oferta que apunta a un asset borrado deja de ser una oferta."""
        self.assertIn("se borran", self.t.lower())


if __name__ == "__main__":
    unittest.main()
