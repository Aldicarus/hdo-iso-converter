"""El MKV analizado que cambió de carpeta se encuentra solo (`phases.mkv_reubicar`).

La caché de «Consultar / Editar MKV» identifica cada MKV por su contenido, así
que moverlo no invalida su análisis; lo que se quedaba atrás era la ruta, y la
columna lo pintaba «No encontrado», sin botón de abrir. Es el flujo normal:
CMv4.0 deja el análisis hecho con el MKV en Output y el usuario lo mueve a su
biblioteca. Medido en el NAS el 2026-10-27, 3 de los 10 MKV de la columna.

Lo que estos tests protegen:

* **Por contenido, nunca por nombre.** Un fichero del mismo tamaño con otro
  contenido no hereda el análisis, y uno del mismo nombre tampoco. Uno
  renombrado al moverlo sí.
* **Las carpetas de sistema no cuentan.** Una copia en un snapshot o en la
  papelera tiene el mismo contenido y daría por vivo un fichero borrado; en
  `.@__thumb` viven las miniaturas de QNAP, que se llaman «*.mkv».
* **Ni un enlace simbólico**: la ruta nueva tiene que poder abrirse, y eso
  exige que viva bajo una de las tres carpetas.
* **Lo que está no se toca.** Ni un fingerprint ni un recorrido si no falta
  nada: la columna se pide al entrar en la pestaña.
* **Lo que no aparece no se busca en cada petición**, salvo que lo pidan, y
  un MKV que acaba de desaparecer se busca aunque haya otro en la memoria.
* **Un recorrido cortado no concluye** que algo no está.
* **La entrada vieja de la ruta nueva se olvida**: con dos tarjetas para la
  misma ruta, «Borrar» en una se llevaba las dos.
* **Limpieza no ofrece borrar lo que solo se movió**, y su escaneo no escribe.

Ejecutar desde la raíz del repo:
    python3 app/tests/correr_suite.py test_tab2_reubicar
"""
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402

CUANDO = "2026-10-27T10:00:00+00:00"


class ReubicarCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        import storage
        from phases import mkv_reubicar
        from phases.mkv_analyze import CACHE_VERSION_BASIC, CACHE_VERSION_QUALITY
        self.storage, self.reubicar = storage, mkv_reubicar
        self.v_basic, self.v_quality = CACHE_VERSION_BASIC, CACHE_VERSION_QUALITY
        self.audits = storage.MKV_AUDIT_DIR
        self.audits.mkdir(parents=True, exist_ok=True)
        # Un sitio que no es ninguna de las tres carpetas, para lo que se va.
        self.fuera = self.tmp / "fuera"
        self.fuera.mkdir()

    # ── construcción de estado ───────────────────────────────────────

    @staticmethod
    def contenido(semilla: str, tamano: int = 3000) -> bytes:
        """Unos bytes que dependen de la semilla, para huellas distintas."""
        base = (semilla * 64).encode()
        return (base * (tamano // len(base) + 1))[:tamano]

    def mkv(self, ruta: Path, semilla: str, tamano: int = 3000) -> Path:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(self.contenido(semilla, tamano))
        return ruta

    def analizado(self, ruta: Path, *, quality=True, cuando=CUANDO) -> dict:
        """La entrada de caché que dejaría analizarlo en `ruta`, con la forma
        de `storage._write_mkv_cache_full`. Devuelve su fingerprint."""
        fp = self.storage.compute_mkv_fingerprint(str(ruta))
        versions = {"basic": self.v_basic}
        bloque = None
        if quality:
            versions["quality"] = self.v_quality
            bloque = {"quality_total_frames_rpu": 1000, "quality_classification": "real",
                      "light_profile": {"total_frames": 1000}}
        (self.audits / f"{fp['sha256_1mb']}.json").write_text(json.dumps({
            "schema_version": 1, "fingerprint": fp, "versions": versions,
            "cached_at": cuando,
            "basic": {"file_path": str(ruta), "file_name": ruta.name,
                      "duration_seconds": 7200.0},
            "quality": bloque,
            "original_file_path": str(ruta),
        }, ensure_ascii=False), encoding="utf-8")
        return fp

    def cache(self, fp: dict) -> dict:
        return json.loads((self.audits / f"{fp['sha256_1mb']}.json").read_text(encoding="utf-8"))

    def mover(self, desde: Path, hasta: Path) -> Path:
        hasta.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(desde), str(hasta))
        return hasta

    # ── acceso ───────────────────────────────────────────────────────

    def pedir(self, **params) -> dict:
        r = self.client.get("/api/mkv/recientes", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def tarjeta(self, cuerpo: dict, nombre: str) -> dict:
        [t] = [t for t in cuerpo["recientes"] if t["nombre"] == nombre]
        return t

    def contar_recorridos(self) -> list:
        """Cada recorrido de las tres carpetas que hace `mkv_reubicar`.

        Se espía su recorrido y no `os.scandir`: el `glob` con el que se lee
        la caché también lo usa, y contaría lo que no es."""
        recorridos = []
        original = self.reubicar._mkv_por_tamano

        def _espia(raices, tamanos, tope_s):
            recorridos.append(sorted(tamanos))
            return original(raices, tamanos, tope_s)

        self.reubicar._mkv_por_tamano = _espia
        self.addCleanup(setattr, self.reubicar, "_mkv_por_tamano", original)
        return recorridos


class TestLaColumnaEncuentraElMkvMovido(ReubicarCase):

    def test_movido_a_la_biblioteca_sale_con_la_ruta_nueva(self):
        viejo = self.mkv(self.output_dir / "Dune (2021) [CMv4 FULL].mkv", "dune")
        fp = self.analizado(viejo)
        nuevo = self.mover(viejo, self.library_dir / "UHD" / "CMv4.0" / viejo.name)
        cuerpo = self.pedir()
        t = self.tarjeta(cuerpo, viejo.name)
        self.assertEqual(t["ruta"], str(nuevo))
        self.assertTrue(t["existe"])
        self.assertTrue(t["tiene_extendido"], "el análisis vale entero: va por contenido")
        self.assertEqual(t["huella"], fp["sha256_1mb"])
        self.assertEqual(cuerpo["reubicados"], [
            {"nombre": viejo.name, "antes": str(viejo), "ahora": str(nuevo),
             "huella": fp["sha256_1mb"]}])
        # Y queda escrito: la próxima petición ni busca.
        guardado = self.cache(fp)
        self.assertEqual(guardado["original_file_path"], str(nuevo))
        self.assertEqual(guardado["basic"]["file_path"], str(nuevo))
        self.assertIsNotNone(guardado["quality"], "reapuntar no puede perder el extendido")

    def test_la_segunda_petición_ya_no_lo_anuncia(self):
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune")
        self.analizado(viejo)
        nuevo = self.mover(viejo, self.library_dir / "Dune.mkv")
        self.pedir()
        cuerpo = self.pedir()
        self.assertEqual(cuerpo["reubicados"], [], "se dice una vez")
        self.assertEqual(self.tarjeta(cuerpo, "Dune.mkv")["ruta"], str(nuevo))

    def test_renombrado_al_moverlo_también(self):
        viejo = self.mkv(self.output_dir / "Peli.2024.UHD.BluRay [CMv4 FULL].mkv", "peli")
        self.analizado(viejo)
        nuevo = self.mover(viejo, self.library_dir / "Peli (2024)" / "Peli (2024).mkv")
        cuerpo = self.pedir()
        self.assertEqual(self.tarjeta(cuerpo, "Peli (2024).mkv")["ruta"], str(nuevo))

    def test_en_las_otras_dos_carpetas_también(self):
        for destino in (self.isos_dir, self.output_dir):
            with self.subTest(destino=destino.name):
                origen = self.mkv(self.library_dir / f"de {destino.name}.mkv", destino.name)
                self.analizado(origen)
                nuevo = self.mover(origen, destino / "sub" / origen.name)
                cuerpo = self.pedir()
                self.assertEqual(self.tarjeta(cuerpo, origen.name)["ruta"], str(nuevo))

    def test_en_mayúsculas_también(self):
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune")
        self.analizado(viejo)
        nuevo = self.mover(viejo, self.library_dir / "DUNE.MKV")
        self.assertEqual(self.tarjeta(self.pedir(), "DUNE.MKV")["ruta"], str(nuevo))

    def test_de_dos_del_mismo_tamaño_se_queda_el_que_coincide(self):
        """El del mismo nombre se mira primero, y aquí es otro contenido: el
        bueno es el renombrado, y hay que llegar a él."""
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune", tamano=4096)
        self.analizado(viejo)
        self.mkv(self.library_dir / "Dune.mkv", "otra peli", tamano=4096)
        nuevo = self.mover(viejo, self.library_dir / "Archivo" / "Dune (2021).mkv")
        self.assertEqual(self.tarjeta(self.pedir(), "Dune (2021).mkv")["ruta"], str(nuevo))

    def test_con_más_candidatos_que_el_tope_no_concluye(self):
        """Haber recorrido las carpetas no es haber descartado a todos los
        candidatos: el que no se miró puede ser el bueno."""
        original = self.reubicar.MAX_CANDIDATOS
        self.reubicar.MAX_CANDIDATOS = 1
        self.addCleanup(setattr, self.reubicar, "MAX_CANDIDATOS", original)
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune", tamano=4096)
        fp = self.analizado(viejo)
        self.mkv(self.library_dir / "Dune.mkv", "otra peli", tamano=4096)
        self.mover(viejo, self.library_dir / "Archivo" / "Dune (2021).mkv")
        t = self.tarjeta(self.pedir(), "Dune.mkv")
        self.assertFalse(t["existe"])
        self.assertFalse(t["buscado"])
        self.assertFalse(self.reubicar.buscado_sin_exito(fp["sha256_1mb"]))

    def test_un_candidato_que_no_se_deja_leer_no_concluye(self):
        if os.geteuid() == 0:
            self.skipTest("root lee cualquier fichero")
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune")
        self.analizado(viejo)
        nuevo = self.mover(viejo, self.library_dir / "Dune.mkv")
        nuevo.chmod(0)
        self.addCleanup(nuevo.chmod, 0o644)
        t = self.tarjeta(self.pedir(), "Dune.mkv")
        self.assertFalse(t["existe"])
        self.assertFalse(t["buscado"], "no haberlo podido leer no es haberlo descartado")

    def test_una_carpeta_de_la_app_que_falta_no_concluye(self):
        """Un montaje caído es una carpeta sin mirar, no una carpeta vacía."""
        import paths
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune")
        self.analizado(viejo)
        viejo.unlink()
        raices = dict(paths.LIBRARY_ROOTS, library=self.tmp / "montaje caido")
        original = paths.LIBRARY_ROOTS
        paths.LIBRARY_ROOTS = raices
        self.addCleanup(setattr, paths, "LIBRARY_ROOTS", original)
        self.assertFalse(self.tarjeta(self.pedir(), "Dune.mkv")["buscado"])

    def test_el_tamaño_se_vuelve_a_mirar_con_la_huella(self):
        """El tamaño del recorrido y el de la huella son dos lecturas: si el
        fichero creció entre las dos, no es el analizado aunque empiece igual."""
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune")
        self.analizado(viejo)
        self.mover(viejo, self.library_dir / "Dune.mkv")
        original = self.storage.compute_mkv_fingerprint

        def _crecido(ruta):
            fp = original(ruta)
            return fp and dict(fp, size_bytes=fp["size_bytes"] + 1)

        self.storage.compute_mkv_fingerprint = _crecido
        self.addCleanup(setattr, self.storage, "compute_mkv_fingerprint", original)
        self.assertFalse(self.tarjeta(self.pedir(), "Dune.mkv")["existe"])

    def test_el_recorrido_va_con_el_cerrojo_puesto(self):
        """Dos peticiones a la vez no recorren las carpetas dos veces, y la
        segunda ve la memoria que dejó la primera."""
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune")
        self.analizado(viejo)
        viejo.unlink()
        cerrado = []
        original = self.reubicar._mkv_por_tamano

        def _espia(*a):
            cerrado.append(self.reubicar._cerrojo.locked())
            return original(*a)

        self.reubicar._mkv_por_tamano = _espia
        self.addCleanup(setattr, self.reubicar, "_mkv_por_tamano", original)
        self.pedir()
        self.assertEqual(cerrado, [True])

    def test_mismo_tamaño_con_otro_contenido_no(self):
        """El tamaño filtra; la huella decide. Sin ella, un MKV cualquiera del
        mismo tamaño se quedaría con el análisis de otro."""
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune", tamano=4096)
        self.analizado(viejo)
        viejo.unlink()
        self.mkv(self.library_dir / "Dune.mkv", "otra peli", tamano=4096)
        t = self.tarjeta(self.pedir(), "Dune.mkv")
        self.assertEqual(t["ruta"], str(viejo))
        self.assertFalse(t["existe"])
        self.assertTrue(t["buscado"])

    def test_el_mismo_nombre_con_otro_contenido_no(self):
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune")
        self.analizado(viejo)
        viejo.unlink()
        self.mkv(self.library_dir / "Dune.mkv", "remux nuevo", tamano=5000)
        t = self.tarjeta(self.pedir(), "Dune.mkv")
        self.assertFalse(t["existe"])

    def test_una_copia_en_una_carpeta_de_sistema_no_cuenta(self):
        """Un snapshot o la papelera guardan el MISMO contenido de un fichero
        borrado: reapuntar ahí lo daría por vivo. Y en `.@__thumb` viven las
        miniaturas de QNAP, que se llaman «s800<nombre>.mkv»."""
        for sistema in (".zfs/snapshot/diario", "@Recycle", "@Recently-Snapshot",
                        "#snapshot", "#recycle", "$RECYCLE.BIN", ".@__thumb",
                        "lost+found"):
            with self.subTest(carpeta=sistema):
                self.reubicar.olvidar()
                viejo = self.mkv(self.output_dir / "Borrada.mkv", sistema)
                fp = self.analizado(viejo)
                self.mover(viejo, self.library_dir / sistema / "Borrada.mkv")
                t = self.tarjeta(self.pedir(), "Borrada.mkv")
                self.assertFalse(t["existe"], sistema)
                (self.audits / f"{fp['sha256_1mb']}.json").unlink()

    def test_un_enlace_simbólico_no_se_sigue(self):
        """Ni a un fichero ni a una carpeta de fuera: la ruta tiene que poder
        abrirse después, y eso exige que viva bajo una de las tres."""
        viejo = self.mkv(self.output_dir / "Enlazada.mkv", "enlace")
        self.analizado(viejo)
        real = self.mover(viejo, self.fuera / "Enlazada.mkv")
        os.symlink(real, self.library_dir / "Enlazada.mkv")
        os.symlink(self.fuera, self.library_dir / "carpeta enlazada")
        t = self.tarjeta(self.pedir(), "Enlazada.mkv")
        self.assertFalse(t["existe"])

    def test_la_entrada_vieja_de_la_ruta_nueva_se_olvida(self):
        """Se sustituye en la biblioteca la versión vieja por la nueva, con el
        mismo nombre. La entrada vieja dice estar en esa ruta, la huella
        acaba de demostrar que ahí hay otro contenido, y el suyo no está en
        ninguna carpeta: sin olvidarla, la columna pintaba dos tarjetas con la
        misma ruta, y «Borrar» en una (que va por ruta) se llevaba las dos."""
        en_biblioteca = self.mkv(self.library_dir / "Dune.mkv", "version vieja", tamano=6000)
        fp_viejo = self.analizado(en_biblioteca, cuando="2026-09-01T10:00:00+00:00")
        nuevo = self.mkv(self.output_dir / "Dune.mkv", "version nueva")
        fp_nuevo = self.analizado(nuevo)
        en_biblioteca.unlink()
        self.mover(nuevo, self.library_dir / "Dune.mkv")
        cuerpo = self.pedir()
        self.assertEqual([t["ruta"] for t in cuerpo["recientes"]],
                         [str(self.library_dir / "Dune.mkv")])
        self.assertFalse((self.audits / f"{fp_viejo['sha256_1mb']}.json").exists())
        self.assertEqual(self.cache(fp_nuevo)["original_file_path"],
                         str(self.library_dir / "Dune.mkv"))

    def test_la_versión_vieja_que_se_archivó_se_reapunta_en_vez_de_olvidarse(self):
        """La versión vieja no se sustituyó: se movió a otra carpeta, y la
        nueva ocupa su ruta. Su análisis sigue valiendo, y olvidarlo por la
        ruta lo tiraba (revisión de Codex del 2026-10-27)."""
        en_biblioteca = self.mkv(self.library_dir / "Dune.mkv", "version vieja", tamano=6000)
        fp_viejo = self.analizado(en_biblioteca, cuando="2026-09-01T10:00:00+00:00")
        nuevo = self.mkv(self.output_dir / "Dune.mkv", "version nueva")
        self.analizado(nuevo)
        archivada = self.mover(en_biblioteca, self.library_dir / "Archivo" / "Dune.mkv")
        self.mover(nuevo, self.library_dir / "Dune.mkv")
        cuerpo = self.pedir()
        self.assertEqual(sorted(t["ruta"] for t in cuerpo["recientes"]),
                         sorted([str(self.library_dir / "Dune.mkv"), str(archivada)]))
        self.assertEqual(self.cache(fp_viejo)["original_file_path"], str(archivada))
        self.assertEqual(len(cuerpo["reubicados"]), 2)

    def test_la_versión_vieja_sin_poder_concluir_no_se_olvida(self):
        """Si la búsqueda de la versión vieja no puede mirarlo todo, no se
        sabe si existe: no se borra su análisis."""
        en_biblioteca = self.mkv(self.library_dir / "Dune.mkv", "version vieja", tamano=6000)
        fp_viejo = self.analizado(en_biblioteca, cuando="2026-09-01T10:00:00+00:00")
        nuevo = self.mkv(self.output_dir / "Dune.mkv", "version nueva")
        self.analizado(nuevo)
        en_biblioteca.unlink()
        self.mover(nuevo, self.library_dir / "Dune.mkv")
        original = self.reubicar._mkv_por_tamano
        llamadas = []

        def _segunda_cortada(raices, tamanos, tope_s):
            hallados, completo = original(raices, tamanos, tope_s)
            llamadas.append(1)
            return hallados, completo and len(llamadas) == 1

        self.reubicar._mkv_por_tamano = _segunda_cortada
        self.addCleanup(setattr, self.reubicar, "_mkv_por_tamano", original)
        self.pedir()
        self.assertEqual(len(llamadas), 2, "la versión vieja se busca en una segunda pasada")
        self.assertTrue((self.audits / f"{fp_viejo['sha256_1mb']}.json").exists())

    def test_las_demás_entradas_no_se_tocan(self):
        """Olvidar va SOLO por la ruta recién comprobada: reapuntar una no
        puede llevarse otras."""
        presente = self.mkv(self.library_dir / "Otra.mkv", "otra")
        fp_otra = self.analizado(presente)
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune")
        self.analizado(viejo)
        self.mover(viejo, self.library_dir / "Dune.mkv")
        cuerpo = self.pedir()
        self.assertEqual(sorted(t["nombre"] for t in cuerpo["recientes"]),
                         ["Dune.mkv", "Otra.mkv"])
        self.assertTrue((self.audits / f"{fp_otra['sha256_1mb']}.json").exists())
        self.assertEqual([r["nombre"] for r in cuerpo["reubicados"]], ["Dune.mkv"],
                         "la que no se movió no se busca ni se anuncia")

    def test_una_entrada_que_no_se_deja_mirar_no_concluye(self):
        """Una entrada de la carpeta cuyo `stat` falla (un fichero que se
        borra mientras se lista) puede ser justo la que se busca."""
        (self.library_dir / "Dune.mkv").write_bytes(b"x")

        class _Rota:
            name, path = "Dune.mkv", str(self.library_dir / "Dune.mkv")

            def is_dir(self, follow_symlinks=True):
                raise FileNotFoundError(self.path)

        class _Listado:
            def __enter__(self):
                return iter([_Rota()])

            def __exit__(self, *a):
                return False

        original = self.reubicar.os.scandir
        self.reubicar.os.scandir = lambda carpeta: _Listado()
        try:
            hallados, completo = self.reubicar._mkv_por_tamano(
                [str(self.library_dir)], {1}, 5.0)
        finally:
            self.reubicar.os.scandir = original
        self.assertEqual((hallados, completo), ({}, False))

    def test_una_carpeta_que_no_se_deja_leer_no_concluye(self):
        """Lo que no se ha podido mirar puede estar justo ahí."""
        if os.geteuid() == 0:
            self.skipTest("root lee cualquier carpeta")
        viejo = self.mkv(self.output_dir / "Borrada.mkv", "borrada")
        fp = self.analizado(viejo)
        viejo.unlink()
        cerrada = self.library_dir / "cerrada"
        cerrada.mkdir()
        cerrada.chmod(0)
        self.addCleanup(cerrada.chmod, 0o755)
        t = self.tarjeta(self.pedir(), "Borrada.mkv")
        self.assertFalse(t["existe"])
        self.assertFalse(t["buscado"], "con una carpeta sin mirar no se afirma que no está")
        self.assertFalse(self.reubicar.buscado_sin_exito(fp["sha256_1mb"]))

    def test_sin_nada_que_falte_no_se_lee_ningún_mkv(self):
        """La columna se pide al entrar en la pestaña: lo que está no se
        comprueba —ni un fingerprint, ni un recorrido—."""
        presente = self.mkv(self.library_dir / "Aqui.mkv", "aqui")
        self.analizado(presente)
        leidos = []
        original = self.storage.compute_mkv_fingerprint
        self.storage.compute_mkv_fingerprint = lambda r: leidos.append(r) or original(r)
        self.addCleanup(setattr, self.storage, "compute_mkv_fingerprint", original)
        abiertas = self.contar_recorridos()
        self.pedir()
        self.assertEqual((leidos, abiertas), ([], []))

    def test_dev_mode_no_toca_el_disco(self):
        from routers import tab2 as t2
        original = t2.DEV_MODE
        t2.DEV_MODE = True
        self.addCleanup(setattr, t2, "DEV_MODE", original)
        abiertas = self.contar_recorridos()
        cuerpo = self.pedir()
        self.assertEqual(cuerpo["reubicados"], [])
        self.assertEqual(abiertas, [])


class TestLoQueNoApareceNoSeBuscaEnCadaPetición(ReubicarCase):

    def setUp(self):
        super().setUp()
        self.borrado = self.mkv(self.output_dir / "Borrada.mkv", "borrada")
        self.fp = self.analizado(self.borrado)
        self.borrado.unlink()
        self.mkv(self.library_dir / "sub" / "Otra.mkv", "otra")

    def test_la_segunda_petición_no_recorre_nada(self):
        abiertas = self.contar_recorridos()
        self.pedir()
        primera = len(abiertas)
        self.assertGreater(primera, 0, "la primera vez se busca")
        self.pedir()
        self.assertEqual(len(abiertas), primera)

    def test_volver_a_buscar_se_salta_la_memoria(self):
        abiertas = self.contar_recorridos()
        self.pedir()
        primera = len(abiertas)
        self.pedir(buscar="true")
        self.assertGreater(len(abiertas), primera)

    def test_pasado_el_plazo_se_vuelve_a_buscar(self):
        abiertas = self.contar_recorridos()
        self.pedir()
        primera = len(abiertas)
        original = self.reubicar.REBUSCAR_TRAS_S
        self.reubicar.REBUSCAR_TRAS_S = 0.0
        self.addCleanup(setattr, self.reubicar, "REBUSCAR_TRAS_S", original)
        self.pedir()
        self.assertGreater(len(abiertas), primera)

    def test_uno_que_acaba_de_desaparecer_se_busca_aunque_otro_esté_en_la_memoria(self):
        """Es justo el que el usuario acaba de mover: esperar al plazo del
        otro lo dejaría «No encontrado» diez minutos."""
        self.pedir()                                  # «Borrada» entra en la memoria
        movido = self.mkv(self.output_dir / "Recien movido.mkv", "recien")
        self.analizado(movido)
        nuevo = self.mover(movido, self.library_dir / "Recien movido.mkv")
        cuerpo = self.pedir()
        self.assertEqual(self.tarjeta(cuerpo, "Recien movido.mkv")["ruta"], str(nuevo))

    def test_el_porqué_dice_que_se_buscó_en_las_tres_carpetas(self):
        from i18n import t as tr
        t = self.tarjeta(self.pedir(), "Borrada.mkv")
        self.assertEqual(t["relato"]["situacion"], "no_disponible")
        self.assertEqual(t["relato"]["porque"], tr("relato.mkv_porque_no_disponible_buscado"))

    def test_un_recorrido_cortado_no_concluye_nada(self):
        """Lo que no se ha mirado puede estar justo ahí: ni entra en la
        memoria ni el porqué afirma que se buscó."""
        from i18n import t as tr
        original = self.reubicar.TOPE_RECORRIDO_S
        self.reubicar.TOPE_RECORRIDO_S = -1.0
        self.addCleanup(setattr, self.reubicar, "TOPE_RECORRIDO_S", original)
        t = self.tarjeta(self.pedir(), "Borrada.mkv")
        self.assertFalse(t["buscado"])
        self.assertEqual(t["relato"]["porque"], tr("relato.mkv_porque_no_disponible"))
        self.assertFalse(self.reubicar.buscado_sin_exito(self.fp["sha256_1mb"]))


class TestLimpiezaNoOfreceBorrarLoQueSoloSeMovió(ReubicarCase):
    """⚙︎ Configuración → Limpieza: «Cache MKV huérfano» se ofrecía borrar
    como seguro con solo que la ruta guardada no existiera — o sea, el
    análisis de todo MKV movido a la biblioteca."""

    def huerfanos(self) -> list[str]:
        r = self.client.get("/api/cleanup/scan")
        self.assertEqual(r.status_code, 200, r.text)
        return [Path(i["path"]).name for i in r.json()["items"]
                if i["category"] == "mkv_cache_orphan"]

    def test_un_mkv_movido_no_es_huérfano_y_el_escaneo_no_escribe(self):
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune")
        fp = self.analizado(viejo)
        self.mover(viejo, self.library_dir / "Dune.mkv")
        antes = (self.audits / f"{fp['sha256_1mb']}.json").read_bytes()
        self.assertEqual(self.huerfanos(), [])
        self.assertEqual((self.audits / f"{fp['sha256_1mb']}.json").read_bytes(), antes,
                         "el escaneo es de solo lectura: reapunta la columna, no él")

    def test_uno_que_no_está_en_ninguna_carpeta_sí(self):
        viejo = self.mkv(self.output_dir / "Borrada.mkv", "borrada")
        fp = self.analizado(viejo)
        viejo.unlink()
        self.assertEqual(self.huerfanos(), [f"{fp['sha256_1mb']}.json"])

    def test_si_la_búsqueda_no_llega_al_final_no_se_ofrece_borrar(self):
        viejo = self.mkv(self.output_dir / "Borrada.mkv", "borrada")
        self.analizado(viejo)
        viejo.unlink()
        original = self.reubicar.TOPE_RECORRIDO_S
        self.reubicar.TOPE_RECORRIDO_S = -1.0
        self.addCleanup(setattr, self.reubicar, "TOPE_RECORRIDO_S", original)
        self.assertEqual(self.huerfanos(), [])

    def test_busca_aunque_la_columna_acabe_de_buscar(self):
        """La columna recuerda lo que no encontró diez minutos; Limpieza no
        puede fiarse de eso, porque decide qué se borra. El MKV que vuelve a
        aparecer dentro del plazo no se ofrece borrar."""
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune")
        fp = self.analizado(viejo)
        guardado = self.mover(viejo, self.fuera / "Dune.mkv")
        self.pedir()                                  # la columna no lo encuentra
        self.assertTrue(self.reubicar.buscado_sin_exito(fp["sha256_1mb"]))
        self.mover(guardado, self.library_dir / "Dune.mkv")
        self.assertEqual(self.huerfanos(), [])

    def test_lo_que_la_columna_acaba_de_buscar_sin_éxito_se_ofrece(self):
        """La memoria de la columna dice «no lo busques otra vez», no «no lo
        ofrezcas»: Limpieza vuelve a buscarlo y, si concluye, lo ofrece."""
        viejo = self.mkv(self.output_dir / "Borrada.mkv", "borrada")
        fp = self.analizado(viejo)
        viejo.unlink()
        self.pedir()
        self.assertTrue(self.reubicar.buscado_sin_exito(fp["sha256_1mb"]))
        self.assertEqual(self.huerfanos(), [f"{fp['sha256_1mb']}.json"])

    def test_lo_que_desaparece_después_de_buscar_no_es_huérfano(self):
        """Estaba en su ruta cuando se buscó —así que no se buscó— y se movió
        antes de que el escaneo mirara su ruta: «no está en lo encontrado» no
        es «se buscó y no está» (revisión de Codex del 2026-10-27)."""
        presente = self.mkv(self.output_dir / "Dune.mkv", "dune")
        self.analizado(presente)
        original = self.reubicar.buscar

        def _y_se_mueve(entradas, **kw):
            res = original(entradas, **kw)
            self.mover(presente, self.library_dir / "Dune.mkv")
            return res

        self.reubicar.buscar = _y_se_mueve
        self.addCleanup(setattr, self.reubicar, "buscar", original)
        self.assertEqual(self.huerfanos(), [])

    def test_borrar_no_se_lleva_uno_que_se_movió(self):
        """El borrado vuelve a escanear antes de tocar nada: una petición con
        la ruta de un análisis que se puede reubicar no lo borra."""
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune")
        fp = self.analizado(viejo)
        self.mover(viejo, self.library_dir / "Dune.mkv")
        cache = self.audits / f"{fp['sha256_1mb']}.json"
        r = self.client.post("/api/cleanup/execute", json={"paths": [str(cache)]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["deleted"], [])
        self.assertTrue(cache.exists())

    def test_un_movido_con_versión_vieja_sigue_saliendo_por_su_versión(self):
        """Solo el «huérfano» depende de dónde esté el fichero: un análisis
        caducado se ofrece borrar esté donde esté."""
        viejo = self.mkv(self.output_dir / "Dune.mkv", "dune")
        fp = self.analizado(viejo)
        guardado = self.cache(fp)
        guardado["versions"]["basic"] = self.v_basic - 1
        (self.audits / f"{fp['sha256_1mb']}.json").write_text(json.dumps(guardado),
                                                              encoding="utf-8")
        self.mover(viejo, self.library_dir / "Dune.mkv")
        r = self.client.get("/api/cleanup/scan")
        categorias = [i["category"] for i in r.json()["items"]
                      if Path(i["path"]).name == f"{fp['sha256_1mb']}.json"]
        self.assertEqual(categorias, ["mkv_cache_stale_version"])


class TestLasEscriturasDeLaCachéNoSePisan(ReubicarCase):
    """Escribir una entrada es leer el JSON entero, cambiarlo y escribirlo: dos
    a la vez se pisan, y el bloque que escribió la primera se pierde. Desde
    que la columna reapunta al listar, en un thread, se cruza con la apertura
    y con el análisis extendido de la misma entrada (revisión de Codex del
    2026-10-27). Lo que se comprueba es que cada escritura de `mkv_audits/`
    ocurre con el cerrojo puesto."""

    def setUp(self):
        super().setUp()
        self.cerrado = []
        original = self.storage._atomic_write_json

        def _espia(ruta, texto):
            if Path(ruta).parent == self.audits:
                self.cerrado.append(self.storage._MKV_CACHE_LOCK.locked())
            return original(ruta, texto)

        self.storage._atomic_write_json = _espia
        self.addCleanup(setattr, self.storage, "_atomic_write_json", original)
        self.mkv_ = self.mkv(self.library_dir / "Dune.mkv", "dune")
        self.fp = self.analizado(self.mkv_)

    def test_al_reapuntar(self):
        nuevo = self.mover(self.mkv_, self.library_dir / "Archivo" / "Dune.mkv")
        fp = self.storage.compute_mkv_fingerprint(str(nuevo))
        self.assertTrue(self.storage.repoint_mkv_cache(fp, str(nuevo)))
        self.assertEqual(self.cerrado, [True])

    def test_al_escribir_el_básico_y_el_extendido(self):
        self.storage.write_mkv_cache_basic(self.fp, self.v_basic, self.v_quality,
                                           {"file_path": str(self.mkv_)})
        self.storage.write_mkv_cache_quality(self.fp, self.v_basic, self.v_quality,
                                             {"quality_total_frames_rpu": 1})
        self.assertEqual(self.cerrado, [True, True])


class TestLaAperturaDiceQuéContenidoTieneAbierto(ReubicarCase):
    """La sub-pestaña abierta sigue a su MKV por el contenido: para eso la
    respuesta de abrir lleva la huella, también cuando sale de la caché."""

    def setUp(self):
        super().setUp()
        from cmv40_harness import FakeToolbox, write_artifacts
        self.tb = FakeToolbox(self.tmp / "tb").install()
        self.addCleanup(self.tb.uninstall)
        self.abierto = self.output_dir / "Peli (2024).mkv"
        write_artifacts(self.output_dir, self.abierto.name)
        self.tb.define_mkv(self.abierto.name)
        self.tb.define_mediainfo(self.abierto.name)

    def test_lleva_la_huella_al_analizar_y_desde_la_caché(self):
        esperada = self.storage.compute_mkv_fingerprint(str(self.abierto))["sha256_1mb"]
        for vez in ("analizado", "desde la caché"):
            with self.subTest(vez=vez):
                r = self.client.post("/api/mkv/analyze", json={"file_path": str(self.abierto)})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["huella"], esperada)


if __name__ == "__main__":
    unittest.main()
