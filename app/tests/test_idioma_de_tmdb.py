# -*- coding: utf-8 -*-
"""El idioma con el que se le pide la ficha a TMDb.

Nadie comprobaba esto, y por eso el bug duró hasta que el usuario lo vio en
pantalla (2026-09-17): con la app en inglés, el título y la sinopsis de la
tarjeta seguían llegando en castellano. El `es-ES` estaba cableado en cinco
peticiones **y** la caché de 30 días no llevaba el idioma en la clave, así
que la primera consulta en castellano se servía después a la app en inglés.

Aquí se ejecutan las funciones con un `httpx` de mentira y se afirma sobre
**el parámetro `language` que sale en la petición**, que es el dato que
estaba mal. Un test que solo mirara el retorno no habría visto nada: el
fake habría devuelto lo que le pidieras.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


class _TmdbFalso:
    """Registra cada petición y contesta lo que se le diga.

    `vacios` son los campos que la respuesta deja en blanco cuando el
    `language` NO es `es-ES`: así se reproduce lo que TMDb hace de verdad
    con el catalán —contestar 200 con los campos traducibles vacíos— sin lo
    cual el respaldo no se puede comprobar.
    """

    def __init__(self, cuerpo: dict, vacios: tuple[str, ...] = ()):
        self.cuerpo, self.vacios = cuerpo, vacios
        self.peticiones: list[tuple[str, dict]] = []

    def cliente(self):
        fake = self

        class _Resp:
            def __init__(self, datos): self._datos = datos
            def raise_for_status(self): return None
            def json(self): return self._datos

        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

            async def get(self, url, params=None):
                params = params or {}
                fake.peticiones.append((url, dict(params)))
                datos = dict(fake.cuerpo)
                if params.get("language") != "es-ES":
                    for c in fake.vacios:
                        datos[c] = [] if isinstance(datos.get(c), list) else ""
                return _Resp(datos)

        return _Client

    @property
    def idiomas(self) -> list[str]:
        return [p.get("language") for _, p in self.peticiones]


class IdiomaDeTmdb(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))
        # `settings_store` resuelve su CONFIG_DIR en el import, así que se
        # parchea el ya resuelto — igual que hace `api_harness`.
        import services.settings_store as st
        import services.tmdb as tmdb
        import i18n
        self.st, self.tmdb = st, tmdb
        for mod, attr in ((st, "CONFIG_DIR"), (tmdb, "CONFIG_DIR")):
            p = mock.patch.object(mod, attr, self.tmp)
            p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(tmdb, "CACHE_PATH", self.tmp / "tmdb_cache.json")
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(st, "SETTINGS_PATH", self.tmp / "app_settings.json")
        p.start(); self.addCleanup(p.stop)
        os.environ.pop("HDO_IDIOMA", None)
        tmdb._cache = None
        self.addCleanup(setattr, tmdb, "_cache", None)
        i18n.limpiar_cache()
        self.addCleanup(i18n.limpiar_cache)

    def _idioma(self, valor: str):
        (self.tmp / "app_settings.json").write_text(
            json.dumps({"idioma": valor}), encoding="utf-8")
        self.st._cache = None
        import i18n
        i18n.limpiar_cache()

    # ── El locale ─────────────────────────────────────────────────────
    def test_cada_idioma_pide_su_locale(self):
        for idioma, locale in (("es", "es-ES"), ("en", "en-US"),
                               ("ca", "ca-ES")):
            with self.subTest(idioma=idioma):
                self._idioma(idioma)
                self.assertEqual(self.tmdb.locale_tmdb(), locale)

    def test_un_idioma_desconocido_cae_al_castellano(self):
        self._idioma("es")
        with mock.patch("i18n.idioma_activo", return_value="pt"):
            self.assertEqual(self.tmdb.locale_tmdb(), "es-ES")

    # ── La caché ──────────────────────────────────────────────────────
    def test_la_clave_de_cache_separa_los_idiomas(self):
        self._idioma("es")
        en_es = self.tmdb._cache_key("Blade Runner 2049", 2017)
        self._idioma("en")
        en_en = self.tmdb._cache_key("Blade Runner 2049", 2017)
        self.assertNotEqual(en_es, en_en, "la ficha castellana se serviría "
                                          "a la app en inglés 30 días")

    # ── Las peticiones ────────────────────────────────────────────────
    def _detalles(self, idioma, cuerpo, vacios=()):
        self._idioma(idioma)
        self.st.update_tmdb_api_key("clave_de_prueba")
        f = _TmdbFalso(cuerpo, vacios)
        with mock.patch.object(self.tmdb.httpx, "AsyncClient", f.cliente()):
            res = asyncio.run(self.tmdb.fetch_details(1234))
        return f, res

    def test_la_ficha_se_pide_en_el_idioma_de_la_app(self):
        f, res = self._detalles("en", {"id": 1234, "title": "Blade Runner 2049",
                                       "overview": "A young blade runner."})
        self.assertEqual(f.idiomas, ["en-US"])
        self.assertEqual(res.title, "Blade Runner 2049")

    def test_una_ficha_muda_se_repite_en_castellano(self):
        # Es el caso real del catalán: TMDb contesta 200 con la sinopsis en
        # blanco, y una tarjeta sin sinopsis es peor que una en castellano.
        f, res = self._detalles(
            "ca", {"id": 1234, "title": "Blade Runner 2049",
                   "overview": "Un jove blade runner."}, vacios=("overview",))
        self.assertEqual(f.idiomas, ["ca-ES", "es-ES"])
        self.assertTrue(res.overview)

    def test_con_ficha_en_el_idioma_pedido_no_se_repite(self):
        f, _ = self._detalles("ca", {"id": 1234, "title": "Blade Runner 2049",
                                     "overview": "Un jove blade runner."})
        self.assertEqual(f.idiomas, ["ca-ES"])

    def test_el_castellano_nunca_se_pide_dos_veces(self):
        f, _ = self._detalles("es", {"id": 1234, "title": "Blade Runner 2049",
                                     "overview": ""}, vacios=("overview",))
        self.assertEqual(f.idiomas, ["es-ES"])

    # ── Lo que NO debe cambiar ────────────────────────────────────────
    def test_el_titulo_ingles_del_match_sigue_pidiendose_en_en_us(self):
        """`_fetch_english_title` existe para la traducción ES→EN del match
        contra la hoja de DoviTools, que está en inglés. Ese `en-US` no es
        el idioma de la interfaz y seguir el de la app lo rompería: con la
        app en catalán, el match se haría contra un título catalán."""
        self._idioma("ca")
        f = _TmdbFalso({"id": 1234, "title": "Blade Runner 2049"})
        cliente = f.cliente()()

        async def _ir():
            return await self.tmdb._fetch_english_title(cliente, 1234, "k")

        asyncio.run(_ir())
        self.assertEqual(f.idiomas, ["en-US"])


if __name__ == "__main__":
    unittest.main()
