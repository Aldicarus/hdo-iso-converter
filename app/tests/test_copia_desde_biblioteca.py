"""La copia de un MKV de la biblioteca a /mnt/output, ejecutada de verdad.

`tab2` estaba al 36 % de cobertura (auditoría de la suite del 2026-10-17) y de
la copia solo se probaba que se ENCOLA y que deja su línea en el historial con
la copia sustituida por un `raise`. Nada ejecutaba la copia: ni que el destino
sale entero, ni que mkvpropedit edita la COPIA y no la biblioteca, ni la
cancelación cooperativa a mitad, ni el recovery que borra el parcial tras un
reinicio — y ese último es el que puede borrar el fichero bueno si se equivoca.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_copia_desde_biblioteca -v
"""
import asyncio
import io
import json
import pathlib
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
from cmv40_harness import FakeToolbox  # noqa: E402

MB = 1024 * 1024


class CopiaCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        from routers import tab2
        import queue_manager as qm
        import workload
        self.tab2, self.qm = tab2, qm
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        tab2._mkv_apply_cancel["requested"] = False
        self.addCleanup(tab2._mkv_apply_cancel.update, {"requested": False})
        self.tb = FakeToolbox(self.tmp / "tb").install()
        self.addCleanup(self.tb.uninstall)
        # Tres trozos de 8 MB y pico, con contenido distinto en cada uno: una
        # copia que se saltara o repitiera un trozo daría el mismo tamaño.
        self.contenido = b"".join(bytes([i]) * (8 * MB) for i in range(3)) + b"fin"
        self.src = self.library_dir / "Peli (2024).mkv"
        self.src.write_bytes(self.contenido)
        self.dst = self.output_dir / self.src.name

    def _pedir(self, **extra):
        body = {"file_path": str(self.src), "copy_to_output": True,
                "audio_tracks": [{"id": 1, "name": "Castellano TrueHD Atmos 7.1"}],
                "subtitle_tracks": [], **extra}
        return self.client.post("/api/mkv/apply", json=body)

    def _turno(self):
        """Lo que haría la cola al llegarle el turno."""
        [(tipo, clave, datos, _)] = [t for t in self.trabajos_encolados
                                     if t[0] == self.qm.TIPO_COPIA_BIBLIOTECA]
        return self.qm.TrabajoEnCola(tab="mkv", tipo=tipo, clave=clave, datos=datos)

    async def _correr(self, trabajo, durante=None, *, esperar_liberacion=False):
        tarea = asyncio.create_task(self.tab2._runner_copia_biblioteca(trabajo))
        if durante:
            await durante()
        await asyncio.wait_for(tarea, 30)
        # El runner deja una tarea que libera `active` pasados unos segundos.
        # Por defecto no se espera (se cancelaría al cerrar el loop); el test
        # de la liberación la pone a cero y la espera.
        pendientes = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if esperar_liberacion:
            await asyncio.wait_for(asyncio.gather(*pendientes), 10)
        for t in pendientes:
            t.cancel()

    def _en_disco(self):
        return json.loads(self.tab2._MKV_APPLY_STATE_FILE.read_text())["step"]

    def _lenta(self, *, falla_en=None):
        """La lectura del ORIGEN tarda 0,2 s por trozo, para que la
        cancelación llegue a mitad de la copia y no después. Con `falla_en`,
        la lectura número N lanza un error de E/S."""
        original = pathlib.Path.open
        src = self.src
        lecturas = [0]

        class _Lento(io.BufferedReader):
            def read(self, n=-1):
                import time
                time.sleep(0.2)
                lecturas[0] += 1
                if falla_en and lecturas[0] >= falla_en:
                    raise OSError(5, "Input/output error")
                return super().read(n)

        def _open(p, mode="r", *a, **k):
            if p == src and "r" in mode and "b" in mode:
                return _Lento(io.FileIO(str(p), "rb"))
            return original(p, mode, *a, **k)
        pathlib.Path.open = _open
        self.addCleanup(setattr, pathlib.Path, "open", original)


