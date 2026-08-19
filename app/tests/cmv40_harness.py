"""
Arnés para ejecutar fases CMv4.0 de verdad dentro de un unittest.

Antes de esto, las fases C/F/G/H no se podían invocar en un test: son
corutinas de 200-440 líneas que lanzan `ffmpeg`, `dovi_tool` y `mkvmerge`
sobre ficheros de decenas de GB. La consecuencia era que lo poco que se
comprobaba de ellas se comprobaba **leyendo el fuente** (`src.find("async
def run_phase_f_inject")` + `assertIn`), que pasa en verde aunque el
comportamiento esté roto y se rompe al renombrar una variable.

`FakeToolbox` instala en el PATH cuatro binarios falsos —`dovi_tool`,
`ffmpeg`, `ffprobe`, `mkvmerge`— que el pipeline resuelve por nombre
(`create_subprocess_exec("dovi_tool", ...)`), así que **no hay que tocar
el código de producción** para usarlo.

Los falsos no son mudos: modelan la semántica de dovi_tool lo justo para
que las validaciones internas del pipeline tengan sentido. Cada artefacto
producido lleva un sidecar `.meta.json` con las propiedades del RPU que
contiene, y esas propiedades se propagan por la cadena igual que en el NAS:

    editor + allow_cmv4_transfer  → el output pasa a CM v4.0 con L8
    editor + {"mode": 2}          → el output pasa a Profile 8 sin el_type
    inject-rpu -i H --rpu-in R    → H_out hereda las props de R
    mux --bl B --el E             → el output hereda las props de E
    extract-rpu H -o R.bin        → R.bin hereda las props de H

Sin esa fidelidad las verificaciones post-merge del pipeline fallarían
siempre y no se podría testear nada. Con ella, un test puede afirmar QUÉ
RPU se inyecta en QUÉ HEVC en cada rama de la matriz de workflows.

Uso típico:

    tb = FakeToolbox(self.tmp)
    tb.define_rpu("RPU_source.bin", profile=7, el_type="MEL",
                  cm_version="v2.9", frames=1000)
    tb.define_rpu("RPU_target.bin", profile=8, cm_version="v4.0",
                  frames=1000, has_l8=True)
    self.enterContext(tb)          # instala y restaura el PATH al salir
    await run_phase_f_inject(session, log)
    self.assertEqual(tb.one("dovi_tool", "inject-rpu").opt("-i"), ".../BL.hevc")
"""
import hashlib
import json
import os
import shutil
import stat
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Relleno de los artefactos falsos. Por encima de los dos mínimos que el
# pipeline exige: `artifact_exists` pide >=100 bytes y `_ensure_profile8_rpu`
# descarta una conversión que deje <1000.
_FILLER = b"\x00" * 4096

FAKE_BINARIES = ("dovi_tool", "ffmpeg", "ffprobe", "mkvmerge",
                 "mediainfo", "mkvextract", "mkvpropedit")


# ════════════════════════════════════════════════════════════════════════
#  Propiedades de un RPU — lo que `dovi_tool info --summary` reporta
# ════════════════════════════════════════════════════════════════════════

