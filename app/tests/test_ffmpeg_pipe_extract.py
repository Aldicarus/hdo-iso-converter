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


if __name__ == "__main__":
    unittest.main()
