"""El repo DoviTools viene con la app, y la app reconoce la puerta del autor.

El enlace del repositorio se verificó **genérico** antes de incluirlo
(2026-09-14): una petición anónima a la carpeta, sin sesión de Google ni
credencial, devuelve el listado — está compartida como «cualquiera con el
enlace» y el ID es el de la carpeta, igual para todos. Lo que el manual
describe («dona, manda tu correo y te dan acceso») es una puerta SOCIAL.

Como esa puerta es de otra persona, la app no la ignora: al usar el enlace
incluido cuenta los bins descargados y cada `DESCARGAS_POR_AVISO` recuerda la
donación. Lo que fija este módulo son los dos límites del recordatorio:

  · **nunca con repo propio** — quien lo puso, donó. Y se compara el ID, no
    la URL: el mismo repo se pega con `?usp=sharing` o sin nada;
  · **se cuentan descargas COMPLETADAS**, en el único sitio por el que baja
    un bin (`download_file`), que es por donde pasan las dos rutas que
    existen — el pre-flight y la Fase B.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_repo_dovitools -v
"""
import os
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

ID_APP = "1lg46Oic1pWiANf79zwGrdd74Gq5lmPgN"
REPO_APP = f"https://drive.google.com/drive/folders/{ID_APP}"
REPO_OTRO = "https://drive.google.com/drive/folders/0000000000000000000000000000"


