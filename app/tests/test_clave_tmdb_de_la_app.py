"""La app trae su propia clave de TMDb, y configurar una es un override.

TMDb dejó de ser un requisito: la ficha de la película, el mapeo de episodios
del modo serie y la traducción ES→EN funcionan desde el primer arranque. Lo
que este módulo fija es el ORDEN —`settings.json > env > la de la app`— y sus
dos consecuencias, que son las que hacen que la promesa se sostenga:

  · **borrar tu clave no deja la app sin TMDb**, devuelve a la de la app;
  · **sin clave de la app, todo se comporta como antes** — un fork que no
    ponga la suya ve la UI de «no configurada» de siempre.

Y el detalle que decide cómo se ve: cuando la clave activa es la de la app, el
`last4` NO viaja al frontend. El usuario no la ha puesto, y enseñar una cola de
cuatro caracteres solo invita a confundirla con la suya.

La clave **no está en el repositorio**: se hornea en la imagen al construirla
(`ARG TMDB_APP_KEY` ← secreto del workflow). Por eso aquí se parchea la
variable de entorno y no una constante, y por eso hay un guard aparte del
cableado Dockerfile → compose → workflow: si una de las tres puntas se cae, la
imagen se publica sin clave y no lo nota nadie hasta que un usuario se queja.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_clave_tmdb_de_la_app -v
"""
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402

CLAVE_APP = "clavedelaapp0000"


