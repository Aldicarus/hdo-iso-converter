"""Test de robustez GLOBAL de timeouts de las pipelines.

Lección del bug de Proyecto Salvación (2026-08): un timeout FIJO pequeño
sobre una operación que ESCALA con la duración de la peli (demux, extract-rpu,
export -d all, extract ffmpeg completo) muere a mitad en NAS lentos / pelis
largas. La regla: esas operaciones deben usar timeout ADAPTATIVO
(`_adaptive_timeout`, anclado a la carga real vía ffmpeg_wall_seconds) o, si
no hay ancla, un timeout fijo GENEROSO (≥ FLOOR).

Este test escanea el código fuente de las pipelines y falla si aparece una
operación pesada con un timeout fijo por debajo del suelo → previene que
alguien reintroduzca la clase de bug (p.ej. `timeout=300` en un export).

Operaciones ACOTADAS (input fijo pequeño: sniff de 30s, info --summary sobre
un RPU ya extraído, mkvmerge -J de metadata) están exentas — su coste no
escala con la peli.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_pipeline_timeouts -v
"""
import re
import unittest
from pathlib import Path

_APP = Path(__file__).parent.parent
_FILES = [
    _APP / "phases" / "cmv40_pipeline.py",
    _APP / "phases" / "mkv_analyze.py",
    _APP / "main.py",
    _APP / "routers" / "tab1.py",
    _APP / "routers" / "tab2.py",
]

# Suelo para timeouts FIJOS sobre operaciones pesadas: 1500s (25 min). Por
# debajo se considera arriesgado para un NAS lento / peli larga; hay que usar
# _adaptive_timeout o subir el fijo. (El demux que petó estaba a 900s.)
FLOOR = 1500

# Tokens que delatan una operación que lee/escribe el vídeo/RPU COMPLETO.
HEAVY_TOKENS = ('"demux"', '"extract-rpu"', 'all={', '"export"')

# Marcadores de que el input está ACOTADO (no escala con la peli) → exento.
BOUNDED_MARKERS = (
    "sniff",           # extract-rpu sobre un sniff de 30s
    '"-t", "30"',      # clip de 30s
    "--summary",       # dovi_tool info --summary (RPU ya extraído, rápido)
    '"info"',
    "--identify",      # mkvmerge --identify (metadata)
    '"-J"',            # mkvmerge -J (metadata)
    "--gui-mode",      # mkvmerge mux (tiene su propio progreso, sin timeout duro)
    "_run_export_simple",
)

# Ventana (caracteres antes del `timeout=`) donde buscamos el comando. Las
# llamadas _run([...], timeout=N) tienen el comando 3-6 líneas antes (~200-350
# chars); 380 captura el comando sin arrastrar llamadas vecinas no relacionadas.
_WINDOW = 380

_LITERAL_TIMEOUT = re.compile(r"(?:export_)?timeout\s*=\s*(\d+)")


def _find_heavy_offenders(src: str, fname: str = "?") -> list[str]:
    """Devuelve las ocurrencias de `timeout=<int>` pequeño sobre op pesada."""
    offenders = []
    for m in _LITERAL_TIMEOUT.finditer(src):
        value = int(m.group(1))
        window = src[max(0, m.start() - _WINDOW): m.start()]
        heavy = any(tok in window for tok in HEAVY_TOKENS)
        bounded = any(mk in window for mk in BOUNDED_MARKERS)
        if heavy and not bounded and value < FLOOR:
            line = src[: m.start()].count("\n") + 1
            snippet = window.strip().splitlines()[-1] if window.strip() else ""
            offenders.append(
                f"{fname}:{line}  timeout={value} (<{FLOOR}) sobre op pesada"
                f"  ·  …{snippet[-70:]}"
            )
    return offenders


class TestPipelineTimeoutRobustness(unittest.TestCase):
    def test_detector_catches_synthetic_bad_case(self):
        """Control positivo: el detector DEBE cazar un timeout pequeño sobre
        un extract-rpu (que no pase vacuamente el escaneo real)."""
        bad = (
            'rc, out, err = await _run([\n'
            '    DOVI_TOOL_BIN, "extract-rpu", str(full_hevc), "-o", str(rpu),\n'
            '], timeout=300)\n'
        )
        self.assertTrue(_find_heavy_offenders(bad, "synthetic"),
                        "El detector no cazó un extract-rpu con timeout=300")
        # …y NO debe cazar el sniff acotado ni un info --summary.
        ok_sniff = (
            'rc, _ = await _run([DOVI_TOOL_BIN, "extract-rpu", str(sniff_hevc),\n'
            '    "-o", str(sniff_rpu)], timeout=60)\n'
        )
        self.assertEqual(_find_heavy_offenders(ok_sniff, "sniff"), [])
        ok_info = (
            'rc, s, e = await _run([DOVI_TOOL_BIN, "info", "--summary", str(rpu)], timeout=30)\n'
        )
        self.assertEqual(_find_heavy_offenders(ok_info, "info"), [])

    def test_no_small_fixed_timeout_on_heavy_ops(self):
        offenders = []
        for f in _FILES:
            offenders += _find_heavy_offenders(f.read_text(encoding="utf-8"), f.name)
        self.assertEqual(
            offenders, [],
            "Operación pesada (escala con la peli) con timeout fijo pequeño. "
            "Usa _adaptive_timeout(...) o sube el fijo ≥ %ds:\n  %s"
            % (FLOOR, "\n  ".join(offenders)),
        )

    def test_named_export_timeout_is_generous(self):
        """El timeout del export del análisis extendido va por parámetro, no por
        `_run`, así que el escáner de arriba no lo ve. Escala con la peli, así
        que se comprueba explícitamente.

        Antes esta prueba cubría además `ff_timeout` y `dt_timeout` del perfil
        de luminancia, que tenía su propio pipeline en `main.py`. Ya no lo
        tiene: el perfil se calcula junto a la auditoría, compartiendo la
        extracción, y esa extracción usa el timeout adaptativo del pipe."""
        src = (_APP / "phases" / "mkv_analyze.py").read_text(encoding="utf-8")
        m = re.search(r"export_timeout=(\d+)", src)
        self.assertIsNotNone(m, "No encontré export_timeout en mkv_analyze.py")
        self.assertGreaterEqual(
            int(m.group(1)), FLOOR,
            f"export_timeout={m.group(1)} < {FLOOR}: demasiado justo para un "
            "RPU full-movie")

    def test_demux_uses_adaptive_timeout_and_reuse_guard(self):
        """Regresión directa del bug: el demux debe usar timeout ADAPTATIVO y
        la reutilización debe ir por _demux_output_reusable (marcador), nunca
        por 'existe el fichero'."""
        src = (_APP / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        # El demux referencia demux_timeout, calculado con _adaptive_timeout.
        self.assertIn("demux_timeout = _adaptive_timeout(", src)
        self.assertIn('timeout=demux_timeout', src)
        # La reutilización pasa por el guard de completitud, no por exists() a secas.
        self.assertIn("_demux_output_reusable(", src)
        self.assertIn(".demux_done", src)


if __name__ == "__main__":
    unittest.main()
