"""El idioma de la app decide con qué pistas nace un proyecto.

Hasta el 2026-09-17 el perfil `filtered` estaba clavado a Castellano, así que
un usuario en inglés tenía que quitar a mano el audio castellano de cada
disco, y uno en catalán no podía pedir su lengua. **Esto no es traducción:
cambia el contenido del MKV**, así que lleva sus propios tests.

Las seis decisiones se cerraron con el usuario el 2026-09-15 y están escritas
en `phase_b.IDIOMAS_DEL_PERFIL`. Lo que aquí se fija:

  · **en castellano NO cambia nada** — es la garantía que hace seguro el
    cambio, y la que sostienen además los 41 discos reales de
    `test_golden_discos_reales`;
  · el VO entra siempre, sea cual sea el idioma de la app;
  · el catalán conserva también el castellano;
  · el `flag_default` y el `flag_forced` del contenedor van al primer
    preferido que el disco traiga;
  · sin doblaje en el idioma preferido, el subtítulo por defecto pasa del
    forzado al COMPLETO;
  · el `(DCP 9.1.6)` se queda en la castellana pase lo que pase.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_perfil_de_idioma_de_pistas -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import i18n  # noqa: E402
from models import RawAudioTrack, RawSubtitleTrack  # noqa: E402
from phases.phase_b import (  # noqa: E402
    _select_audio_tracks,
    _select_subtitle_tracks,
    idiomas_preferidos,
)


def idioma(cual: str):
    return mock.patch.object(i18n, "idioma_activo", lambda: cual)


def audio(lang: str, codec: str = "Dolby Digital Audio", ch: str = "5.1",
          kbps: int = 640) -> RawAudioTrack:
    return RawAudioTrack(language=lang, codec=codec, description=ch,
                         bitrate_kbps=kbps)


def sub(lang: str, paquetes: int) -> RawSubtitleTrack:
    return RawSubtitleTrack(language=lang, codec="PGS",
                            description="1920x1080",
                            packet_count=paquetes, bitrate_kbps=10.0)


# Un disco corriente: VO inglés, doblaje castellano, francés y catalán.
DISCO_AUDIO = [audio("English", "Dolby TrueHD/Atmos Audio", "7.1", 4500),
               audio("Spanish", "DTS-HD Master Audio", "5.1", 3000),
               audio("French"), audio("Catalan", "DTS Audio")]
# Subtítulos: completo + forzado por idioma.
DISCO_SUBS = [sub("English", 6000), sub("Spanish", 5000), sub("Catalan", 4000),
              sub("French", 4800),
              sub("English", 900), sub("Spanish", 800), sub("Catalan", 700),
              sub("French", 750)]


def _langs(pistas) -> list[str]:
    return [p.raw.language for p in pistas]


class TestElPerfilPorIdioma(unittest.TestCase):

    def test_la_tabla_es_una_lista_ordenada(self):
        self.assertEqual(idiomas_preferidos("es"), ("spanish",))
        self.assertEqual(idiomas_preferidos("en"), ("english",))
        self.assertEqual(idiomas_preferidos("ca"), ("catalan", "spanish"))

    def test_un_idioma_desconocido_cae_al_castellano(self):
        """Es el comportamiento de siempre, y el que no rompe nada."""
        self.assertEqual(idiomas_preferidos("de"), ("spanish",))
        self.assertEqual(idiomas_preferidos(""), ("spanish",))


class TestAudio(unittest.TestCase):

    def _inc(self, cual: str):
        with idioma(cual):
            inc, _ = _select_audio_tracks(DISCO_AUDIO, "English", False)
        return inc

    def test_en_castellano_sale_lo_de_siempre(self):
        """Castellano primero y el VO detrás. Es la garantía del cambio."""
        inc = self._inc("es")
        self.assertEqual(_langs(inc), ["Spanish", "English"])
        self.assertEqual([p.flag_default for p in inc], [True, False])

    def test_en_ingles_no_entra_el_castellano(self):
        """Era la queja: con la app en inglés había que quitarlo a mano."""
        inc = self._inc("en")
        self.assertEqual(_langs(inc), ["English"])
        self.assertTrue(inc[0].flag_default)

    def test_en_catalan_entran_catalan_castellano_y_el_vo(self):
        inc = self._inc("ca")
        self.assertEqual(_langs(inc), ["Catalan", "Spanish", "English"])
        # El default va al PRIMER preferido que el disco trae.
        self.assertEqual([p.flag_default for p in inc], [True, False, False])

    def test_el_frances_se_descarta_en_los_tres(self):
        for cual in ("es", "en", "ca"):
            with self.subTest(idioma=cual):
                self.assertNotIn("French", _langs(self._inc(cual)))

    def test_sin_pista_del_idioma_preferido_queda_el_vo(self):
        """Un disco sin catalán con la app en catalán: el VO y el castellano,
        y el default se va al primer preferido que SÍ está."""
        with idioma("ca"):
            inc, _ = _select_audio_tracks(
                [audio("English", "Dolby TrueHD/Atmos Audio", "7.1", 4500),
                 audio("Spanish")], "English", False)
        self.assertEqual(_langs(inc), ["Spanish", "English"])
        self.assertEqual([p.flag_default for p in inc], [True, False])

    def test_el_dcp_se_queda_en_la_castellana(self):
        """Es una propiedad de esa mezcla, no del idioma de la interfaz."""
        with idioma("en"):
            inc, _ = _select_audio_tracks(
                [audio("Spanish", "Dolby TrueHD/Atmos Audio", "7.1", 4500),
                 audio("English", "Dolby TrueHD/Atmos Audio", "7.1", 4500)],
                "Spanish", True)
        etiquetas = {p.raw.language: p.label for p in inc}
        self.assertIn("(DCP 9.1.6)", etiquetas["Spanish"])
        self.assertNotIn("(DCP 9.1.6)", etiquetas.get("English", ""))

    def test_el_nombre_de_pista_sigue_el_idioma(self):
        """Decidido el 2026-09-15: un MKV creado con la app en inglés dice
        «English DTS-HD MA 5.1», no «Inglés»."""
        with idioma("es"):
            es = {p.raw.language: p.label for p in self._inc("es")}
        with idioma("en"):
            en = {p.raw.language: p.label for p in self._inc("en")}
        self.assertTrue(es["Spanish"].startswith("Castellano"), es)
        self.assertTrue(en["English"].startswith("English"), en)


class TestSubtitulos(unittest.TestCase):

    def _inc(self, cual: str, doblaje: bool = True, pistas=None):
        with idioma(cual):
            inc, _ = _select_subtitle_tracks(
                pistas if pistas is not None else DISCO_SUBS,
                "English", hay_doblaje=doblaje)
        return inc

    def test_en_castellano_sale_el_orden_de_la_spec(self):
        inc = self._inc("es")
        self.assertEqual(
            [(p.raw.language, p.subtitle_type) for p in inc],
            [("Spanish", "forced"), ("English", "complete"),
             ("Spanish", "complete"), ("English", "forced")])
        # El forzado castellano es el default y el único con flag_forced.
        self.assertEqual([p.flag_default for p in inc], [True, False, False, False])
        self.assertEqual([p.flag_forced for p in inc], [True, False, False, False])

    def test_en_ingles_el_default_va_al_forzado_ingles(self):
        inc = self._inc("en")
        idx = [(p.raw.language, p.subtitle_type) for p in inc]
        self.assertEqual(idx[0], ("English", "forced"))
        self.assertTrue(inc[0].flag_default)
        self.assertTrue(inc[0].flag_forced)
        self.assertNotIn(("Spanish", "forced"), idx)

    def test_en_catalan_entran_catalan_y_castellano(self):
        idx = [(p.raw.language, p.subtitle_type) for p in self._inc("ca")]
        self.assertIn(("Catalan", "forced"), idx)
        self.assertIn(("Catalan", "complete"), idx)
        self.assertIn(("Spanish", "complete"), idx)

    def test_sin_doblaje_el_default_pasa_al_completo(self):
        """Un forzado solo traduce los carteles: sirve si estás oyendo tu
        lengua. Sin esa pista de audio hace falta el diálogo entero."""
        inc = self._inc("es", doblaje=False)
        porgrupo = {(p.raw.language, p.subtitle_type): p for p in inc}
        self.assertFalse(porgrupo[("Spanish", "forced")].flag_default)
        self.assertTrue(porgrupo[("Spanish", "complete")].flag_default)
        # El `flag_forced` del contenedor no se mueve: sigue en el forzado.
        self.assertTrue(porgrupo[("Spanish", "forced")].flag_forced)

    def test_con_doblaje_el_default_sigue_en_el_forzado(self):
        inc = self._inc("es", doblaje=True)
        porgrupo = {(p.raw.language, p.subtitle_type): p for p in inc}
        self.assertTrue(porgrupo[("Spanish", "forced")].flag_default)
        self.assertFalse(porgrupo[("Spanish", "complete")].flag_default)

    def test_el_ingles_entra_siempre_como_red(self):
        """Era la regla de la spec desde el principio: si no hay otro, hay
        subtítulo en inglés."""
        pistas = [sub("Spanish", 5000), sub("Spanish", 800),
                  sub("English", 6000), sub("French", 4800)]
        idx = [(p.raw.language, p.subtitle_type) for p in self._inc("ca", pistas=pistas)]
        self.assertIn(("English", "complete"), idx)


if __name__ == "__main__":
    unittest.main()


class TestElToggleDiceElPerfilDeVerdad(unittest.TestCase):
    """La etiqueta del toggle AFIRMA qué pistas conserva el perfil.

    Decía «Spanish + original» con la app en inglés mientras el perfil ya
    conservaba solo el inglés: la selección funcionaba y el rótulo mentía.
    Lo reportó el usuario el 2026-09-18, y **ningún detector de traducción
    podía verlo** — «Spanish + original» es inglés perfectamente correcto;
    lo que estaba mal era el contenido de la frase, no su lengua.

    Lo único que lo caza es cruzar la etiqueta con `IDIOMAS_DEL_PERFIL`. Y se
    cruza contra el CATÁLOGO y no contra una tabla en el JS a propósito: una
    réplica de una regla del backend en el frontend se desincroniza en
    silencio, que es la regla del proyecto. El catálogo ya es por idioma, así
    que cada lengua nombra su propio perfil sin que el JS decida nada.
    """

    # Las cuatro cadenas que describen el perfil `filtered`, y si además del
    # preferido tienen que nombrar el inglés (la red de los subtítulos).
    ETIQUETAS = {
        "core.castellano_vo": False,
        "core.solo_castellano_vo_con_seleccion_por": False,
        "core.castellano_vo_ingles": True,
        "core.solo_castellano_vo_ingles_detecta_forzados": True,
    }

    @classmethod
    def setUpClass(cls):
        import json
        front = APP_DIR / "static" / "i18n"
        srv = APP_DIR / "i18n"
        cls.cat = {}
        for l in ("es", "en", "ca"):
            cls.cat[l] = {
                **json.loads((srv / f"{l}.json").read_text(encoding="utf-8")),
                **json.loads((front / f"{l}.json").read_text(encoding="utf-8")),
            }

    def test_la_siembra_manda_el_idioma_que_manda(self):
        """El nombre del idioma preferido viaja en `window.__I18N`, no como
        clave del catálogo.

        Como clave chocaba con «el mismo castellano se traduce igual»: su
        castellano es «Castellano», igual que `idioma.spanish`, pero su
        inglés es «English» y el de la otra «Spanish». Y replicar la tabla en
        el JS sería la réplica de una regla del backend que se desincroniza
        en silencio. Así que lo manda el servidor, que es quien resuelve el
        perfil — y aquí se comprueba que manda lo que toca.
        """
        from phases.phase_b import nombre_de_idioma
        for lengua in ("es", "en", "ca"):
            with self.subTest(idioma=lengua):
                with idioma(lengua):
                    pref = nombre_de_idioma(idiomas_preferidos(lengua)[0])
                self.assertEqual(pref, self.cat[lengua][
                    f"idioma.{idiomas_preferidos(lengua)[0]}"])

    def test_el_frontend_no_replica_la_tabla_del_perfil(self):
        """Si alguien vuelve a escribir los idiomas del perfil en el JS, la
        réplica se desincroniza y nadie se enteraría."""
        import re
        from frontend_sources import rutas
        fuera = []
        for r in rutas():
            if not str(r).endswith(".js"):
                continue
            src = Path(r).read_text(encoding="utf-8")
            for m in re.finditer(r"catalan\s*['\"]?\s*[:,]", src):
                linea = src[:m.start()].count("\n") + 1
                fuera.append(f"{Path(r).name}:{linea}")
        self.assertEqual(fuera, [], (
            "\nel perfil de idiomas se lee de la siembra "
            "(`idiomaDePistaPreferido`), no de una tabla en el JS:\n  · "
            + "\n  · ".join(fuera)))

    def test_ningun_mensaje_afirma_un_idioma_en_vez_de_usar_el_hueco(self):
        """Los cinco mensajes que hablan del idioma que manda lo llevan como
        PARÁMETRO (`{pref}`), no escrito dentro.

        Decían «no es Castellano» y «solo el de Castellano lleva flag
        forced»; con el perfil inglés eso es falso. Es el mismo defecto que
        el del toggle, un nivel más abajo: una frase correcta en su lengua
        que afirma algo que ya no pasa.
        """
        claves = ("phase_b.motivo_idioma_no_target_audio",
                  "phase_b.motivo_idioma_no_target_sub",
                  "phase_b.nota_flag_forced_no",
                  "phase_b.nota_flag_forzados_castellano",
                  "tab1.sin_flag_forced_de_matroska_porque")
        fallos = []
        for lengua in ("es", "en", "ca"):
            for clave in claves:
                v = self.cat[lengua][clave]
                if "{pref}" not in v:
                    fallos.append(f"[{lengua}] `{clave}` sin `{{pref}}`")
                    continue
                # Y que no lo diga ADEMÁS escrito: sería decirlo dos veces y
                # una de ellas mal.
                for lang in ("spanish", "english", "catalan"):
                    if lang in idiomas_preferidos(lengua):
                        continue
                    nombre = self.cat[lengua][f"idioma.{lang}"]
                    # «Inglés» es legítimo en el mensaje de subtítulos: el
                    # inglés entra siempre como red, no como preferido.
                    if lang == "english" and "no_target_sub" in clave:
                        continue
                    if nombre.lower() in v.lower():
                        fallos.append(f"[{lengua}] `{clave}` escribe "
                                      f"«{nombre}» en vez de usar el hueco")
        self.assertEqual(fallos, [], "\n  · ".join([""] + fallos))

    def test_cada_etiqueta_nombra_los_idiomas_que_el_perfil_conserva(self):
        fallos = []
        for lengua in ("es", "en", "ca"):
            prefs = idiomas_preferidos(lengua)
            for clave, con_ingles in self.ETIQUETAS.items():
                etiqueta = self.cat[lengua][clave]
                esperados = list(prefs)
                if con_ingles and "english" not in prefs:
                    esperados.append("english")
                for lang in esperados:
                    nombre = self.cat[lengua][f"idioma.{lang}"]
                    if nombre.lower() not in etiqueta.lower():
                        fallos.append(
                            f"[{lengua}] `{clave}` no nombra «{nombre}» "
                            f"(perfil: {esperados}): {etiqueta[:54]}")
        self.assertEqual(fallos, [], (
            "\nla etiqueta del toggle no dice el perfil que el idioma "
            "aplica:\n  · " + "\n  · ".join(fallos)))

    def test_ninguna_etiqueta_nombra_un_idioma_que_el_perfil_descarta(self):
        """El defecto reportado era éste: la inglesa decía «Spanish» y el
        perfil inglés no conserva el castellano."""
        fallos = []
        for lengua in ("es", "en", "ca"):
            prefs = idiomas_preferidos(lengua)
            for clave, con_ingles in self.ETIQUETAS.items():
                etiqueta = self.cat[lengua][clave].lower()
                permitidos = set(prefs) | ({"english"} if con_ingles else set())
                for lang in ("spanish", "english", "catalan"):
                    if lang in permitidos:
                        continue
                    nombre = self.cat[lengua][f"idioma.{lang}"].lower()
                    if nombre in etiqueta:
                        fallos.append(
                            f"[{lengua}] `{clave}` nombra «{nombre}», que el "
                            f"perfil {prefs} NO conserva")
        self.assertEqual(fallos, [], "\n  · ".join([""] + fallos))