class TestLaCopiaCompleta(CopiaCase):

    def test_copia_entera_y_edita_la_copia_no_la_biblioteca(self):
        r = self._pedir()
        self.assertEqual(r.status_code, 200, r.text)
        asyncio.run(self._correr(self._turno()))

        st = self.tab2._mkv_apply_state
        self.assertEqual(st["step"], "done", st.get("error"))
        self.assertEqual(self.dst.read_bytes(), self.contenido, "la copia no es idéntica")
        self.assertEqual(self.src.read_bytes(), self.contenido, "se ha tocado la biblioteca")
        [prop] = self.tb.find("mkvpropedit")
        self.assertEqual(Path(prop.argv[0]), self.dst, "mkvpropedit no edita la copia")
        self.assertIn("name=Castellano TrueHD Atmos 7.1", prop.argv)
        self.assertEqual(st["result"]["new_file_path"], str(self.dst))
        self.assertTrue(st["result"]["copied_from_library"])
        persistido = json.loads(self.tab2._MKV_APPLY_STATE_FILE.read_text())
        self.assertEqual(persistido["step"], "done", "el estado no se persiste")

    def test_si_mkvpropedit_falla_es_un_error_y_no_un_done(self):
        self.tb.fail("mkvpropedit", "*", rc=2, stderr="Error: no se puede escribir\n")
        self._pedir()
        asyncio.run(self._correr(self._turno()))
        self.assertEqual(self.tab2._mkv_apply_state["step"], "error")
        self.assertEqual(self._en_disco(), "error", "la transición no se persiste")

    def test_un_error_de_lectura_a_mitad_borra_el_parcial(self):
        self._lenta(falla_en=2)
        self._pedir()
        asyncio.run(self._correr(self._turno()))
        st = self.tab2._mkv_apply_state
        self.assertEqual(st["step"], "error")
        self.assertFalse(self.dst.exists(), "queda la copia a medias con el nombre definitivo")
        self.assertFalse((st.get("result") or {}).get("new_file_path"))
        with open(self.src, "rb") as f:     # `Path.open` sigue parcheado
            self.assertEqual(f.read(), self.contenido)

    def test_un_destino_que_aparece_mientras_espera_turno_no_se_toca(self):
        """El endpoint comprueba el destino al encolar, y la cola puede
        despachar cuarenta minutos después."""
        self._pedir()
        self.dst.write_bytes(b"otro fichero")
        asyncio.run(self._correr(self._turno()))
        self.assertEqual(self.tab2._mkv_apply_state["step"], "error")
        self.assertEqual(self.dst.read_bytes(), b"otro fichero", "se ha pisado o borrado un fichero ajeno")
        self.assertFalse(self.tb.ran("mkvpropedit"))

    def test_al_terminar_se_libera_para_el_siguiente(self):
        self.tab2._SEGUNDOS_ANTES_DE_LIBERAR, orig = 0, self.tab2._SEGUNDOS_ANTES_DE_LIBERAR
        self.addCleanup(setattr, self.tab2, "_SEGUNDOS_ANTES_DE_LIBERAR", orig)
        self._pedir()
        asyncio.run(self._correr(self._turno(), esperar_liberacion=True))
        self.assertFalse(self.tab2._mkv_apply_state["active"])
        self.assertFalse(json.loads(self.tab2._MKV_APPLY_STATE_FILE.read_text())["active"])
        otro = self.library_dir / "Otra (2023).mkv"
        otro.write_bytes(b"y" * 1024)
        r = self.client.post("/api/mkv/apply", json={
            "file_path": str(otro), "copy_to_output": True,
            "audio_tracks": [], "subtitle_tracks": []})
        self.assertEqual(r.status_code, 200, r.text)

    def test_y_la_copia_entera_se_queda_y_se_dice_donde(self):
        """La copia terminó: el fichero es válido, solo le faltan los cambios.
        Si no se dice dónde está, la interfaz se queda en la biblioteca y
        reintentar da 409 «ya existe un MKV con ese nombre»."""
        self.tb.fail("mkvpropedit", "*", rc=2, stderr="Error: no se puede escribir\n")
        self._pedir()
        asyncio.run(self._correr(self._turno()))
        self.assertEqual(self.dst.read_bytes(), self.contenido)
        res = self.tab2._mkv_apply_state["result"]
        self.assertEqual((res["ok"], res["new_file_path"], res["copied_from_library"]),
                         (False, str(self.dst), True))

    def test_pero_si_falla_la_copia_no_se_anuncia_ninguna_copia(self):
        self._pedir()

        async def _revienta(*a, **k):
            raise RuntimeError("disco lleno")
        orig = self.tab2._mkv_copy_to_output_with_progress
        self.tab2._mkv_copy_to_output_with_progress = _revienta
        self.addCleanup(setattr, self.tab2, "_mkv_copy_to_output_with_progress", orig)
        asyncio.run(self._correr(self._turno()))
        self.assertEqual(self.tab2._mkv_apply_state["step"], "error")
        self.assertFalse((self.tab2._mkv_apply_state.get("result") or {}).get("new_file_path"))


class TestLoQueSeRechazaAntesDeEncolar(CopiaCase):

    def test_sin_confirmar_la_copia_es_409_y_no_se_encola_nada(self):
        r = self._pedir(copy_to_output=False)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.trabajos_encolados, [])

    def test_un_destino_que_ya_existe_no_se_sobrescribe(self):
        self.dst.write_bytes(b"el bueno")
        r = self._pedir()
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.dst.read_bytes(), b"el bueno")
        self.assertEqual(self.trabajos_encolados, [])

    def test_con_otra_copia_en_marcha_es_409(self):
        self.tab2._mkv_apply_state.update({"active": True, "step": "copying"})
        r = self._pedir()
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.trabajos_encolados, [])


