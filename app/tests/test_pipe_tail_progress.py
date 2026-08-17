"""La cola de la Fase A: ffmpeg acaba mucho antes que extract-rpu.

Con el pipeline, TODO el progreso salía del stderr de ffmpeg. Pero ffmpeg lee
del disco a cientos de MB/s mientras `extract-rpu` parsea NALs a un core, así
que ffmpeg termina y el pipeline sigue trabajando un buen rato. Medido en el
NAS sobre tres jobs reales, con los mtime de los artefactos como prueba:

    Transformers One   40,7 GB   fase 263 s   cola  71 s   (27 %)
    Nosferatu          63,5 GB   fase 495 s   cola 119 s   (24 %)
    John Wick 4        73,2 GB   fase 519 s   cola 198 s   (38 %)

    John Wick 4: source.hevc mtime 09:02:52 (= último tick de ffmpeg),
                 RPU_source.bin mtime 09:06:08 (= cierre de la fase).

En esa cola no se emitía nada: barra clavada en 99 %, "quedan ~3s" y el log
mudo tres minutos. El usuario lo reportó como "el log tardó casi 5 minutos en
actualizarse al terminar la Fase A".

La señal correcta es cuántos bytes lleva sacados del pipe el consumidor
(`rchar` en /proc), sobre el total del stream. Estos tests inyectan lecturas
sintéticas para poder comprobar el comportamiento sin un fichero de 73 GB.
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))


def _pcts(emitidos):
    """Porcentajes de los marcadores §§PROGRESS§§ emitidos, en orden."""
    out = []
    for m in emitidos:
        if m.startswith("§§PROGRESS§§"):
            out.append(json.loads(m[len("§§PROGRESS§§"):])["pct"])
    return out


def _etas(emitidos):
    out = []
    for m in emitidos:
        if m.startswith("§§PROGRESS§§"):
            d = json.loads(m[len("§§PROGRESS§§"):])
            if "eta_s" in d:
                out.append(d["eta_s"])
    return out


class TestSenalDelConsumidor(unittest.TestCase):
    """`_proc_rchar`: bytes leídos por un proceso, del kernel."""

    def test_pid_inexistente_devuelve_none(self):
        from phases.cmv40_pipeline import _proc_rchar
        self.assertIsNone(_proc_rchar(999999))

    @unittest.skipUnless(Path("/proc/self/io").exists(), "/proc/<pid>/io no existe")
    def test_lee_el_rchar_del_proceso_actual(self):
        from phases.cmv40_pipeline import _proc_rchar
        antes = _proc_rchar(0) if False else _proc_rchar(
            __import__("os").getpid())
        self.assertIsNotNone(antes)
        with tempfile.NamedTemporaryFile("wb", delete=False) as fh:
            fh.write(b"x" * (2 * 1024 * 1024))
            tmp = fh.name
        with open(tmp, "rb") as fh:
            fh.read()
        despues = _proc_rchar(__import__("os").getpid())
        self.assertGreater(despues, antes)
        Path(tmp).unlink()

    def test_un_pipe_no_tiene_posicion_de_lectura(self):
        """Por esto no vale `_ReadProgress` aquí: mira `fdinfo`, y el fd del
        consumidor es un pipe. De ahí que haga falta `rchar`."""
        import os
        r, w = os.pipe()
        try:
            os.write(w, b"hola")
            os.read(r, 4)
            with open(f"/proc/self/fdinfo/{r}") as fh:
                pos = [l for l in fh if l.startswith("pos:")]
            # En Linux la posición de un pipe se queda a 0 pese a haber leído
            if pos:
                self.assertEqual(pos[0].split()[1], "0")
        except FileNotFoundError:
            self.skipTest("/proc no disponible")
        finally:
            os.close(r)
            os.close(w)


class TestColaDelPipeline(unittest.IsolatedAsyncioTestCase):
    """Comportamiento del ticker con lecturas del kernel simuladas."""

    async def _correr(self, lecturas, media_frac_por_tick, hevc_bytes,
                      ticks_hasta_fin_ffmpeg):
        """Ejecuta el pipeline con binarios falsos y `_proc_rchar` inyectado.

        `lecturas` son los bytes acumulados que el consumidor va reportando en
        cada tick; el HEVC se hace crecer en paralelo según `media_frac`.
        """
        import phases.cmv40_pipeline as pipe

        emitidos: list[str] = []

        async def _log(msg):
            emitidos.append(msg)

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        hevc = Path(td.name) / "source.hevc"
        rpu = Path(td.name) / "RPU_source.bin"

        # ffmpeg falso: escribe el HEVC creciendo y emite líneas time= en
        # stderr; termina antes que el consumidor, como el de verdad.
        ff = Path(td.name) / "ff.sh"
        lineas = []
        for i, frac in enumerate(media_frac_por_tick[:ticks_hasta_fin_ffmpeg]):
            n = int(hevc_bytes * frac)
            segs = int(600 * frac)
            lineas.append(
                f"dd if=/dev/zero of={hevc} bs=1 count=0 seek={n} 2>/dev/null\n"
                f'printf "frame=%d fps=100 size=N/A time=00:%02d:%02d.00 '
                f'speed=30x\\r" {i * 1000} {segs // 60} {segs % 60} >&2\n'
                f"sleep 0.25\n")
        ff.write_text("#!/bin/sh\n" + "".join(lineas))
        ff.chmod(0o755)

        # consumidor falso: vive más que ffmpeg y al final escribe el RPU.
        dv = Path(td.name) / "dv.sh"
        dv.write_text(
            "#!/bin/sh\n"
            f"sleep {0.25 * len(media_frac_por_tick) + 0.6}\n"
            f"printf 'rpu' > {rpu}\n")
        dv.chmod(0o755)

        # El consumidor avanza con el reloj, no con el número de consultas:
        # así el test no depende de cuántas veces se le pregunte.
        import time as _t
        t0 = _t.monotonic()
        paso = (0.25 * len(media_frac_por_tick) + 0.6) / len(lecturas)

        def _rchar_falso(pid):
            i = min(int((_t.monotonic() - t0) / paso), len(lecturas) - 1)
            return lecturas[i]

        orig = (pipe.FFMPEG_BIN, pipe.DOVI_TOOL_BIN, pipe._proc_rchar,
                pipe.PIPE_TICK_EVERY_S)
        pipe.FFMPEG_BIN, pipe.DOVI_TOOL_BIN = str(ff), str(dv)
        pipe._proc_rchar = _rchar_falso
        pipe.PIPE_TICK_EVERY_S = 0.05     # el tick real es de 1 s
        try:
            ok = await pipe._ffmpeg_extract_rpu_piped(
                "/x.mkv", rpu, hevc_out=hevc, duration=600.0,
                log_callback=_log, estimated_s=60.0, total_frames=10000,
                label="Extrayendo HEVC + RPU")
        finally:
            (pipe.FFMPEG_BIN, pipe.DOVI_TOOL_BIN, pipe._proc_rchar,
             pipe.PIPE_TICK_EVERY_S) = orig
        return ok, emitidos

    async def test_sigue_emitiendo_progreso_despues_de_que_ffmpeg_acabe(self):
        """Lo que fallaba: cero emisiones entre el último tick de ffmpeg y el
        cierre de la fase."""
        # ffmpeg cubre el vídeo en 4 ticks; el consumidor va por detrás y
        # tarda 4 más en terminar de leer los 10 GB.
        ok, emitidos = await self._correr(
            lecturas=[1_000_000_000, 3_000_000_000, 5_000_000_000, 6_000_000_000,
                      7_000_000_000, 8_000_000_000, 9_000_000_000, 10_000_000_000],
            media_frac_por_tick=[0.25, 0.5, 0.75, 1.0],
            hevc_bytes=10_000_000_000,
            ticks_hasta_fin_ffmpeg=4)
        self.assertTrue(ok)

        # Hay progreso emitido DESPUÉS de la última línea de ffmpeg
        idx_ffmpeg = [i for i, m in enumerate(emitidos) if "leído del vídeo" in m]
        self.assertTrue(idx_ffmpeg, emitidos)
        posteriores = _pcts(emitidos[idx_ffmpeg[-1] + 1:])
        self.assertTrue(posteriores,
                        f"nada emitido tras el fin de ffmpeg: {emitidos}")

    async def test_el_progreso_no_retrocede(self):
        ok, emitidos = await self._correr(
            lecturas=[500_000_000, 2_000_000_000, 2_000_000_000,  # meseta
                      4_000_000_000, 6_000_000_000, 9_000_000_000,
                      10_000_000_000, 10_000_000_000],
            media_frac_por_tick=[0.3, 0.6, 1.0],
            hevc_bytes=10_000_000_000,
            ticks_hasta_fin_ffmpeg=3)
        self.assertTrue(ok)
        pcts = _pcts(emitidos)
        self.assertEqual(pcts, sorted(pcts), pcts)

    async def test_la_barra_mide_al_consumidor_no_a_ffmpeg(self):
        """Con ffmpeg al 100 % del vídeo pero el consumidor a la mitad, la
        barra no puede estar al 99 %: eso es lo que la clavaba."""
        ok, emitidos = await self._correr(
            lecturas=[1_000_000_000, 2_000_000_000, 5_000_000_000,
                      7_000_000_000, 9_000_000_000, 10_000_000_000],
            media_frac_por_tick=[0.5, 1.0],
            hevc_bytes=10_000_000_000,
            ticks_hasta_fin_ffmpeg=2)
        self.assertTrue(ok)
        pcts = _pcts(emitidos)
        # En algún punto ffmpeg ya acabó y la barra seguía por debajo del 90 %
        self.assertTrue(any(p < 90 for p in pcts), pcts)
        # Y acaba llegando arriba
        self.assertGreaterEqual(max(pcts), 95, pcts)

    async def test_escribe_linea_de_texto_durante_la_cola(self):
        """El log no puede quedarse mudo: el usuario mira el log, no la barra
        (que además se pierde si el WS parpadea)."""
        import phases.cmv40_pipeline as pipe
        orig = pipe.PIPE_LOG_EVERY_S
        pipe.PIPE_LOG_EVERY_S = 0.2      # para no alargar el test
        try:
            ok, emitidos = await self._correr(
                lecturas=[2_000_000_000, 4_000_000_000, 6_000_000_000,
                          7_000_000_000, 8_000_000_000, 9_000_000_000,
                          10_000_000_000, 10_000_000_000],
                media_frac_por_tick=[0.5, 1.0],
                hevc_bytes=10_000_000_000,
                ticks_hasta_fin_ffmpeg=2)
        finally:
            pipe.PIPE_LOG_EVERY_S = orig
        self.assertTrue(ok)
        cola = [m for m in emitidos if "extract-rpu procesando" in m]
        self.assertTrue(cola, emitidos)
        # Dice cuánto lleva y cuánto queda, no solo "sigo vivo"
        self.assertIn("de 10,0 GB", cola[0])
        self.assertTrue(any("quedan" in m or "casi listo" in m for m in cola), cola)

    async def test_sin_senal_del_kernel_no_rompe_nada(self):
        """En un kernel que no exponga `rchar` el pipeline debe seguir
        funcionando con el progreso de ffmpeg de siempre."""
        ok, emitidos = await self._correr(
            lecturas=[None] * 8,
            media_frac_por_tick=[0.5, 1.0],
            hevc_bytes=10_000_000_000,
            ticks_hasta_fin_ffmpeg=2)
        self.assertTrue(ok)
        self.assertTrue(any("leído del vídeo" in m for m in emitidos), emitidos)
        self.assertFalse(any("extract-rpu procesando" in m for m in emitidos))


if __name__ == "__main__":
    unittest.main()
