"""
ffmpeg y extract-rpu deben trabajar a la vez, no uno detrás del otro.

Los dos pasos grandes de la Fase A recorren el vídeo entero y se estorban.
Medido sobre John Wick (243.552 frames, MKV de 88 GB):

    ffmpeg      574 s   limitado por DISCO   (CPU ociosa)
    extract-rpu 372 s   limitado por CPU     (100% de un core, disco ocioso)

En serie son ~946 s usando media máquina cada vez. Conectados por un pipe el
conjunto va al ritmo del más lento y, además, extract-rpu deja de releer del
disco los 73 GB del HEVC.

Verificado en el NAS con 120 s de vídeo real: el HEVC y el RPU que salen del
pipeline tienen el MISMO md5 que los del camino en dos pasos.

Aquí se fija el contrato de construcción del comando y el comportamiento del
fallback, que es lo que se puede probar sin el binario.
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))


class TestTeePathSafety(unittest.TestCase):
    """El muxer `tee` de ffmpeg usa `|`, `[`, `]` y `:` como sintaxis: una
    ruta con esos caracteres rompería el descriptor de salidas."""

    def test_rutas_normales_son_seguras(self):
        from phases.cmv40_pipeline import _tee_path_is_safe
        for p in (
            "/mnt/tmp/cmv40/cmv40_John_Wick__Chapter_4_2023_1786809913/source.hevc",
            "/mnt/tmp/cmv40/cmv40_28_años_después_2026_1779294213/source.hevc",
            "/mnt/tmp/cmv40/cmv40_Cómo_entrenar_a_tu_dragón_2025_1/source.hevc",
        ):
            self.assertTrue(_tee_path_is_safe(Path(p)), p)

    def test_rutas_con_sintaxis_del_tee_se_rechazan(self):
        from phases.cmv40_pipeline import _tee_path_is_safe
        for p in (
            "/mnt/tmp/pelis [DV FEL]/source.hevc",   # corchetes
            "/mnt/tmp/a|b/source.hevc",              # pipe
            "/mnt/tmp/x:y/source.hevc",              # dos puntos
        ):
            self.assertFalse(_tee_path_is_safe(Path(p)), p)


class TestPipelineFallback(unittest.IsolatedAsyncioTestCase):
    """Ante cualquier problema debe devolver False, nunca lanzar: el caller
    tiene que poder seguir por el camino clásico."""

    async def test_ruta_no_apta_para_tee_no_intenta_el_pipeline(self):
        from phases.cmv40_pipeline import _ffmpeg_extract_rpu_piped
        logged = []

        async def _log(msg):
            logged.append(msg)

        ok = await _ffmpeg_extract_rpu_piped(
            "/no/existe.mkv", Path("/tmp/out.bin"),
            hevc_out=Path("/mnt/tmp/mala [ruta]/source.hevc"),
            log_callback=_log,
        )
        self.assertFalse(ok)
        # Ni siquiera debe haber lanzado ffmpeg: el aviso llega antes del `$`
        self.assertTrue(any("muxer tee" in m for m in logged))
        self.assertFalse(any(m.startswith("$ ") for m in logged))

    async def test_binario_inexistente_devuelve_false(self):
        """Si ffmpeg no está, hay que devolver False — nunca propagar la
        excepción, o el caller no llegaría a intentar el camino clásico."""
        import phases.cmv40_pipeline as pipe
        orig = pipe.FFMPEG_BIN
        pipe.FFMPEG_BIN = "/binario/que/no/existe"
        try:
            ok = await pipe._ffmpeg_extract_rpu_piped(
                "/no/existe.mkv", Path("/tmp/out.bin"), hevc_out=None)
            self.assertFalse(ok)
        finally:
            pipe.FFMPEG_BIN = orig

    async def test_dovi_tool_inexistente_devuelve_false(self):
        import phases.cmv40_pipeline as pipe
        orig = pipe.DOVI_TOOL_BIN
        pipe.DOVI_TOOL_BIN = "/binario/que/no/existe"
        try:
            ok = await pipe._ffmpeg_extract_rpu_piped(
                "/no/existe.mkv", Path("/tmp/out.bin"), hevc_out=None)
            self.assertFalse(ok)
        finally:
            pipe.DOVI_TOOL_BIN = orig


class TestCommandShape(unittest.TestCase):
    """Forma de los comandos, comprobada contra lo que se validó en el NAS."""

    def test_el_codigo_construye_tee_y_pipe(self):
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        # Salida doble (fichero + stdout) cuando se quiere conservar el HEVC
        self.assertIn('"-f", "tee", f"[f=hevc]{hevc_out}|[f=hevc]pipe:1"', src)
        # Pipe puro cuando el HEVC no hace falta (pre-flight sobre otro MKV)
        self.assertIn('"-f", "hevc", "pipe:1"', src)
        # dovi_tool leyendo de stdin
        self.assertIn('[DOVI_TOOL_BIN, "extract-rpu", "-", "-o", str(rpu_out)]', src)

    def test_el_padre_cierra_los_dos_extremos_del_pipe(self):
        """Si el proceso padre se queda con el extremo de escritura abierto,
        dovi_tool nunca ve el EOF y el pipeline se cuelga para siempre."""
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("os.close(read_fd)", src)
        self.assertIn("os.close(write_fd)", src)



class TestProgresoEnElLog(unittest.TestCase):
    """Con el pipeline, el log de la Fase A se quedaba MUDO seis minutos.

    El camino clásico reenviaba las líneas de ffmpeg (throttled a 500 ms) y
    se veía avanzar. Al pasar a pipe, el stderr de ffmpeg solo alimentaba la
    barra y no escribía nada en el log. En vez de devolver las líneas crudas
    —~700 por job, ilegibles y persistidas— se emite una consolidada cada
    20 s.
    """

    def test_formato_de_la_linea(self):
        from phases.cmv40_pipeline import (
            _fmt_ffmpeg_size, _fmt_ffmpeg_speed, _fmt_eta)
        line = ("frame=58601 fps=560 q=-1.0 size=19459840kB "
                "time=00:41:07.38 bitrate=64609.0kbits/s speed=23.6x")
        # Decimales con coma, que es como se escribe en el resto de la UI
        self.assertEqual(_fmt_ffmpeg_size(line), "18,6 GB")
        self.assertEqual(_fmt_ffmpeg_speed(line), " · 23,6x")
        self.assertEqual(_fmt_eta(200), "quedan ~3min 20s")
        self.assertEqual(_fmt_eta(45), "quedan ~45s")
        self.assertEqual(_fmt_eta(0), "casi listo")
        self.assertEqual(_fmt_eta(None), "casi listo")

    def test_lineas_sin_datos_no_rompen(self):
        """ffmpeg no siempre reporta size/speed (arranque, ciertos filtros)."""
        from phases.cmv40_pipeline import _fmt_ffmpeg_size, _fmt_ffmpeg_speed
        self.assertEqual(_fmt_ffmpeg_size("frame=1 time=00:00:01.00"), "…")
        self.assertEqual(_fmt_ffmpeg_speed("frame=1 time=00:00:01.00"), "")

    def test_cadencia_no_es_por_linea(self):
        """ffmpeg emite varias líneas por segundo; el log va cada 10 s."""
        from phases.cmv40_pipeline import PIPE_LOG_EVERY_S
        self.assertGreaterEqual(PIPE_LOG_EVERY_S, 5)

    def test_incluye_el_frame_actual_y_el_total(self):
        from phases.cmv40_pipeline import _fmt_ffmpeg_frame
        line = "frame=58601 fps=560 q=-1.0 size=19459840kB speed=23.6x"
        # Separador de miles español
        self.assertEqual(_fmt_ffmpeg_frame(line, 145303), "frame 58.601/145.303 · ")
        # Sin total conocido (pre-flight sobre otro MKV)
        self.assertEqual(_fmt_ffmpeg_frame(line), "frame 58.601 · ")
        # Si ffmpeg aún no reporta frame, no se inventa nada
        self.assertEqual(_fmt_ffmpeg_frame("size=1024kB", 145303), "")


class TestNumeracionDePasos(unittest.TestCase):
    """La Fase A tiene 3 pasos con pipeline y 4 sin él; el log no puede
    anunciar 'Paso 1/4' y a continuación 'pasos 1+2 juntos'."""

    def test_los_pasos_se_renumeran_segun_la_ruta(self):
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("Paso 1/3: Extrayendo el HEVC y su RPU a la vez", src)
        self.assertIn("""{'2/3' if piped_ok else '3/4'}""", src)
        self.assertIn("""{'3/3' if piped_ok else '4/4'}""", src)

    def test_el_camino_clasico_conserva_su_numeracion(self):
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("Paso 1/4: Extrayendo stream HEVC del MKV origen con ffmpeg", src)
        self.assertIn("Paso 2/4: Extrayendo RPU del HEVC con dovi_tool extract-rpu", src)

if __name__ == "__main__":
    unittest.main()