class TestCancelarLaCopia(CopiaCase):

    def test_a_mitad_borra_el_parcial_y_acaba_cancelada(self):
        self._lenta()
        self._pedir()
        trabajo = self._turno()

        async def _cancelar_cuando_copie():
            for _ in range(300):
                st = self.tab2._mkv_apply_state
                if st.get("step") == "copying" and st.get("bytes_copied", 0) > 0:
                    break
                await asyncio.sleep(0.02)
            self.assertGreater(self.tab2._mkv_apply_state["bytes_copied"], 0)
            self.assertLess(self.tab2._mkv_apply_state["bytes_copied"], len(self.contenido),
                            "la copia ya había terminado: no se prueba nada")
            self.en_disco_al_cancelar = self._en_disco()
            r = await self.tab2.mkv_apply_cancel()
            self.assertEqual(r, {"ok": True})

        asyncio.run(self._correr(trabajo, _cancelar_cuando_copie))
        self.assertEqual(self.tab2._mkv_apply_state["step"], "cancelled")
        self.assertEqual(self.en_disco_al_cancelar, "copying",
                         "a mitad de la copia el disco tiene que decir «copying»: es lo que lee el recovery")
        self.assertEqual(self._en_disco(), "cancelled", "la transición no se persiste")
        self.assertFalse(self.dst.exists(), "queda la copia a medias en /mnt/output")
        self.assertEqual(self.src.read_bytes(), self.contenido)
        self.assertFalse(self.tb.ran("mkvpropedit"), "se ha editado tras cancelar")

    def test_sin_trabajo_o_fuera_de_la_copia_no_hace_nada(self):
        self.assertEqual(asyncio.run(self.tab2.mkv_apply_cancel()),
                         {"ok": False, "reason": "no_active_job"})
        self.tab2._mkv_apply_state.update({"active": True, "step": "applying"})
        self.assertEqual(asyncio.run(self.tab2.mkv_apply_cancel()),
                         {"ok": False, "reason": "not_in_copying_step"})
        self.assertFalse(self.tab2._mkv_apply_cancel["requested"])


class TestElReinicioAMitad(CopiaCase):
    """`recuperar_apply_interrumpido` corre al arrancar el contenedor."""

    def _persistido(self, **estado):
        self.tab2._MKV_APPLY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.tab2._MKV_APPLY_STATE_FILE.write_text(json.dumps({
            "active": True, "src_path": str(self.src), "dst_path": str(self.dst),
            "file_name": self.src.name, **estado}))

    def test_una_copia_cortada_borra_el_parcial_y_lo_dice(self):
        self.dst.write_bytes(self.contenido[: 8 * MB])
        self._persistido(step="copying")
        self.tab2.recuperar_apply_interrumpido()
        self.assertFalse(self.dst.exists())
        self.assertEqual(self.src.read_bytes(), self.contenido)
        st = self.tab2._mkv_apply_state
        self.assertEqual((st["active"], st["step"]), (False, "error"))
        self.assertIn(self.src.name, st["error"])
        self.assertEqual(json.loads(self.tab2._MKV_APPLY_STATE_FILE.read_text())["step"], "error")

    def test_un_trabajo_ya_terminado_NO_pierde_su_fichero(self):
        """El estado se queda `active` cinco segundos tras terminar (el modal
        cierra en el siguiente poll). Un reinicio en esa ventana deja un
        `done` activo en disco, y ahí el destino es el MKV BUENO."""
        self.dst.write_bytes(self.contenido)
        for terminal in ("done", "cancelled", "error"):
            with self.subTest(terminal=terminal):
                self._persistido(step=terminal)
                self.tab2.recuperar_apply_interrumpido()
                self.assertEqual(self.dst.read_bytes(), self.contenido)
                self.assertFalse(self.tab2._mkv_apply_state["active"])
                self.assertEqual(self.tab2._mkv_apply_state["step"], terminal)

    def test_un_trabajo_que_no_llego_a_escribir_no_borra_nada(self):
        """En la cola no se ha escrito nada: lo que haya en el destino no es
        de esta copia."""
        self.dst.write_bytes(self.contenido)
        for paso in ("en_cola", ""):
            with self.subTest(paso=paso):
                self._persistido(step=paso)
                self.tab2.recuperar_apply_interrumpido()
                self.assertEqual(self.dst.read_bytes(), self.contenido)

    def test_sin_nada_activo_no_toca_nada(self):
        self.dst.write_bytes(self.contenido)
        self._persistido(step="copying", active=False)
        self.tab2.recuperar_apply_interrumpido()
        self.assertTrue(self.dst.exists())


if __name__ == "__main__":
    unittest.main()
