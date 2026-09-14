"""Un rip que muere con el contenedor deja 24 GB ilegibles con el nombre bueno.

Caso real del 12-sep-2026: un deploy en mitad de la cola de Juego de Tronos
recreó el contenedor entre `S05E05` y `S05E06`. El recovery de arranque hizo su
parte —las dos sesiones volvieron a `pending` y la cola siguió con E07-E10—
pero **no tocó el fichero**: `S05E05` se quedó en /mnt/output con **24,7 GB y
el nombre definitivo**, al lado de los episodios buenos, y `mkvmerge -J` no le
encuentra ni una pista. En un listado no hay forma de distinguirlo de un rip
terminado; en Plex es un episodio roto.

`_limpiar_parcial` de las fases no cubre esto: solo corre cuando mkvmerge
falla, no cuando al proceso lo mata el reinicio. El simétrico que sí lo hacía
era el de Tab 2 (`recuperar_apply_interrumpido`, que borra la copia a medias),
y allí se borra sin preguntar porque el destino lo creó la copia. Aquí no se
puede: una sesión se re-ejecuta sobre el mismo nombre, así que el fichero puede
ser el resultado BUENO de la pasada anterior. De ahí que el criterio sea si el
MKV se puede usar, y no si existe.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_parcial_al_reiniciar -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import FakeToolbox  # noqa: E402

# Las dos pistas mínimas de un MKV terminado de Tab 1. Lo que define a un
# parcial es que NO tenga la de vídeo.
PISTAS_OK = [
    {"type": "video", "codec": "HEVC/H.265/MPEG-H", "dimensions": "3840x2160"},
    {"type": "audio", "codec": "TrueHD Atmos", "language": "spa"},
]


class RecoveryCase(unittest.TestCase):
    """`/config` y `/mnt/output` aislados, y mkvmerge falso en el PATH."""

    def setUp(self):
        import paths
        import storage
        from queue_manager import queue_manager

        self.tmp = Path(tempfile.mkdtemp(prefix="parcial_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.output = self.tmp / "output"
        self.output.mkdir()

        orig = (storage.CONFIG_DIR, paths.CONFIG_DIR, paths.OUTPUT_DIR_MKV)
        storage.CONFIG_DIR = paths.CONFIG_DIR = self.tmp / "config"
        storage.CONFIG_DIR.mkdir()
        paths.OUTPUT_DIR_MKV = self.output

        def _restaurar_dirs():
            storage.CONFIG_DIR, paths.CONFIG_DIR, paths.OUTPUT_DIR_MKV = orig
        self.addCleanup(_restaurar_dirs)

        cache = getattr(storage, "_sessions_summary_by_file", None)
        if isinstance(cache, dict):
            cache.clear()

        # El recovery pregunta a la cola por lo `queued`; que no vea la real.
        self.cola = queue_manager
        estado = (list(self.cola._queue), self.cola._running)
        self.cola._queue.clear()
        self.cola._running = None

        def _restaurar_cola():
            self.cola._queue[:] = estado[0]
            self.cola._running = estado[1]
        self.addCleanup(_restaurar_cola)

        self.tb = FakeToolbox(self.tmp).install()
        self.addCleanup(self.tb.uninstall)

    def sesion(self, sid, mkv_name, status="running"):
        import storage
        from models import Session
        s = Session(id=sid, iso_path=f"/mnt/isos/{sid}.iso",
                    mkv_name=mkv_name, status=status)
        storage.save_session(s)
        return s

    def mkv(self, nombre_relativo, *, pistas, tam=4096) -> Path:
        """Escribe el fichero y declara lo que mkvmerge contestará sobre él."""
        f = self.output / nombre_relativo
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\x00" * tam)
        self.tb.define_mkv(f.name, tracks=pistas)
        return f

    def recuperar(self):
        from routers import tab1
        tab1.recuperar_sesiones_interrumpidas()


class TestElParcialSeBorra(RecoveryCase):

    def test_un_mkv_sin_pistas_es_un_mux_a_medias_y_se_borra(self):
        import storage
        f = self.mkv("S05E05.mkv", pistas=[])
        self.sesion("e05", "S05E05.mkv")

        self.recuperar()

        self.assertFalse(f.exists(), "el MKV a medias sigue en /mnt/output")
        s = storage.load_session("e05")
        self.assertEqual(s.status, "pending")
        self.assertIn("borrado", (s.error_message or "").lower())

    def test_el_aviso_dice_cuanto_se_ha_liberado(self):
        import storage
        self.mkv("peli.mkv", pistas=[], tam=3_000_000_000)
        self.sesion("peli", "peli.mkv")

        self.recuperar()

        self.assertIn("3.00 GB", storage.load_session("peli").error_message)

    def test_tambien_con_la_ruta_de_serie_en_subdirectorios(self):
        """`mkv_name` de una serie trae `Serie/Season NN/` dentro."""
        rel = "Juego de tronos (2011)/Season 05/S05E05 - Matad al chico.mkv"
        f = self.mkv(rel, pistas=[])
        self.sesion("got_e05", rel)

        self.recuperar()

        self.assertFalse(f.exists())


class TestLoQueNoSeToca(RecoveryCase):

    def test_un_mkv_completo_no_se_borra_nunca(self):
        """Re-ejecutar escribe sobre el mismo nombre: el fichero de una pasada
        anterior que sí terminó no es nuestro parcial."""
        import storage
        f = self.mkv("buena.mkv", pistas=PISTAS_OK)
        self.sesion("buena", "buena.mkv")

        self.recuperar()

        self.assertTrue(f.exists(), "se ha borrado un MKV utilizable")
        self.assertIn("no llegó a validarse",
                      storage.load_session("buena").error_message)

    def test_si_mkvmerge_no_contesta_no_se_borra(self):
        """Sin respuesta no hay veredicto: `None` no es `False`."""
        import storage
        f = self.mkv("dudosa.mkv", pistas=[])
        # Sale con rc != 0 y sin nada en stdout: es lo que se ve cuando el
        # binario no está o la identificación revienta.
        self.tb.fail_when_arg("mkvmerge", "dudosa.mkv")
        self.sesion("dudosa", "dudosa.mkv")

        self.recuperar()

        self.assertTrue(f.exists(), "se ha borrado sin poder comprobarlo")
        self.assertEqual(storage.load_session("dudosa").status, "pending")

    def test_lo_que_estaba_esperando_turno_no_tiene_parcial_que_borrar(self):
        """`queued` no había empezado: su MKV, si existe, es de otra pasada."""
        from queue_manager import TIPO_RIP, TrabajoEnCola
        f = self.mkv("encolada.mkv", pistas=[])
        self.sesion("encolada", "encolada.mkv", status="queued")
        self.cola._queue.append(TrabajoEnCola(
            tab="rip", tipo=TIPO_RIP, clave="encolada", que="rip"))

        self.recuperar()

        self.assertTrue(f.exists())

    def test_una_sesion_sin_mkv_en_disco_solo_deja_el_aviso_de_siempre(self):
        import storage
        self.sesion("sin_fichero", "no_existe.mkv")

        self.recuperar()

        s = storage.load_session("sin_fichero")
        self.assertEqual(s.status, "pending")
        self.assertEqual(s.error_message,
                         "Sesión interrumpida por reinicio del servidor")

    def test_un_mkv_name_que_se_sale_de_output_no_se_toca(self):
        """`mkv_name` es editable por el usuario, y esto acaba en un `unlink`."""
        fuera = self.tmp / "biblioteca.mkv"
        fuera.write_bytes(b"\x00" * 128)
        self.tb.define_mkv(fuera.name, tracks=[])
        self.sesion("traviesa", "../biblioteca.mkv")

        self.recuperar()

        self.assertTrue(fuera.exists(), "ha borrado fuera de /mnt/output")


if __name__ == "__main__":
    unittest.main()
