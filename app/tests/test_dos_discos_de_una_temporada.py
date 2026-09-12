"""Dos discos de la misma temporada son DOS trabajos, no uno repetido.

Caso real (2026-09-12, Juego de Tronos S04). Los episodios 1-3 están en un
disco y los 4-6 en otro. El usuario lanzó el primero y, sin esperar, el
segundo. Lo que quedó en el `/config` del NAS:

    S4E1 pending · GOT UHD S04 DISC1
    S4E2 pending · GOT UHD S04 DISC1
    S4E3 pending · GOT UHD S04 DISC1
    (E4, E5 y E6 no existen)

El segundo trabajo **se descartó en silencio**, y tres cosas lo taparon:

1. La clave era `serie:{nombre_de_la_serie}:{temporada}`, idéntica para los
   dos discos, y la cola deduplica por `(tipo, clave)`.
2. `encolar` devolvía lo mismo tanto si encolaba como si descartaba, y el
   endpoint contestaba `{"queued": true}` en los dos casos.
3. El progreso es un dict global, así que el modal del segundo leyó el
   `resultado` del primero y lo dio por suyo — de ahí que pareciera que el
   segundo había terminado bien y que faltaban los proyectos del primero.
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_manager as qm  # noqa: E402

DISCO_1 = "GOT UHD S04/GOT UHD S04 DISC1"
DISCO_2 = "GOT UHD S04/GOT UHD S04 DISC2"


def _clave(spath, temporada=4):
    """La misma que usa el endpoint, importada de donde vive."""
    from routers.tab1 import _clave_de_serie
    return _clave_de_serie(spath, temporada)


def _trabajo(spath, temporada=4):
    return qm.TrabajoEnCola(
        tab="rip", tipo=qm.TIPO_SERIE, clave=_clave(spath, temporada),
        sobre=spath, que=f"Análisis de 3 episodios · {spath}",
        datos={"spath": spath},
    )


class _ColaAislada:
    """Un `QueueManager` de usar y tirar, sin persistir ni ejecutar nada."""

    def __enter__(self):
        self.cola = qm.QueueManager()
        self.cola._persist_state = lambda: None
        async def _nada(*a, **k): pass
        self.cola._notify = _nada
        self.cola._process = _nada
        return self.cola

    def __exit__(self, *e):
        pass


class TestLaIdentidadEsElOrigen(unittest.TestCase):

    def test_dos_discos_de_la_misma_temporada_no_comparten_clave(self):
        """El fallo, en una línea: con la clave vieja los dos daban
        `serie:Juego de tronos:4`."""
        self.assertNotEqual(_clave(DISCO_1), _clave(DISCO_2))

    def test_el_mismo_disco_dos_veces_si_comparte_clave(self):
        """Deduplicar sigue haciendo falta: eso es un doble envío."""
        self.assertEqual(_clave(DISCO_1), _clave(DISCO_1))

    def test_la_misma_carpeta_en_temporadas_distintas_tampoco_choca(self):
        self.assertNotEqual(_clave(DISCO_1, 4), _clave(DISCO_1, 5))


class TestElSegundoDiscoSeEncola(unittest.TestCase):

    def test_los_dos_discos_caben_en_la_cola(self):
        async def _t():
            with _ColaAislada() as cola:
                await cola.encolar(_trabajo(DISCO_1))
                await cola.encolar(_trabajo(DISCO_2))
                return [t.datos["spath"] for t in cola._queue]
        self.assertEqual(asyncio.run(_t()), [DISCO_1, DISCO_2],
                         "el segundo disco se ha perdido")

    def test_el_segundo_tambien_se_encola_con_el_primero_YA_CORRIENDO(self):
        """El caso exacto: «antes de que acabara he lanzado el siguiente»."""
        async def _t():
            with _ColaAislada() as cola:
                cola._running = _trabajo(DISCO_1)      # el primero, en marcha
                estado = await cola.encolar(_trabajo(DISCO_2))
                return estado, [t.datos["spath"] for t in cola._queue]
        estado, en_cola = asyncio.run(_t())
        self.assertTrue(estado["encolado"])
        self.assertEqual(en_cola, [DISCO_2])

    def test_el_mismo_disco_repetido_si_se_descarta(self):
        async def _t():
            with _ColaAislada() as cola:
                await cola.encolar(_trabajo(DISCO_1))
                estado = await cola.encolar(_trabajo(DISCO_1))
                return estado, len(cola._queue)
        estado, n = asyncio.run(_t())
        self.assertFalse(estado["encolado"])
        self.assertEqual(n, 1)


class TestLaColaDiceSiEncoloDeVerdad(unittest.TestCase):
    """Sin esto el descarte es mudo, y un trabajo perdido no se distingue de
    uno hecho."""

    def test_encolado_true_cuando_entra(self):
        async def _t():
            with _ColaAislada() as cola:
                return await cola.encolar(_trabajo(DISCO_1))
        self.assertIs(asyncio.run(_t())["encolado"], True)

    def test_encolado_false_cuando_ya_esta_en_la_cola(self):
        async def _t():
            with _ColaAislada() as cola:
                await cola.encolar(_trabajo(DISCO_1))
                return await cola.encolar(_trabajo(DISCO_1))
        self.assertIs(asyncio.run(_t())["encolado"], False)

    def test_encolado_false_cuando_ese_mismo_esta_corriendo(self):
        async def _t():
            with _ColaAislada() as cola:
                cola._running = _trabajo(DISCO_1)
                return await cola.encolar(_trabajo(DISCO_1))
        self.assertIs(asyncio.run(_t())["encolado"], False)

    def test_los_demas_tipos_tambien_lo_informan(self):
        """`encolar` es una sola: si el campo faltara en otra rama, el
        llamador leería `undefined` y lo tomaría por descartado."""
        async def _t():
            with _ColaAislada() as cola:
                t = qm.TrabajoEnCola(tab="rip", tipo=qm.TIPO_RIP,
                                     clave="una_sesion", que="rip")
                return await cola.encolar(t)
        self.assertIn("encolado", asyncio.run(_t()))


class TestElProgresoLlevaElSelloDeSuTrabajo(unittest.TestCase):
    """Un dict global con dos trabajos vivos necesita decir de quién es.

    Es lo que hizo que el modal del segundo disco enseñara los tres proyectos
    del primero y los diera por creados.
    """

    def test_el_runner_sella_el_progreso_con_la_clave(self):
        import routers.tab1 as tab1
        self.assertIn("job", tab1._series_create_progress,
                      "el progreso no lleva sello: dos trabajos de serie se "
                      "confunden entre sí")

    def test_el_frontend_descarta_el_progreso_de_otro_trabajo(self):
        from frontend_sources import js_completo
        self.assertIn("prog.job !== miJob", js_completo(),
                      "el modal aceptaría el resultado de otro trabajo")


if __name__ == "__main__":
    unittest.main()