@dataclass
class RpuProps:
    """Las propiedades que el pipeline lee de un RPU y sobre las que decide."""
    profile: int = 7
    el_type: str = "FEL"          # "FEL" | "MEL" | "" (single-layer)
    cm_version: str = "v2.9"      # "v2.9" | "v4.0"
    frames: int = 1000
    scenes: int = 120
    has_l8: bool = False
    has_l11: bool = True

    def to_summary(self) -> str:
        """Reconstruye el texto que `_parse_dovi_summary` sabe leer.

        El formato sigue los regex de `phases/phase_a._parse_dovi_summary`:
        P8 se anuncia como "Profile: 8.1" (sin sufijo de capa) y P7 lleva
        "(FEL)" o "(MEL)".
        """
        if self.profile == 8:
            profile_line = "Profile: 8.1"
        elif self.el_type:
            profile_line = f"Profile: {self.profile} ({self.el_type})"
        else:
            profile_line = f"Profile: {self.profile}"
        dm = 4 if self.cm_version == "v4.0" else 2
        lines = [
            "Parsing RPU file...",
            profile_line,
            f"DM version: {dm} (CM {self.cm_version})",
            f"Scene/shot count: {self.scenes}",
            f"Frames: {self.frames}",
            "content light level (L1): min 0, max 1000",
            "L2 trims: 100, 600",
            "L5 offsets: 0, 0, 0, 0",
            "L6 metadata: MaxCLL 1000, MaxFALL 400",
        ]
        if self.has_l8:
            lines.append("L8 trims: 100, 600, 1000, 2000")
            lines.append("L9 source primaries: BT.2020")
        if self.has_l11:
            lines.append("L11 content type: Cinema")
        return "\n".join(lines) + "\n"

    def as_dict(self) -> dict:
        return {
            "profile": self.profile, "el_type": self.el_type,
            "cm_version": self.cm_version, "frames": self.frames,
            "scenes": self.scenes, "has_l8": self.has_l8,
            "has_l11": self.has_l11,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RpuProps":
        return cls(**{k: v for k, v in d.items() if k in cls.__annotations__})


# ════════════════════════════════════════════════════════════════════════
#  Invocaciones registradas
# ════════════════════════════════════════════════════════════════════════

@dataclass
class Call:
    """Una invocación de un binario falso, tal y como la hizo el pipeline."""
    binary: str
    argv: list[str]
    json_args: dict = field(default_factory=dict)   # contenido de los `-j` leídos

    @property
    def subcommand(self) -> str:
        """Primer argumento que no es una opción (`info`, `editor`, `mux`…)."""
        for a in self.argv:
            if not a.startswith("-"):
                return a
        return ""

    def opt(self, name: str) -> str | None:
        """Valor que sigue a una opción (`-i`, `--rpu-in`, `-o`…)."""
        try:
            return self.argv[self.argv.index(name) + 1]
        except (ValueError, IndexError):
            return None

    def opt_name(self, name: str) -> str | None:
        """Como `opt` pero devolviendo solo el basename — lo habitual al
        afirmar sobre artefactos, cuyo directorio es un tmpdir aleatorio."""
        v = self.opt(name)
        return Path(v).name if v else None

    def has(self, flag: str) -> bool:
        return flag in self.argv

    def __repr__(self) -> str:  # mensajes de fallo legibles
        return f"<Call {self.binary} {' '.join(self.argv)}>"


# ════════════════════════════════════════════════════════════════════════
#  FakeToolbox
# ════════════════════════════════════════════════════════════════════════

class FakeToolbox:
    """Instala binarios falsos en el PATH y registra cómo se invocan."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.bin_dir = self.root / "fakebin"
        self.state_dir = self.root / "fakestate"
        self._scenario: dict = {"rpus": {}, "media": {}, "fail": {},
                                "fail_json": [], "mediainfo": {},
                                "chapters": {}, "mkvs": {},
                                "pgs": {}, "levels": {}}
        self._old_path: str | None = None

    # ── configuración del escenario ──────────────────────────────────

    def define_rpu(self, name: str, **props) -> RpuProps:
        """Declara las propiedades de un RPU de entrada (por basename)."""
        p = RpuProps(**props)
        self._scenario["rpus"][name] = p.as_dict()
        self._flush()
        return p

    def define_rpu_levels(self, name: str, *, l8_indices: list[int] | None = None,
                          l9_primary: int | None = None,
                          l11_content_type: int | None = None) -> None:
        """Los niveles que `dovi_tool export --levels` devolverá para un RPU.

        Formatos verificados contra RPUs reales del repo DoviTools:
          level8  → {"frame", "length", "target_display_index", "trim_*"}
          level9  → {"frame", "length", "source_primary_index"}
          level11 → {"frame", "content_type", "whitepoint",
                     "reference_mode_flag"}

        `l9_primary` es 0 (BT.709) en los RPUs reales, así que `None` y `0`
        tienen que poder distinguirse: pasar 0 declara el nivel, no lo omite.
        """
        self._scenario.setdefault("levels", {})[name] = {
            "l8_indices": l8_indices,
            "l9_primary": l9_primary,
            "l11_content_type": l11_content_type,
        }
        self._flush()

    def define_media(self, name: str, duration: float = 7200.0,
                     frames: int = 1000) -> None:
        """Duración y frame count que `ffprobe` reportará para un fichero."""
        self._scenario["media"][name] = {"duration": duration, "frames": frames}
        self._flush()

    def define_mkv(self, name: str, *, tracks: list[dict] | None = None,
                   title: str = "", duration_s: float = 7200.0) -> None:
        """Las pistas que `mkvmerge -J` reportará para un MKV.

        Cada pista es un dict con `type` ("video"/"audio"/"subtitles") y los
        campos que `analyze_mkv` lee: `codec`, `language`, `track_name`,
        `default`, `forced`, `channels`, `dimensions`, `fps`.

        Por defecto describe un UHD Blu-ray dual-layer con Dolby Vision: dos
        pistas HEVC (la segunda a 1920x1080 es la Enhancement Layer, que es
        cómo `analyze_mkv` detecta la FEL), audio y subtítulos.
        """
        if tracks is None:
            tracks = [
                {"type": "video", "codec": "HEVC/H.265/MPEG-H",
                 "dimensions": "3840x2160", "fps": 23.976},
                {"type": "video", "codec": "HEVC/H.265/MPEG-H",
                 "dimensions": "1920x1080", "fps": 23.976},
                {"type": "audio", "codec": "TrueHD Atmos", "language": "spa",
                 "channels": 8, "default": True},
                {"type": "audio", "codec": "TrueHD Atmos", "language": "eng",
                 "channels": 8},
                {"type": "subtitles", "codec": "HDMV PGS", "language": "spa",
                 "forced": True},
                {"type": "subtitles", "codec": "HDMV PGS", "language": "spa"},
            ]
        self._scenario.setdefault("mkvs", {})[name] = {
            "tracks": tracks, "title": title, "duration_s": duration_s,
        }
        self._flush()

    def define_mediainfo(self, name: str, *, video: dict | None = None,
                         audio: list[dict] | None = None,
                         subs: list[dict] | None = None,
                         duration_s: float = 7200.0,
                         size_bytes: int = 80_000_000_000) -> None:
        """Lo que `mediainfo --Output=JSON` reportará para un fichero.

        Tab 2 lo usa para el bitrate real, el `Format_Commercial_IfAny` (que es
        la señal definitiva de Atmos/DTS:X) y la metadata HDR10. Los defaults
        describen un UHD con HDR10 y una pista TrueHD Atmos.
        """
        if audio is None:
            # Coherente con el MKV por defecto de `define_mkv`: dos TrueHD
            # Atmos. Bitrates tomados de un UHD real del NAS.
            audio = [{"bitrate": 4_423_581, "language": "es"},
                     {"bitrate": 4_151_159, "language": "en"}]
        if subs is None:
            subs = [{"language": "es"}, {"language": "es"}]
        self._scenario.setdefault("mediainfo", {})[name] = {
            "video": video, "audio": audio, "subs": subs,
            "duration_s": duration_s, "size_bytes": size_bytes,
        }
        self._flush()

    def define_pgs_packets(self, name: str, por_pista: dict[int, int]) -> None:
        """Paquetes PGS por índice de stream, que `ffprobe -show_packets`
        emitirá como una línea por paquete.

        Es la señal con la que Fase B distingue un subtítulo forzado de uno
        completo (los forzados tienen muchos menos), y la que Tab 2 muestra
        como "paquetes PGS".
        """
        self._scenario.setdefault("pgs", {})[name] = {
            str(k): v for k, v in por_pista.items()}
        self._flush()

    def define_chapters(self, name: str, capitulos: list[tuple[str, str]]) -> None:
        """Capítulos que `mkvextract chapters --simple` devolverá.

        `capitulos` es una lista de (timestamp, nombre).
        """
        self._scenario.setdefault("chapters", {})[name] = capitulos
        self._flush()

    def fail(self, binary: str, subcommand: str, rc: int = 1,
             stderr: str = "fallo simulado") -> None:
        """Fuerza un código de retorno para una invocación concreta."""
        self._scenario["fail"][f"{binary}:{subcommand}"] = {"rc": rc, "stderr": stderr}
        self._flush()

    def fail_when_json(self, binary: str, subcommand: str, json_match: dict,
                       rc: int = 1, stderr: str = "fallo simulado") -> None:
        """Falla solo las invocaciones cuyo `-j` contenga esos pares.

        `dovi_tool editor` hace dos trabajos muy distintos según su config
        (`allow_cmv4_transfer` para el merge, `{"mode": 2}` para convertir a
        Profile 8.1) y un test necesita poder romper uno sin romper el otro.
        """
        self._scenario.setdefault("fail_json", []).append({
            "binary": binary, "subcommand": subcommand,
            "json_match": json_match, "rc": rc, "stderr": stderr,
        })
        self._flush()

    # ── ciclo de vida ────────────────────────────────────────────────

    def install(self) -> "FakeToolbox":
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._flush()
        script = _FAKE_SCRIPT.replace("@@PYTHON@@", sys.executable)
        for name in FAKE_BINARIES:
            p = self.bin_dir / name
            p.write_text(script, encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{self._old_path}"
        os.environ["CMV40_FAKE_STATE"] = str(self.state_dir)
        return self

    def uninstall(self) -> None:
        if self._old_path is not None:
            os.environ["PATH"] = self._old_path
            self._old_path = None
        os.environ.pop("CMV40_FAKE_STATE", None)

    # context manager, para `self.enterContext(tb)` en los tests
    def __enter__(self) -> "FakeToolbox":
        return self.install()

    def __exit__(self, *exc) -> None:
        self.uninstall()

    # ── lectura de las invocaciones ──────────────────────────────────

    @property
    def calls(self) -> list[Call]:
        log = self.state_dir / "calls.jsonl"
        if not log.exists():
            return []
        out = []
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            out.append(Call(binary=d["binary"], argv=d["argv"],
                            json_args=d.get("json_args") or {}))
        return out

    def find(self, binary: str, subcommand: str | None = None) -> list[Call]:
        """Todas las invocaciones de un binario (y opcionalmente subcomando)."""
        return [c for c in self.calls
                if c.binary == binary
                and (subcommand is None or c.subcommand == subcommand)]

    def one(self, binary: str, subcommand: str | None = None) -> Call:
        """La única invocación esperada. Falla con detalle si hay 0 o >1."""
        hits = self.find(binary, subcommand)
        if len(hits) != 1:
            raise AssertionError(
                f"Se esperaba 1 invocación de {binary} {subcommand or ''}, "
                f"hay {len(hits)}. Todas las llamadas registradas:\n  "
                + "\n  ".join(repr(c) for c in self.calls)
            )
        return hits[0]

    def ran(self, binary: str, subcommand: str | None = None) -> bool:
        return bool(self.find(binary, subcommand))

    def reset_calls(self) -> None:
        (self.state_dir / "calls.jsonl").unlink(missing_ok=True)

    # ── artefactos ───────────────────────────────────────────────────

    def props_of(self, path: Path) -> RpuProps | None:
        """Propiedades del RPU que un artefacto producido contiene.

        Es la forma de comprobar, por ejemplo, que el HEVC inyectado en un
        workflow single-layer lleva un RPU Profile 8 y no Profile 7.

        Mira la cabecera del contenido antes del sidecar: el sidecar está
        indexado por ruta, así que un artefacto que el pipeline haya movido
        (`os.replace`, el `.mkv.tmp` -> `.mkv` de la Fase H) solo se
        identifica por la cabecera.
        """
        try:
            with open(path, "rb") as f:
                primera = f.readline()
            cab = b"#HARNESS-PROPS "
            if primera.startswith(cab):
                return RpuProps.from_dict(json.loads(primera[len(cab):]))
        except (OSError, ValueError):
            pass
        meta = self._meta_path(path)
        if not meta.exists():
            return None
        return RpuProps.from_dict(json.loads(meta.read_text(encoding="utf-8")))

    def _meta_path(self, path) -> Path:
        return sidecar_path(path, self.state_dir)

    def _flush(self) -> None:
        if self.state_dir.exists():
            (self.state_dir / "scenario.json").write_text(
                json.dumps(self._scenario), encoding="utf-8")


# ════════════════════════════════════════════════════════════════════════
#  Constructores de sesión y workdir
# ════════════════════════════════════════════════════════════════════════

def make_session(workdir: Path, **overrides):
    """CMv40Session mínima pero coherente, con el workdir apuntando al tmpdir."""
    from models import CMv40Session

    fields = {
        "id": "cmv40_test_1700000000",
        "source_mkv_path": str(workdir / "source.mkv"),
        "source_mkv_name": "source.mkv",
        "artifacts_dir": str(workdir),
        "output_mkv_name": "Peli (2024) [DV FEL][CMv4 CORE].mkv",
        "source_frame_count": 1000,
        "target_frame_count": 1000,
        "source_workflow": "p7_fel",
        "target_type": "trusted_p7_fel_final",
        "target_trust_ok": True,
        "trust_override": "auto",
    }
    fields.update(overrides)
    return CMv40Session(**fields)


def sidecar_path(path, state_dir=None) -> Path:
    """Ruta del sidecar de props de un artefacto.

    Vive bajo el `state_dir` del arnés, NO junto al artefacto: así el
    directorio de trabajo que ve un test contiene exactamente lo que el
    pipeline dejó ahí, sin excepciones que haya que recordar en cada
    assert. Debe coincidir con `meta_path` del script embebido.
    """
    if state_dir is None:
        base = os.environ.get("CMV40_FAKE_STATE")
        if not base:
            raise RuntimeError(
                "sidecar_path necesita un FakeToolbox instalado "
                "(CMV40_FAKE_STATE sin definir) o un state_dir explícito")
        state_dir = base
    return Path(state_dir) / "meta" / (hashlib.sha1(
        str(Path(path).resolve()).encode("utf-8")).hexdigest() + ".json")


def write_artifacts(workdir: Path, *names: str, props: RpuProps | None = None) -> None:
    """Crea artefactos falsos con tamaño suficiente para los guards del pipeline.

    Si se pasan `props`, se escribe también su sidecar para que los falsos
    sepan qué RPU contiene cada fichero (ver `sidecar_path`).
    """
    workdir.mkdir(parents=True, exist_ok=True)
    for n in names:
        p = workdir / n
        p.write_bytes(_FILLER)
        if props is not None:
            mp = sidecar_path(p)
            mp.parent.mkdir(parents=True, exist_ok=True)
            mp.write_text(json.dumps(props.as_dict()), encoding="utf-8")


class CollectingLog:
    """log_callback que acumula las líneas emitidas por una fase."""

    def __init__(self):
        self.lines: list[str] = []

    async def __call__(self, msg: str) -> None:
        self.lines.append(msg)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def find(self, needle: str) -> list[str]:
        return [l for l in self.lines if needle in l]

    def says(self, needle: str) -> bool:
        return any(needle in l for l in self.lines)

    @property
    def plan(self) -> str:
        """El texto del `📋 Plan` que la fase anunció al usuario."""
        hits = [l for l in self.lines if "📋 Plan" in l]
        return "\n".join(hits)


class PhaseTestCase(unittest.IsolatedAsyncioTestCase):
    """Base para tests que ejecutan fases: tmpdir, toolbox y OUTPUT_DIR aislados."""

    def setUp(self) -> None:
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="cmv40_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wd = self.tmp / "workdir"
        self.wd.mkdir(parents=True, exist_ok=True)
        self.output_dir = self.tmp / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)

        # OUTPUT_DIR se resuelve al importar el módulo desde una env var que
        # en el Mac apunta a /mnt/output; las fases G/H escriben ahí.
        from phases import cmv40_pipeline as pipeline
        self._orig_output = pipeline.OUTPUT_DIR
        pipeline.OUTPUT_DIR = self.output_dir
        self.addCleanup(lambda: setattr(pipeline, "OUTPUT_DIR", self._orig_output))

        self.log = CollectingLog()


# ════════════════════════════════════════════════════════════════════════
#  El binario falso
# ════════════════════════════════════════════════════════════════════════
#
# Un único script que hace de los cuatro binarios, distinguiéndolos por
# `argv[0]`. Se instala con permisos de ejecución y shebang al mismo
# intérprete que corre los tests, así que no depende del PATH del sistema.

_FAKE_SCRIPT = r'''#!@@PYTHON@@
"""Binario falso del arnés CMv4.0 — ver app/tests/cmv40_harness.py."""
import hashlib
import json
import os
import sys
from pathlib import Path

FILLER = b"\x00" * 4096
STATE = Path(os.environ["CMV40_FAKE_STATE"])
META_DIR = STATE / "meta"
BINARY = Path(sys.argv[0]).name
ARGV = sys.argv[1:]


def scenario():
    p = STATE / "scenario.json"
    return json.loads(p.read_text()) if p.exists() else {"rpus": {}, "media": {}, "fail": {}}


def subcommand():
    for a in ARGV:
        if not a.startswith("-"):
            return a
    return ""


def opt(name):
    try:
        return ARGV[ARGV.index(name) + 1]
    except (ValueError, IndexError):
        return None


DEFAULT_PROPS = {
    "profile": 7, "el_type": "FEL", "cm_version": "v2.9",
    "frames": 1000, "scenes": 120, "has_l8": False, "has_l11": True,
}


def meta_path(p):
    """Sidecar de props, FUERA del directorio del artefacto.

    Adyacente (`fichero.hevc.meta.json`) obligaba a todo test que mirase
    restos en un workdir a excluirlo a mano, y esa excepción tapa restos
    de verdad: un `.meta.json` que dejara el pipeline sería invisible.
    Los binarios reales no dejan sidecars, así que el arnés tampoco.
    """
    return META_DIR / (hashlib.sha1(
        str(Path(p).resolve()).encode("utf-8")).hexdigest() + ".json")


def read_props(path, sc):
    """Props del fichero: cabecera del contenido, sidecar, o escenario.

    La cabecera va PRIMERO porque es la que sobrevive a un rename. El
    pipeline mueve artefactos (`os.replace` del RPU corregido, el
    `.mkv.tmp` -> `.mkv` de la Fase H) y el sidecar está indexado por
    ruta absoluta, así que tras mover el fichero apuntaba al nombre viejo
    y el artefacto movido volvía a los valores por defecto. El
    `dovi_tool` real lleva la metadata DENTRO del fichero: un rename no
    se la lleva por delante.
    """
    if path is None:
        return dict(DEFAULT_PROPS)
    path = Path(path)
    cab = props_in_file(path)
    if cab is not None:
        return cab
    mp = meta_path(path)
    if mp.exists():
        d = dict(DEFAULT_PROPS)
        d.update(json.loads(mp.read_text()))
        return d
    d = dict(DEFAULT_PROPS)
    d.update(sc.get("rpus", {}).get(path.name, {}))
    return d


def props_in_file(path):
    """Props escritas en la cabecera del artefacto, o None si no las lleva."""
    try:
        with open(str(path), "rb") as f:
            primera = f.readline()
    except OSError:
        return None
    cab = b"#HARNESS-PROPS "
    if not primera.startswith(cab):
        return None
    try:
        d = dict(DEFAULT_PROPS)
        d.update(json.loads(primera[len(cab):]))
        return d
    except ValueError:
        return None


def write_props(path, props):
    mp = meta_path(Path(path))
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(props))


def produce(path, props=None):
    """Crea un artefacto de salida con su sidecar de props.

    Exige ruta absoluta: un fake que reciba una relativa escribiría en el
    cwd del test, que es la raíz del repo. Así apareció commiteado un
    fichero llamado `pipe:1`, porque el fake de ffmpeg trataba la salida
    del pipe como si fuera un nombre de fichero.
    """
    if not str(path).startswith("/"):
        raise RuntimeError(
            "produce() con ruta relativa %r: escribiría en el cwd del test. "
            "Si es una salida especial (pipe:N, -), trátala antes." % (path,))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if props is not None:
        # Cabecera en el contenido + sidecar. La cabecera es la que viaja si
        # el pipeline mueve el fichero (ver `read_props`).
        p.write_bytes(stream_bytes(props))
        write_props(p, props)
    else:
        p.write_bytes(FILLER)


def summary(props):
    if props["profile"] == 8:
        profile_line = "Profile: 8.1"
    elif props["el_type"]:
        profile_line = "Profile: %d (%s)" % (props["profile"], props["el_type"])
    else:
        profile_line = "Profile: %d" % props["profile"]
    dm = 4 if props["cm_version"] == "v4.0" else 2
    lines = [
        "Parsing RPU file...",
        profile_line,
        "DM version: %d (CM %s)" % (dm, props["cm_version"]),
        "Scene/shot count: %d" % props["scenes"],
        "Frames: %d" % props["frames"],
        "content light level (L1): min 0, max 1000",
        "L2 trims: 100, 600",
        "L5 offsets: 0, 0, 0, 0",
        "L6 metadata: MaxCLL 1000, MaxFALL 400",
    ]
    if props["has_l8"]:
        lines.append("L8 trims: 100, 600, 1000, 2000")
        lines.append("L9 source primaries: BT.2020")
    if props["has_l11"]:
        lines.append("L11 content type: Cinema")
    return "\n".join(lines) + "\n"


def record(json_args=None):
    with (STATE / "calls.jsonl").open("a") as f:
        f.write(json.dumps({
            "binary": BINARY, "argv": ARGV, "json_args": json_args or {},
        }) + "\n")


def main():
    sc = scenario()
    sub = subcommand()

    # "*" como subcomando: falla cualquier invocación de ese binario. Útil con
    # ffmpeg, cuyo "subcomando" (primer argumento sin guion) es en realidad la
    # ruta del fichero de entrada.
    forced = (sc.get("fail", {}).get("%s:%s" % (BINARY, sub))
              or sc.get("fail", {}).get("%s:*" % BINARY))

    json_args = {}
    cfg = opt("-j")
    if cfg and Path(cfg).exists():
        try:
            json_args = json.loads(Path(cfg).read_text())
        except Exception:
            json_args = {}
    record(json_args)

    if forced is None:
        for rule in sc.get("fail_json", []):
            if rule["binary"] != BINARY or rule["subcommand"] != sub:
                continue
            if all(json_args.get(k) == v for k, v in rule["json_match"].items()):
                forced = rule
                break

    if forced:
        sys.stderr.write(forced.get("stderr", "fallo simulado"))
        return int(forced.get("rc", 1))

    if BINARY == "dovi_tool":
        return dovi_tool(sc, sub, json_args)
    if BINARY == "ffprobe":
        return ffprobe(sc)
    if BINARY == "ffmpeg":
        return ffmpeg(sc)
    if BINARY == "mkvmerge":
        return mkvmerge(sc)
    if BINARY == "mediainfo":
        return mediainfo(sc)
    if BINARY == "mkvextract":
        return mkvextract(sc)
    if BINARY == "mkvpropedit":
        return mkvpropedit(sc)
    return 0


def dovi_tool(sc, sub, json_args):
    if sub == "info":
        # `info --summary <path>` y `info -s -i <path>` son la misma consulta.
        target = opt("--summary") or opt("-i")
        if target is None:
            # `--summary` puede venir sin valor y el path llegar al final.
            positional = [a for a in ARGV if not a.startswith("-") and a != "info"]
            target = positional[-1] if positional else None
        sys.stdout.write(summary(read_props(target, sc)))
        return 0

    if sub == "editor":
        src = opt("-i")
        out = opt("-o")
        props = read_props(src, sc)
        if json_args.get("allow_cmv4_transfer"):
            # El transfer copia levels CMv4.0 del donante preservando la
            # estructura (profile + el_type) del input. Igual que en el NAS.
            donor = read_props(json_args.get("source_rpu"), sc)
            props["cm_version"] = "v4.0"
            props["has_l8"] = donor.get("has_l8", True)
            props["has_l11"] = donor.get("has_l11", True)
        if json_args.get("mode") == 2:
            # "Converts the RPU to be profile 8.1 compatible": el profile
            # cambia, el resto de metadata se preserva.
            props["profile"] = 8
            props["el_type"] = ""
        # remove / duplicate cambian el numero de frames del RPU. Sin esto un
        # test no puede comprobar que las correcciones de sync se acumulan:
        # el frame count saldria igual tras cada pasada.
        quitados = 0
        for r in json_args.get("remove") or []:
            try:
                if "-" in str(r):
                    a, b = str(r).split("-")
                    quitados += int(b) - int(a) + 1
                else:
                    quitados += 1
            except ValueError:
                pass
        anadidos = 0
        for d in json_args.get("duplicate") or []:
            if isinstance(d, dict):
                anadidos += int(d.get("length") or 0)
        if quitados or anadidos:
            props["frames"] = max(0, int(props.get("frames") or 0) - quitados + anadidos)
        produce(out, props)
        return 0

    if sub == "inject-rpu":
        hevc_in = opt("-i")
        rpu_in = opt("--rpu-in")
        out = opt("-o")
        # El HEVC de salida contiene el RPU inyectado, con el frame count del
        # HEVC de entrada.
        props = read_props(rpu_in, sc)
        produce(out, props)
        sys.stdout.write("Processing input video for frame order info...\n")
        sys.stdout.write("Rewriting file with interleaved RPU NALs...\n")
        return 0

    if sub == "demux":
        positional = [a for a in ARGV if not a.startswith("-") and a != "demux"]
        src = positional[0] if positional else None
        props = read_props(src, sc)
        bl, el = opt("--bl-out"), opt("--el-out")
        if bl:
            produce(bl, props)
        if el:
            produce(el, props)
        return 0

    if sub == "mux":
        el = opt("--el")
        out = opt("-o")
        # El dual-layer resultante lleva el RPU de la capa de mejora.
        produce(out, read_props(el, sc))
        return 0

    if sub == "extract-rpu":
        # `-` es la entrada por stdin, no una opción: sin la excepción se
        # filtraba como tal y el modo pipe de la Fase A nunca se reconocía.
        positional = [a for a in ARGV
                      if (a == "-" or not a.startswith("-")) and a != "extract-rpu"]
        src = positional[0] if positional else None
        out = opt("-o")
        if src == "-":
            # Modo pipe (Fase A): hay que sacar del pipe TODO lo que el
            # productor escribió o ffmpeg no puede terminar. Las props
            # llegan en la cabecera del stream, no por sidecar.
            datos = b""
            try:
                datos = sys.stdin.buffer.read()
            except Exception:
                pass
            produce(out, props_from_stream(datos, sc))
            return 0
        produce(out, read_props(src, sc))
        return 0

    if sub == "export":
        src = opt("-i")
        props = read_props(src, sc)
        n = min(int(props["frames"]), 200)

        # Camino preferente del pipeline (dovi_tool >= 2.3.3):
        #   export -i RPU -f json --levels level1=/path/a.json,level5=/path/b.json
        # Es el que se usa en el NAS; volcar el RPU entero son 682 MB frente a 8.
        levels_arg = opt("--levels")
        if levels_arg:
            niveles = sc.get("levels", {}).get(Path(src).name if src else "", {})
            for spec in levels_arg.split(","):
                if "=" not in spec:
                    continue
                lv, dest = spec.split("=", 1)
                rows = []
                for i in range(n):
                    if lv == "level1":
                        rows.append({"frame": i, "min_pq": 0,
                                     "avg_pq": 500, "max_pq": 2000 + i})
                    elif lv == "level5":
                        rows.append({"frame": i,
                                     "active_area_left_offset": 0,
                                     "active_area_right_offset": 0,
                                     "active_area_top_offset": 138,
                                     "active_area_bottom_offset": 138})
                    elif lv == "level6":
                        rows.append({"frame": i,
                                     "max_display_mastering_luminance": 1000,
                                     "max_content_light_level": 1000,
                                     "max_frame_average_light_level": 400})
                    elif lv == "level8":
                        # Un registro por (frame, target display), como el
                        # real: en Zootopia salen 2 índices × 155.001 frames.
                        for idx in (niveles.get("l8_indices") or []):
                            rows.append({
                                "frame": i, "length": 25,
                                "target_display_index": idx,
                                "trim_slope": 2312, "trim_offset": 2059,
                                "trim_power": 2034, "trim_chroma_weight": 2048,
                                "trim_saturation_gain": 2048, "ms_weight": 2048,
                            })
                    elif lv == "level9":
                        # OJO: 0 es un valor válido (BT.709) y el que traen los
                        # RPUs reales. `is None` distingue ausente de cero.
                        if niveles.get("l9_primary") is not None:
                            rows.append({"frame": i, "length": 1,
                                         "source_primary_index": niveles["l9_primary"]})
                    elif lv == "level11":
                        if niveles.get("l11_content_type") is not None:
                            rows.append({
                                "frame": i,
                                "content_type": niveles["l11_content_type"],
                                "whitepoint": 0, "reference_mode_flag": False,
                                "reserved_byte2": 0, "reserved_byte3": 0,
                            })
                    else:
                        # level10 sale vacío en los RPUs reales probados.
                        pass
                Path(dest).write_text(json.dumps(rows))
            return 0

        # Camino de respaldo: `-d all=<json>`, el volcado completo.
        out = None
        for a in ARGV:
            if a.startswith("all="):
                out = a.split("=", 1)[1]
        if out:
            frames = [{
                "cmv29_metadata": {"level1": {
                    "min_pq": 0, "avg_pq": 500, "max_pq": 2000 + i}},
            } for i in range(n)]
            Path(out).write_text(json.dumps(frames))
        return 0

    return 0


def mediainfo(sc):
    """`mediainfo --Output=JSON <fichero>`.

    Tab 2 lo consume vía `phase_a.run_mediainfo`, que espera el árbol
    `{"media": {"track": [...]}}` con `@type` por pista. Los defaults
    describen un UHD con HDR10 y TrueHD Atmos, que es el caso dominante.
    """
    positional = [a for a in ARGV if not a.startswith("-")]
    name = Path(positional[-1]).name if positional else ""
    spec = sc.get("mediainfo", {}).get(name)
    if spec is None:
        # Sin declarar: mediainfo "no encuentra nada útil". El caller lo
        # tolera (el enriquecimiento es opcional).
        sys.stdout.write(json.dumps({"media": {"track": []}}))
        return 0

    tracks = [{
        "@type": "General",
        "Duration": str(spec["duration_s"]),
        "FileSize": str(spec["size_bytes"]),
    }]
    v = spec.get("video")
    if v is None:
        v = {}
    if v is not False:
        tracks.append({
            "@type": "Video",
            "StreamOrder": "0",
            "BitRate": str(v.get("bitrate", 60_000_000)),
            "BitDepth": str(v.get("bit_depth", 10)),
            "colour_primaries": v.get("primaries", "BT.2020"),
            "transfer_characteristics": v.get("transfer", "PQ"),
            "MasteringDisplay_Luminance": v.get(
                "mastering", "min: 0.0001 cd/m2, max: 1000 cd/m2"),
            "MasteringDisplay_ColorPrimaries": v.get("mastering_primaries",
                                                     "Display P3"),
            # CON UNIDAD, como el mediainfo real: verificado contra el
            # contenedor, devuelve "300 cd/m2". Un fake que diera el número
            # pelado habría ocultado el bug de `int(rt["MaxCLL"])`.
            "MaxCLL": str(v.get("maxcll", 1000)) + " cd/m2",
            "MaxFALL": str(v.get("maxfall", 400)) + " cd/m2",
            "Format": v.get("format", "HEVC"),
        })
    for i, a in enumerate(spec["audio"]):
        tracks.append({
            "@type": "Audio",
            "StreamOrder": str(i + 1),
            "BitRate": str(a.get("bitrate", 3_000_000)),
            "Format_Commercial_IfAny": a.get("commercial", "Dolby TrueHD with Dolby Atmos"),
            "Channels": str(a.get("channels", 8)),
            "Compression_Mode": a.get("compression", "Lossless"),
            "Language": a.get("language", "es"),
        })
    for i, t in enumerate(spec["subs"]):
        tracks.append({
            "@type": "Text",
            "StreamOrder": str(len(spec["audio"]) + i + 1),
            "Width": str(t.get("width", 1920)),
            "Height": str(t.get("height", 1080)),
            "Language": t.get("language", "es"),
        })
    sys.stdout.write(json.dumps({"media": {"track": tracks}}))
    return 0


def mkvextract(sc):
    """`mkvextract <mkv> chapters --simple`: capítulos en formato OGM."""
    positional = [a for a in ARGV if not a.startswith("-") and a != "chapters"]
    name = Path(positional[0]).name if positional else ""
    caps = sc.get("chapters", {}).get(name, [])
    for i, (ts, titulo) in enumerate(caps, start=1):
        sys.stdout.write(f"CHAPTER{i:02d}={ts}\n")
        sys.stdout.write(f"CHAPTER{i:02d}NAME={titulo}\n")
    return 0


def mkvpropedit(sc):
    """`mkvpropedit` edita cabeceras in-place: no produce fichero nuevo."""
    sys.stdout.write("The changes are written to the file.\n")
    return 0


def ffprobe(sc):
    media = sc.get("media", {})
    positional = [a for a in ARGV if not a.startswith("-")]
    name = Path(positional[-1]).name if positional else ""
    info = media.get(name, {})
    joined = " ".join(ARGV)

    # Conteo de paquetes por pista de subtítulos: una línea por paquete con el
    # índice del stream. Es como Fase B separa forzados de completos.
    if "-show_packets" in ARGV:
        for idx, n in sorted(sc.get("pgs", {}).get(name, {}).items()):
            for _ in range(int(n)):
                sys.stdout.write(f"{idx}\n")
        return 0
    if "nb_frames" in joined:
        sys.stdout.write("%d\n" % int(info.get("frames", 1000)))
    elif "format=duration" in joined:
        sys.stdout.write("%.3f\n" % float(info.get("duration", 7200.0)))
    return 0


def stream_bytes(props):
    """El "HEVC" que viaja por un pipe, con sus props en cabecera.

    Por un pipe no hay sidecar al que mirar, así que las props van en el
    propio stream: es lo que permite que el consumidor sepa qué RPU
    contiene lo que está leyendo.
    """
    return b"#HARNESS-PROPS " + json.dumps(props).encode("utf-8") + b"\n" + FILLER


def props_from_stream(datos, sc):
    cab = b"#HARNESS-PROPS "
    if datos.startswith(cab):
        try:
            d = dict(DEFAULT_PROPS)
            d.update(json.loads(datos.split(b"\n", 1)[0][len(cab):]))
            return d
        except ValueError:
            pass
    return dict(DEFAULT_PROPS)


def ffmpeg(sc):
    props = read_props(opt("-i"), sc)
    positional = [a for a in ARGV if not a.startswith("-")]
    salidas = []
    if opt("-f") == "tee" and positional:
        # Muxer tee, el del pipe de Fase A: una pasada, varias salidas.
        # "[f=hevc]/ruta/source.hevc|[f=hevc]pipe:1"
        for trozo in positional[-1].split("|"):
            if trozo.startswith("["):
                trozo = trozo.split("]", 1)[-1]
            salidas.append(trozo)
    elif positional:
        ultimo = positional[-1]
        if ultimo not in ("hevc", "copy", "tee") and not ultimo.startswith("0:"):
            salidas.append(ultimo)
    for destino in salidas:
        if destino.startswith("pipe:") or destino == "-":
            # A stdout, como el ffmpeg real. Tratar "pipe:1" como una ruta
            # creaba un fichero llamado así en el directorio del test.
            sys.stdout.buffer.write(stream_bytes(props))
            sys.stdout.buffer.flush()
        else:
            produce(destino, props)
    sys.stderr.write("frame= 1000 fps=100 speed=2.0x\n")
    return 0


def mkvmerge(sc):
    if "-J" in ARGV:
        positional = [a for a in ARGV if not a.startswith("-")]
        name = Path(positional[-1]).name if positional else ""
        spec = sc.get("mkvs", {}).get(name)
        if spec is None:
            # Sin declarar: un MKV mínimo válido (una pista de vídeo).
            spec = {"tracks": [{"type": "video", "codec": "HEVC/H.265/MPEG-H",
                                "dimensions": "3840x2160", "fps": 23.976}],
                    "title": "", "duration_s": 7200.0}
        out = []
        for i, t in enumerate(spec["tracks"]):
            props = {
                "language": t.get("language", "und"),
                "track_name": t.get("track_name", ""),
                "default_track": bool(t.get("default", False)),
                "forced_track": bool(t.get("forced", False)),
            }
            if t.get("dimensions"):
                props["pixel_dimensions"] = t["dimensions"]
            if t.get("fps"):
                # mkvmerge expone default_duration en ns por frame.
                props["default_duration"] = int(round(1e9 / float(t["fps"])))
            if t.get("channels"):
                props["audio_channels"] = t["channels"]
            if t.get("sample_rate"):
                props["audio_sampling_frequency"] = t["sample_rate"]
            out.append({"id": i, "type": t["type"],
                        "codec": t.get("codec", ""), "properties": props})
        sys.stdout.write(json.dumps({
            "container": {"supported": True, "properties": {
                "title": spec["title"],
                "duration": int(spec["duration_s"] * 1e9),
            }},
            "tracks": out,
            "chapters": [],
        }))
        return 0
    out = opt("-o")
    if out:
        # El MKV final hereda el RPU del HEVC que se multiplexa: es el primer
        # positional tras las opciones de salida.
        positional = [a for a in ARGV if not a.startswith("-")]
        hevc = None
        for p in positional:
            if p.endswith(".hevc"):
                hevc = p
                break
        produce(out, read_props(hevc, sc))
    sys.stdout.write("#GUI#progress 100%\n")
    return 0


sys.exit(main())
'''
