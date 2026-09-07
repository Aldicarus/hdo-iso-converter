"""Los `logger.info` de la aplicación tienen que salir por `docker logs`.

No había ninguna configuración de logging. El logger raíz se quedaba en su
default —WARNING y sin handler— así que **los 65 `logger.info` del código
nunca se han visto**. Lo único que salía eran las líneas de acceso de uvicorn,
que configura sus propios loggers aparte, y nuestros `warning`; de ahí que
haya mensajes claramente informativos escritos como `warning` para poder
verlos, como el `[QualityAudit] START`.

Se descubrió midiendo: la contención del registro de `workload` se lee con un
`grep "[workload]"` del log del contenedor, se desplegó, y el grep no devolvía
nada aunque el trabajo sí se había registrado.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_logging_visible -v
"""
import io
import logging
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))


class TestElLoggingEstaConfigurado(unittest.TestCase):
    """Importar `main` deja el logging listo — no hay otro sitio donde
    hacerlo: el contenedor arranca con `python3 -m uvicorn main:app`, sin
    fichero de configuración ni flags."""

    @classmethod
    def setUpClass(cls):
        import main            # noqa: F401  (el import es lo que configura)
        cls.raiz = logging.getLogger()

    def test_el_nivel_del_raiz_llega_a_info(self):
        self.assertLessEqual(self.raiz.level, logging.INFO,
                             "con el raíz en WARNING, ningún logger.info sale")

    def test_hay_un_handler(self):
        """Sin handler, `logging` usa el de último recurso, que escribe a
        stderr **solo los WARNING**."""
        self.assertTrue(self.raiz.handlers)

    def test_httpx_no_grita(self):
        """Una línea por petición a TMDb, Drive y la hoja taparía lo nuestro."""
        for ruidoso in ("httpx", "httpcore"):
            self.assertGreaterEqual(logging.getLogger(ruidoso).level,
                                    logging.WARNING, ruidoso)


class TestLoQueSeQueriaVer(unittest.TestCase):
    """El caso concreto que lo destapó, ejecutado."""

    def setUp(self):
        import main            # noqa: F401
        self.buf = io.StringIO()
        self.h = logging.StreamHandler(self.buf)
        self.h.setLevel(logging.INFO)
        logging.getLogger().addHandler(self.h)
        self.addCleanup(logging.getLogger().removeHandler, self.h)

    def test_el_registro_de_workload_se_puede_grepear(self):
        import workload
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        workload.registrar("k1", workload.TAB_RIP, "rip de Peli (2024)")
        workload.liberar("k1")
        salida = self.buf.getvalue()
        self.assertIn("[workload]", salida,
                      "sin esto no hay forma de medir la contención real")
        self.assertIn("arranca", salida)
        self.assertIn("termina", salida)

    def test_y_lleva_la_clase_para_poder_separarlas(self):
        import workload
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        workload.registrar("k1", workload.TAB_MKV, "apertura de un MKV",
                           workload.CLASE_INTERACTIVO)
        workload.liberar("k1")
        self.assertIn("[interactivo]", self.buf.getvalue())

    def test_un_logger_cualquiera_de_la_app_tambien_se_ve(self):
        logging.getLogger("phases.phase_a").info("Paso 1/4: identificando")
        self.assertIn("Paso 1/4", self.buf.getvalue())


if __name__ == "__main__":
    unittest.main()