class StoreCase(unittest.TestCase):
    """`app_settings.json` en un tmpdir y el entorno sin las variables."""

    def setUp(self):
        from services import settings_store as st
        self.st = st
        self.tmp = Path(tempfile.mkdtemp(prefix="repo_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        orig = (st.CONFIG_DIR, st.SETTINGS_PATH, st._cache)
        st.CONFIG_DIR = self.tmp
        st.SETTINGS_PATH = self.tmp / "app_settings.json"
        st._cache = None
        self.addCleanup(lambda: setattr(st, "_cache", orig[2]))
        self.addCleanup(lambda: setattr(st, "SETTINGS_PATH", orig[1]))
        self.addCleanup(lambda: setattr(st, "CONFIG_DIR", orig[0]))

        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for v in ("CMV40_DRIVE_FOLDER_URL", "CMV40_DRIVE_FOLDER_ID",
                  st.ENV_DRIVE_FOLDER_DE_LA_APP):
            os.environ.pop(v, None)

    def con_repo_de_la_app(self, url=REPO_APP):
        if url:
            os.environ[self.st.ENV_DRIVE_FOLDER_DE_LA_APP] = url
        self.addCleanup(os.environ.pop, self.st.ENV_DRIVE_FOLDER_DE_LA_APP, None)


class TestElOrdenDePrioridad(StoreCase):

    def test_sin_configurar_nada_se_usa_el_repo_de_la_app(self):
        self.con_repo_de_la_app()
        self.assertEqual(self.st.get_cmv40_drive_folder_id(), ID_APP)

    def test_el_del_usuario_gana(self):
        self.con_repo_de_la_app()
        self.st.update_cmv40_drive_folder_url(REPO_OTRO)
        self.assertNotEqual(self.st.get_cmv40_drive_folder_id(), ID_APP)

    def test_el_del_entorno_gana_al_de_la_app(self):
        self.con_repo_de_la_app()
        os.environ["CMV40_DRIVE_FOLDER_URL"] = REPO_OTRO
        self.assertNotEqual(self.st.get_cmv40_drive_folder_id(), ID_APP)

    def test_sin_repo_de_la_app_todo_queda_como_antes(self):
        """Un build sin el arg: la pestaña Repo sigue pidiendo el enlace."""
        self.assertEqual(self.st.get_cmv40_drive_folder_id(), "")
        self.assertFalse(
            self.st.get_public_settings()["drive_folder"]["configured"])

    def test_el_estado_publico_lo_marca_como_default_y_sin_last4(self):
        self.con_repo_de_la_app()
        d = self.st.get_public_settings()["drive_folder"]
        self.assertTrue(d["configured"])
        self.assertEqual(d["source"], "default")
        self.assertTrue(d["is_default"])
        self.assertEqual(d["last4"], "")
        # El ID de una carpeta pública no es un secreto y ayuda a reconocerla.
        self.assertTrue(d["folder_id_last6"])


class TestCuandoSeRecuerdaLaDonacion(StoreCase):

    def descargar(self, n):
        for _ in range(n):
            self.st.registrar_descarga_de_bin()

    def test_con_el_repo_de_la_app_se_avisa_al_llegar_al_umbral(self):
        self.con_repo_de_la_app()
        cada = self.st.DESCARGAS_POR_AVISO
        self.descargar(cada - 1)
        self.assertFalse(self.st.estado_donacion_dovitools()["avisar"])
        self.descargar(1)
        d = self.st.estado_donacion_dovitools()
        self.assertTrue(d["avisar"])
        self.assertEqual(d["descargas"], cada)

    def test_tras_verlo_la_cuenta_arranca_de_nuevo(self):
        self.con_repo_de_la_app()
        cada = self.st.DESCARGAS_POR_AVISO
        self.descargar(cada)
        self.st.marcar_donacion_avisada()
        self.assertFalse(self.st.estado_donacion_dovitools()["avisar"])
        # El total sigue siendo el histórico, no vuelve a cero.
        self.assertEqual(self.st.estado_donacion_dovitools()["descargas"], cada)
        self.descargar(cada - 1)
        self.assertFalse(self.st.estado_donacion_dovitools()["avisar"])
        self.descargar(1)
        self.assertTrue(self.st.estado_donacion_dovitools()["avisar"])

    def test_con_repo_propio_no_se_cuenta_ni_se_avisa_jamas(self):
        """Quien puso su enlace, donó."""
        self.con_repo_de_la_app()
        self.st.update_cmv40_drive_folder_url(REPO_OTRO)
        self.descargar(self.st.DESCARGAS_POR_AVISO * 3)
        d = self.st.estado_donacion_dovitools()
        self.assertFalse(d["repo_de_la_app"])
        self.assertFalse(d["avisar"])
        self.assertEqual(d["descargas"], 0)

    def test_el_donante_que_pega_el_mismo_enlace_deja_de_recibir_avisos(self):
        """Quien dona recibe... el MISMO enlace: la carpeta es pública y
        única. Así que lo que decide no puede ser a qué carpeta apunta —eso
        le recordaría para siempre una donación que ya hizo— sino de dónde
        sale: lo distintivo del donante es que se molestó en pegarlo."""
        self.con_repo_de_la_app()
        self.st.update_cmv40_drive_folder_url(REPO_APP + "?usp=sharing")
        self.assertFalse(self.st.estado_donacion_dovitools()["avisar"])
        self.descargar(self.st.DESCARGAS_POR_AVISO)
        self.assertFalse(self.st.estado_donacion_dovitools()["avisar"])

    def test_sin_repo_de_la_app_no_se_cuenta_nada(self):
        self.descargar(5)
        self.assertEqual(self.st.estado_donacion_dovitools()["descargas"], 0)


class TestLaDescargaEsLaQueCuenta(StoreCase):
    """El contador vive DENTRO de `download_file`, el único sitio por el que
    baja un bin: las dos rutas (pre-flight y Fase B) pasan por ahí."""

    def _drive_falso(self, status=200, cuerpo=b"RPU" * 100):
        """Un `httpx.AsyncClient` de mentira, para no salir a la red."""
        import services.rec999_drive as mod

        class _Resp:
            status_code = status
            headers = {"content-length": str(len(cuerpo))}

            async def aiter_bytes(self, chunk_size=0):
                yield cuerpo

            async def aread(self):
                return cuerpo

        class _Stream:
            async def __aenter__(self): return _Resp()
            async def __aexit__(self, *a): return False

        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def stream(self, *a, **k): return _Stream()

        p = mock.patch.object(mod.httpx, "AsyncClient", _Client)
        p.start()
        self.addCleanup(p.stop)

    async def _bajar(self):
        from services.rec999_drive import download_file
        destino = self.tmp / "RPU_target.bin"
        return await download_file("id_de_prueba", destino), destino

    def test_una_descarga_completada_suma_uno(self):
        import asyncio
        self.con_repo_de_la_app()
        self.st.update_google_api_key("clave_google")
        self._drive_falso()
        escritos, destino = asyncio.run(self._bajar())
        self.assertTrue(destino.exists())
        self.assertEqual(escritos, 300)
        self.assertEqual(self.st.estado_donacion_dovitools()["descargas"], 1)

    def test_una_descarga_que_falla_no_suma(self):
        """Se cuentan descargas, no intentos."""
        import asyncio
        self.con_repo_de_la_app()
        self.st.update_google_api_key("clave_google")
        self._drive_falso(status=500, cuerpo=b"boom")
        with self.assertRaises(RuntimeError):
            asyncio.run(self._bajar())
        self.assertEqual(self.st.estado_donacion_dovitools()["descargas"], 0)

    def test_bajar_con_repo_propio_no_suma(self):
        import asyncio
        self.con_repo_de_la_app()
        self.st.update_cmv40_drive_folder_url(REPO_OTRO)
        self.st.update_google_api_key("clave_google")
        self._drive_falso()
        asyncio.run(self._bajar())
        self.assertEqual(self.st.estado_donacion_dovitools()["descargas"], 0)


class TestLosEndpoints(ApiTestCase):

    def setUp(self):
        super().setUp()
        from services import settings_store as st
        st._cache = None
        self.addCleanup(setattr, st, "_cache", None)
        self.st = st

    def test_sin_repo_de_la_app_nunca_avisa(self):
        r = self.client.get("/api/cmv40/repo-donacion")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["avisar"])
        self.assertFalse(r.json()["repo_de_la_app"])

    def test_el_visto_apaga_el_aviso(self):
        with mock.patch.dict(os.environ,
                             {self.st.ENV_DRIVE_FOLDER_DE_LA_APP: REPO_APP}):
            for _ in range(self.st.DESCARGAS_POR_AVISO):
                self.st.registrar_descarga_de_bin()
            self.assertTrue(self.client.get("/api/cmv40/repo-donacion").json()["avisar"])
            r = self.client.post("/api/cmv40/repo-donacion/visto")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertFalse(r.json()["avisar"])
            self.assertFalse(self.client.get("/api/cmv40/repo-donacion").json()["avisar"])


class TestElCableadoDelBuild(unittest.TestCase):
    """Dockerfile → compose → workflow. Si una punta se cae, la imagen sale
    sin el enlace y la pestaña Repo aparece vacía sin que falle nada."""

    REPO = APP_DIR.parent
    VAR = "CMV40_DRIVE_FOLDER_APP"

    def _leer(self, rel):
        return (self.REPO / rel).read_text(encoding="utf-8")

    def test_el_nombre_de_la_variable_es_el_mismo_en_python(self):
        from services import settings_store as st
        self.assertEqual(st.ENV_DRIVE_FOLDER_DE_LA_APP, self.VAR)

    def test_el_dockerfile_la_acepta_y_la_deja_como_env(self):
        d = self._leer("docker/Dockerfile")
        self.assertIn(f'ARG {self.VAR}=""', d)
        self.assertIn(f"ENV {self.VAR}=${{{self.VAR}}}", d)

    def test_el_compose_la_pasa_como_build_arg(self):
        self.assertIn(f"{self.VAR}: ${{{self.VAR}:-}}",
                      self._leer("docker/docker-compose.yml"))

    def test_el_workflow_la_inyecta_desde_el_secreto_y_avisa_si_falta(self):
        wf = self._leer(".github/workflows/publish-docker.yml")
        self.assertIn(f"{self.VAR}=${{{{ secrets.{self.VAR} }}}}", wf)
        self.assertIn('if [ -z "$REPO_APP" ]', wf)

    def test_el_enlace_no_esta_en_el_repositorio(self):
        """Mismo trato que la clave de TMDb: fuera del código."""
        objetivos = list((self.REPO / "app").rglob("*.py"))
        objetivos += list((self.REPO / "app" / "static").glob("*.js"))
        objetivos += [self.REPO / "docker" / "Dockerfile",
                      self.REPO / "docker" / "docker-compose.yml"]
        for f in objetivos:
            if "__pycache__" in str(f) or f.parent.name == "tests":
                continue
            self.assertNotIn(ID_APP, f.read_text(encoding="utf-8"),
                             f"el ID de la carpeta está en {f.name}")


if __name__ == "__main__":
    unittest.main()