class StoreCase(unittest.TestCase):
    """`app_settings.json` en un tmpdir y el entorno sin `TMDB_API_KEY`."""

    def setUp(self):
        from services import settings_store as st
        self.st = st
        self.tmp = Path(tempfile.mkdtemp(prefix="clave_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        orig = (st.CONFIG_DIR, st.SETTINGS_PATH)
        st.CONFIG_DIR = self.tmp
        st.SETTINGS_PATH = self.tmp / "app_settings.json"
        self.addCleanup(lambda: setattr(st, "SETTINGS_PATH", orig[1]))
        self.addCleanup(lambda: setattr(st, "CONFIG_DIR", orig[0]))

        # El store cachea lo leído; si no se limpia, un test ve el fichero del
        # anterior (o el /config real de la máquina).
        for nombre in ("_cache", "_cached", "_settings_cache"):
            if hasattr(st, nombre):
                setattr(st, nombre, None)
        self._limpiar_cache()

        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        os.environ.pop("TMDB_API_KEY", None)
        os.environ.pop("TMDB_APP_KEY", None)
        self.addCleanup(env.stop)

    def _limpiar_cache(self):
        for nombre in dir(self.st):
            if nombre.startswith("_") and "cache" in nombre.lower():
                valor = getattr(self.st, nombre)
                if isinstance(valor, dict):
                    valor.clear()

    def con_clave_de_app(self, valor=CLAVE_APP):
        """La que hornea el build, simulada — nunca la real."""
        if valor:
            os.environ[self.st.ENV_CLAVE_TMDB_DE_LA_APP] = valor
        else:
            os.environ.pop(self.st.ENV_CLAVE_TMDB_DE_LA_APP, None)
        self.addCleanup(os.environ.pop, self.st.ENV_CLAVE_TMDB_DE_LA_APP, None)


class TestElOrdenDePrioridad(StoreCase):

    def test_sin_configurar_nada_se_usa_la_clave_de_la_app(self):
        self.con_clave_de_app()
        self.assertEqual(self.st.get_tmdb_api_key(), CLAVE_APP)

    def test_la_del_usuario_gana_a_la_de_la_app(self):
        self.con_clave_de_app()
        self.st.update_tmdb_api_key("la_mia_1234")
        self.assertEqual(self.st.get_tmdb_api_key(), "la_mia_1234")

    def test_la_del_entorno_tambien_gana_a_la_de_la_app(self):
        self.con_clave_de_app()
        os.environ["TMDB_API_KEY"] = "del_entorno_1234"
        self.assertEqual(self.st.get_tmdb_api_key(), "del_entorno_1234")

    def test_la_del_usuario_gana_a_la_del_entorno(self):
        self.con_clave_de_app()
        os.environ["TMDB_API_KEY"] = "del_entorno_1234"
        self.st.update_tmdb_api_key("la_mia_1234")
        self.assertEqual(self.st.get_tmdb_api_key(), "la_mia_1234")

    def test_borrar_la_propia_devuelve_a_la_de_la_app_no_a_nada(self):
        """Es lo que hace «Vaciar todo» de ⚙︎. Nunca te quedas sin TMDb."""
        self.con_clave_de_app()
        self.st.update_tmdb_api_key("la_mia_1234")
        self.st.update_tmdb_api_key("")
        self.assertEqual(self.st.get_tmdb_api_key(), CLAVE_APP)


class TestUnBuildSinClaveSeComportaComoAntes(StoreCase):

    def test_sin_clave_de_app_no_hay_clave(self):
        self.con_clave_de_app("")
        self.assertEqual(self.st.get_tmdb_api_key(), "")
        self.assertFalse(self.st.get_public_settings()["tmdb"]["configured"])

    def test_los_espacios_de_un_build_arg_mal_pegado_no_cuentan(self):
        """Un `TMDB_APP_KEY=" abc "` en el .env no debe llegar así a TMDb."""
        self.con_clave_de_app("  " + CLAVE_APP + "\n")
        self.assertEqual(self.st.clave_tmdb_de_la_app(), CLAVE_APP)

    def test_sin_clave_de_app_la_propia_sigue_funcionando(self):
        self.con_clave_de_app("")
        self.st.update_tmdb_api_key("la_mia_1234")
        self.assertEqual(self.st.get_tmdb_api_key(), "la_mia_1234")


class TestLoQueVeElFrontend(StoreCase):

    def test_con_la_de_la_app_sale_configurada_y_como_default(self):
        self.con_clave_de_app()
        tmdb = self.st.get_public_settings()["tmdb"]
        self.assertTrue(tmdb["configured"])
        self.assertEqual(tmdb["source"], "default")
        self.assertTrue(tmdb["is_default"])

    def test_el_last4_de_la_clave_de_la_app_no_se_manda(self):
        """No la ha puesto el usuario: una cola de 4 invita a confundirla."""
        self.con_clave_de_app()
        self.assertEqual(self.st.get_public_settings()["tmdb"]["last4"], "")

    def test_con_la_propia_si_sale_el_last4_y_la_fuente_es_settings(self):
        self.con_clave_de_app()
        self.st.update_tmdb_api_key("la_mia_wxyz")
        tmdb = self.st.get_public_settings()["tmdb"]
        self.assertEqual(tmdb["source"], "settings")
        self.assertEqual(tmdb["last4"], "wxyz")
        self.assertFalse(tmdb["is_default"])

    def test_la_clave_de_la_app_no_sale_en_crudo(self):
        self.con_clave_de_app()
        self.assertNotIn(CLAVE_APP, str(self.st.get_public_settings()))

    def test_google_no_tiene_default_y_lo_dice(self):
        """La de Google no puede venir incluida: su cuota es por proyecto."""
        os.environ.pop("GOOGLE_API_KEY", None)
        google = self.st.get_public_settings()["google"]
        self.assertFalse(google["configured"])
        self.assertEqual(google["source"], "none")
        self.assertFalse(google["is_default"])


class TestElBotonProbar(ApiTestCase):
    """Con el campo vacío prueba la clave ACTIVA, que es la única pregunta
    que trae a alguien a ese botón desde que hay clave incluida."""

    def _fake_test(self, respuesta=(True, "API key válida")):
        """Sustituye la llamada real a TMDb y captura QUÉ clave se probó."""
        import services.tmdb as tmdb_mod
        probadas = []

        async def _fake(api_key):
            probadas.append(api_key)
            return respuesta

        p = mock.patch.object(tmdb_mod, "test_api_key", _fake)
        p.start()
        self.addCleanup(p.stop)
        return probadas

    def test_con_una_clave_escrita_prueba_esa(self):
        probadas = self._fake_test()
        r = self.client.post("/api/settings/test-tmdb",
                             json={"tmdb_api_key": "la_que_escribo"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(probadas, ["la_que_escribo"])
        self.assertEqual(r.json()["probada"], "propia")

    def test_con_el_campo_vacio_prueba_la_activa(self):
        with mock.patch.dict(os.environ, {"TMDB_APP_KEY": CLAVE_APP}):
            probadas = self._fake_test()
            r = self.client.post("/api/settings/test-tmdb", json={})
        self.assertEqual(probadas, [CLAVE_APP], "no ha probado la de la app")
        d = r.json()
        self.assertEqual(d["probada"], "app")
        self.assertTrue(d["ok"])
        self.assertIn("la clave de la app", d["message"].lower())

    def test_si_la_de_la_app_ha_caducado_el_mensaje_dice_que_hacer(self):
        with mock.patch.dict(os.environ, {"TMDB_APP_KEY": CLAVE_APP}):
            self._fake_test((False, "API key inválida"))
            r = self.client.post("/api/settings/test-tmdb", json={})
        d = r.json()
        self.assertFalse(d["ok"])
        self.assertIn("configura la tuya", d["message"].lower())

    def test_sin_ninguna_clave_lo_dice_en_vez_de_llamar_a_tmdb(self):
        with mock.patch.dict(os.environ, {"TMDB_APP_KEY": ""}):
            probadas = self._fake_test()
            r = self.client.post("/api/settings/test-tmdb", json={})
        self.assertEqual(probadas, [], "ha salido a la red sin clave")
        self.assertEqual(r.json()["probada"], "ninguna")


class TestElCableadoDelBuild(unittest.TestCase):
    """Dockerfile → compose → workflow, las tres puntas.

    Si una se cae, la imagen se publica **sin clave** y no falla nada: la app
    arranca, responde y los usuarios ven TMDb como no configurado. Nadie se
    entera hasta que alguien se queja. Por eso las tres se comprueban aquí y
    el workflow además avisa en voz alta si el secreto no está puesto.
    """

    REPO = APP_DIR.parent
    VAR = "TMDB_APP_KEY"

    def _leer(self, rel: str) -> str:
        return (self.REPO / rel).read_text(encoding="utf-8")

    def test_el_nombre_de_la_variable_es_el_mismo_en_python(self):
        from services import settings_store as st
        self.assertEqual(st.ENV_CLAVE_TMDB_DE_LA_APP, self.VAR)

    def test_el_dockerfile_la_acepta_como_arg_y_la_deja_como_env(self):
        """El ARG solo la recibe; sin el ENV no sobrevive al build."""
        dockerfile = self._leer("docker/Dockerfile")
        self.assertIn(f'ARG {self.VAR}=""', dockerfile)
        self.assertIn(f"ENV {self.VAR}=${{{self.VAR}}}", dockerfile)

    def test_el_compose_la_pasa_como_build_arg(self):
        """Para que `up -d --build` con la variable en el .env funcione."""
        compose = self._leer("docker/docker-compose.yml")
        self.assertIn(f"{self.VAR}: ${{{self.VAR}:-}}", compose)

    def test_el_workflow_la_inyecta_desde_el_secreto(self):
        wf = self._leer(".github/workflows/publish-docker.yml")
        self.assertIn(f"{self.VAR}=${{{{ secrets.{self.VAR} }}}}", wf)

    def test_el_workflow_avisa_si_el_secreto_falta(self):
        """Un build sin clave es válido, pero tiene que verse.

        El chequeo va en el `run` y no en un `if:`: el contexto `secrets` no
        está garantizado en las condiciones de un step, y un aviso que no se
        evalúa es peor que ninguno.
        """
        wf = self._leer(".github/workflows/publish-docker.yml")
        self.assertIn("::warning", wf)
        self.assertIn('if [ -z "$CLAVE_APP" ]', wf)
        self.assertNotIn(f"if: ${{{{ secrets.{self.VAR}", wf)


class TestLaClaveNoVuelveAlCodigo(unittest.TestCase):
    """El repositorio es público y la decisión fue que la clave no esté en él.

    Una clave de TMDb v3 son 32 hexadecimales. Este guard no puede saber si
    una cadena así es LA clave, pero sí que no debería haber ninguna suelta en
    el código ni en la configuración del contenedor — que es exactamente la
    forma que buscan los rastreadores que peinan GitHub.
    """

    REPO = APP_DIR.parent
    # 32 hex seguidos, sin nada hexadecimal pegado a los lados.
    HEX32 = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])")

    def test_no_hay_ninguna_cadena_con_pinta_de_clave(self):
        sospechosas = []
        objetivos = list((self.REPO / "app").rglob("*.py"))
        objetivos += list((self.REPO / "app" / "static").glob("*.js"))
        objetivos += [self.REPO / "docker" / "Dockerfile",
                      self.REPO / "docker" / "docker-compose.yml",
                      self.REPO / "docker" / ".env.example"]
        for f in objetivos:
            if "__pycache__" in str(f) or not f.exists():
                continue
            # Los tests llevan claves de mentira a propósito.
            if f.suffix == ".py" and f.parent.name == "tests":
                continue
            for n, linea in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if self.HEX32.search(linea):
                    sospechosas.append(f"{f.relative_to(self.REPO)}:{n}: {linea.strip()[:70]}")
        self.assertEqual(sospechosas, [], "cadenas con forma de API key en el repo:\n"
                                          + "\n".join(sospechosas))


if __name__ == "__main__":
    unittest.main()
