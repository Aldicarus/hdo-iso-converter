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
                 "mediainfo", "mkvextract", "mkvpropedit", "mount", "umount")


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
    # Lo que leen los trust gates de la Fase B. El summary del fake escribía
    # estas tres líneas en una forma que `_parse_dovi_summary` no reconoce
    # («L5 offsets: 0, 0, 0, 0»), así que en TODOS los tests llegaban a 0 y
    # los gates L1/L5/L6 comparaban cero contra cero.
    l1_max_cll: float = 1000.6
    l1_max_fall: float = 138.55
    l6_max_cll: int = 1000
    l6_max_fall: int = 400
    # top, bottom, left, right. El 138 es el que el export por niveles emitió
    # siempre: el summary decía 0 y el export 138 del MISMO RPU, y hoy los
    # dos salen de aquí (revisión de Codex del 2026-10-17).
    l5: tuple = (138, 138, 0, 0)
    # Los target displays del L8 (índices de la spec de Dolby). Uno, el 1
    # (100 nits), como Supergirl. Era la mitad de un dato repartido en dos
    # sitios: el summary anunciaba siempre cuatro targets (100, 600, 1000 y
    # 2000 nits) y el export de ese MISMO RPU traía uno — y el contrato lo
    # tapaba quitando justo esos campos de la comparación (auditoría de la
    # suite con Codex del 2026-10-17, P7 #8). Hoy los dos salen de aquí, o
    # de `define_rpu_levels(l8_indices=...)` si el test los declara.
    l8_indices: tuple = (1,)

    def to_summary(self) -> str:
        """Reconstruye el texto que `_parse_dovi_summary` sabe leer.

        El formato sigue los regex de `phases/phase_a._parse_dovi_summary`:
        P8 se anuncia como "Profile: 8.1" (sin sufijo de capa) y P7 lleva
        "(FEL)" o "(MEL)".
        """
        # «Profile: 8», sin el «.1»: es lo que escribe dovi_tool 2.3.3 sobre
        # un RPU P8 (capturado de `RPU_merged_p81.bin`, 2026-10-17).
        if self.profile == 8:
            profile_line = "Profile: 8"
        elif self.el_type:
            profile_line = f"Profile: {self.profile} ({self.el_type})"
        else:
            profile_line = f"Profile: {self.profile}"
        # El FORMATO REAL de dovi_tool 2.3.3, copiado del NAS (2026-10-17,
        # `RPU_source.bin` y `RPU_target.bin` de Supergirl): un bloque
        # «Summary:» con las líneas sangradas y la unidad detrás de cada valor.
        dm = 2 if self.cm_version == "v4.0" else 1
        top, bottom, left, right = self.l5
        lines = [
            "Parsing RPU file...",
            "",
            "Summary:",
            f"  Frames: {self.frames}",
            "  " + profile_line,
            f"  DM version: {dm} (CM {self.cm_version})",
            f"  Scene/shot count: {self.scenes}",
            "  RPU mastering display: 0.0001/1000 nits",
            f"  RPU content light level (L1): MaxCLL: {self.l1_max_cll:.2f} nits, "
            f"MaxFALL: {self.l1_max_fall:.2f} nits",
            "  L6 metadata: Mastering display: 0.0001/1000 nits. "
            f"MaxCLL: {self.l6_max_cll} nits, MaxFALL: {self.l6_max_fall} nits",
            f"  L5 offsets: top={top}, bottom={bottom}, left={left}, right={right}",
            "  L2 trims: 100 nits, 600 nits, 1000 nits",
        ]
        nits = nits_de_l8(self.l8_indices) if self.has_l8 else []
        if nits:
            lines.append("  L8 trims: " + ", ".join(f"{n} nits" for n in nits))
        if self.has_l8:
            # Formato REAL, copiado de `dovi_tool info --summary` sobre un
            # bin retail CMv4.0 del repo (2026-09-23): la línea es
            # `L9 MDP: DCI-P3 D65`. Aquí decía `L9 source primaries:`, una
            # forma que dovi_tool **no escribe**, así que el parser la casaba
            # en los tests y fallaba en el NAS — el fake hacía pasar en verde
            # un camino muerto.
            lines.append("  L9 MDP: DCI-P3 D65")
        # Y de L11 **no hay línea**: el summary real emite exactamente cuatro
        # de niveles (L5 offsets, L2 trims, L8 trims, L9 MDP). El
        # `l11_content_type` solo puede salir de `export --levels level11`,
        # que es de donde lo lee ahora `_medir_niveles_del_export`.
        return "\n".join(lines) + "\n"

    def as_dict(self) -> dict:
        return {
            "profile": self.profile, "el_type": self.el_type,
            "cm_version": self.cm_version, "frames": self.frames,
            "scenes": self.scenes, "has_l8": self.has_l8,
            "has_l11": self.has_l11,
            "l1_max_cll": self.l1_max_cll, "l1_max_fall": self.l1_max_fall,
            "l6_max_cll": self.l6_max_cll, "l6_max_fall": self.l6_max_fall,
            "l5": list(self.l5),
            "l8_indices": list(self.l8_indices),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RpuProps":
        campos = {k: v for k, v in d.items() if k in cls.__annotations__}
        for k in ("l5", "l8_indices"):
            if k in campos:
                campos[k] = tuple(campos[k])
        return cls(**campos)


def tabla_l8() -> dict[int, int]:
    """La tabla índice → nits de producción: el fake no lleva la suya."""
    from phases.rpu_analyze import L8_NITS_POR_INDICE
    return dict(L8_NITS_POR_INDICE)


def nits_de_l8(indices) -> list[int]:
    """Los nits de unos índices L8, ordenados y sin repetir, como los escribe
    `dovi_tool info --summary`. Un índice fuera de la tabla no da línea."""
    tabla = tabla_l8()
    return sorted({tabla[int(i)] for i in indices if int(i) in tabla})


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
                                "pgs": {}, "levels": {},
                                # La tabla de producción, para que el binario
                                # falso escriba los nits del summary sin
                                # importar la app.
                                "l8_nits": {str(k): v for k, v in tabla_l8().items()}}
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
                          l11_content_type: int | None = None,
                          l5_pattern: list | None = None,
                          l3_combos: int | None = None) -> None:
        """Los niveles que `dovi_tool export --levels` devolverá para un RPU.

        Formatos verificados contra RPUs reales del repo DoviTools:
          level5  → {"frame", "active_area_{top,bottom,left,right}_offset"}
          level8  → {"frame", "length", "target_display_index", "trim_*"}
          level3  → {"frame", "min_pq_offset", "max_pq_offset", "avg_pq_offset"}
          level9  → {"frame", "length", "source_primary_index"}
          level11 → {"frame", "content_type", "whitepoint",
                     "reference_mode_flag"}

        `l9_primary` es 0 (BT.709) en los RPUs reales, así que `None` y `0`
        tienen que poder distinguirse: pasar 0 declara el nivel, no lo omite.

        `l3_combos` es cuántos valores distintos de `avg_pq_offset` emitir:
        `1` describe un bin con L3 constante (The Amateur: un solo combo en
        176.448 frames) y un número alto, uno con grading por escena
        (Zootopia 2: 1.489). `None` = el RPU no trae L3, que es lo que pasa
        con el de un BD CM v2.9 — comprobado, su export sale vacío.

        `l5_pattern` describe el perfil L5 por tramos de frames INCLUSIVOS,
        con los offsets en el orden `[top, bottom, left, right]`:

            tb.define_rpu_levels("RPU_source.bin", l5_pattern=[
                [24, 1000, [275, 275, 0, 0]],
                [1500, 1999, [275, 275, 0, 0]],
            ])

        Los frames que NINGÚN tramo cubre (aquí 0-23 y 1001-1499) **no emiten
        registro alguno**, que es justo lo que hay que poder expresar: un RPU
        real puede no llevar bloque L5 en algunos frames y entonces el
        decodificador asume el neutro (0,0,0,0). Medido sobre el Blu-ray de
        "The Mandalorian and Grogu": 113.247 frames con bloque (275,275,0,0)
        y 76.774 **sin bloque ninguno** (las escenas expandidas tipo IMAX).

        Con `l5_pattern` el falso recorre el frame count declarado del RPU y
        emite el número de frame REAL; sin él conserva su salida de siempre
        (los primeros 200 frames con offsets 138), de la que dependen otros
        tests.
        """
        self._scenario.setdefault("levels", {})[name] = {
            "l8_indices": l8_indices,
            "l9_primary": l9_primary,
            "l11_content_type": l11_content_type,
            "l5_pattern": l5_pattern,
            "l3_combos": l3_combos,
        }
        self._flush()

    def define_media(self, name: str, duration: float = 7200.0,
                     frames: int | None = 1000) -> None:
        """Duración y frame count que `ffprobe` reportará para un fichero.

        En un MKV, `frames` viaja en la etiqueta `NUMBER_OF_FRAMES`, como en
        el real (`nb_frames` no viene nunca); `frames=None` es un MKV sin
        etiquetas de estadísticas, y entonces el pipeline cae a estimarlo
        por duración.
        """
        self._scenario["media"][name] = {"duration": duration, "frames": frames}
        self._flush()

    def define_mkv(self, name: str, *, tracks: list[dict] | None = None,
                   title: str = "", duration_s: float = 7200.0,
                   playlist_size: int | None = None) -> None:
        """Las pistas que `mkvmerge -J` reportará para un MKV.

        Cada pista es un dict con `type` ("video"/"audio"/"subtitles") y los
        campos que `analyze_mkv` lee: `codec`, `language`, `track_name`,
        `default`, `forced`, `channels`, `dimensions`, `fps`.

        Dos campos más, que existen en el mkvmerge de verdad y aquí faltaban:
        `id` (si no se da, la posición — pero en un origen Blu-ray los ids NO
        empiezan en 0 ni son contiguos) y `multiplexed_tracks`, la lista con la
        que mkvmerge señala que un TrueHD y su core AC-3 viajan en el mismo
        stream. Sin ese segundo campo, el fake describía un disco en el que los
        cores no existen y NINGÚN test podía ver que Fase E los tomaba por
        pistas independientes.

        `playlist_size` declara que el fichero es un MPLS: mkvmerge añade
        entonces `playlist`, `playlist_duration` y `playlist_size` —los bytes
        que reproduce el playlist— a las propiedades del contenedor, que es
        con lo que la Fase A distingue la película de un tráiler.

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
            "playlist_size": playlist_size,
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
             stderr: str = "fallo simulado", senal: int | None = None,
             tras_producir: bool = False) -> None:
        """Fuerza un código de retorno para una invocación concreta.

        `senal` hace que el binario falso se MATE con esa señal en vez de salir
        con un código. Es la única forma de reproducir el crash de mkvmerge con
        playlists UHD multi-segmento: aborta con SIGABRT, y `returncode` llega
        como **-6**. El chequeo `>= 2` que había antes no capturaba los códigos
        negativos, así que el fallo se enmascaraba y reventaba aguas abajo con
        un críptico "[Errno 2] No such file or directory" (Avatar Fuego y
        Ceniza, 2025).

        **Usa SIGTERM, no SIGABRT ni SIGSEGV.** Al código bajo prueba solo le
        importa que el `returncode` sea negativo, y en macOS morir con una de
        esas dos dispara el informador de fallos del sistema: un `.ips` en
        ~/Library/Logs/DiagnosticReports y una notificación de "Python se ha
        cerrado inesperadamente" POR CADA test. Pasar la suite dejaba una
        ráfaga de avisos que no significaban nada.
        """
        self._scenario["fail"][f"{binary}:{subcommand}"] = {
            "rc": rc, "stderr": stderr, "senal": senal,
            "tras_producir": tras_producir}
        self._flush()

    def fail_when_arg(self, binary: str, subcadena: str, rc: int = 1,
                      stderr: str = "fallo simulado",
                      senal: int | None = None,
                      tras_producir: bool = False) -> None:
        """Falla solo las invocaciones cuyo argv contenga `subcadena`.

        Modela un fallo que depende del FICHERO de entrada, no del comando. El
        caso que lo pide es el reintento con el M2TS de Tab 1: mkvmerge aborta
        sobre el `.mpls` de un disco UHD multi-segmento y funciona sobre el
        `.m2ts`, así que el orquestador reintenta una vez. Con un `fail`
        permanente no se puede distinguir el primer intento del segundo, y sin
        distinguirlos no se puede comprobar que el reintento ocurre —ni que
        ocurre UNA sola vez.
        """
        self._scenario.setdefault("fail_arg", []).append({
            "binary": binary, "subcadena": subcadena,
            "rc": rc, "stderr": stderr, "senal": senal,
            "tras_producir": tras_producir})
        self._flush()

    def retrasar_cuando_arg(self, binary: str, subcadena: str, segundos: float, *,
                            parcial: bool = False) -> None:
        """El binario TARDA cuando su argv contiene `subcadena`.

        Para lo que solo se puede probar con un proceso vivo: cancelar mata
        el subproceso en curso, y un fake que termina en milisegundos ya ha
        salido cuando llega la cancelación.

        Con `parcial`, ANTES de esperar escribe unos bytes en su `-o`: es lo
        que hace un mkvmerge de verdad a mitad del mux. Sin eso, «no queda
        nada a medias» tras cancelar no prueba ninguna limpieza —no había
        nada que limpiar—, que es justo lo que señaló la revisión de Codex
        del 2026-10-17."""
        self._scenario.setdefault("retraso_arg", []).append({
            "binary": binary, "subcadena": subcadena, "segundos": segundos,
            "parcial": parcial})
        self._flush()

    def define_iso(self, nombre: str, *, mpls: dict[str, int] | None = None) -> None:
        """Lo que el `mount` falso deja en el punto de montaje para ese ISO:
        `BDMV/PLAYLIST/<mpls>` con su tamaño y `BDMV/STREAM`. Sin `mpls`, el
        ISO no lleva BDMV (un ISO que no es un Blu-ray)."""
        self._scenario.setdefault("isos", {})[nombre] = {"mpls": mpls}
        self._flush()

    def mkvmerge_sin_salida(self) -> None:
        """`mkvmerge` sale con 0 pero NO escribe el fichero de salida.

        Es un fallo real y silencioso, y la razón de que las fases D y E tengan
        un guard explícito de existencia del output: sin él, el `stat()` de
        después revienta con un críptico "[Errno 2] No such file or directory"
        en vez de decir qué pasó. No se puede simular con `fail`, porque el
        código de salida tiene que ser 0.
        """
        self._scenario["mkvmerge_sin_salida"] = True
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

    def ediciones_de(self, path: Path) -> list[dict]:
        """El rastro de correcciones (`remove`/`duplicate`) que el editor
        falso dejó en un RPU, en el orden en que se aplicaron."""
        try:
            with open(path, "rb") as f:
                primera = f.readline()
        except OSError:
            return []
        cab = b"#HARNESS-PROPS "
        if not primera.startswith(cab):
            return []
        return json.loads(primera[len(cab):]).get("ediciones") or []

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

    Si se pasan `props`, van en la CABECERA del contenido —como hace
    `produce`— y además en su sidecar. Solo con el sidecar, que va indexado
    por ruta, un artefacto que el pipeline renombra (el `.mkv.tmp` → `.mkv`
    de la Fase H) volvía a los valores por defecto: la firma escribía un
    CMv2.9 inventado en la cabecera y un segundo disparo de la fase leía eso.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    for n in names:
        p = workdir / n
        if props is None:
            p.write_bytes(_FILLER)
        else:
            p.write_bytes(b"#HARNESS-PROPS " + json.dumps(props.as_dict()).encode("utf-8")
                          + b"\n" + _FILLER)
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
    # Ver `RpuProps`: lo que leen los trust gates de la Fase B.
    "l1_max_cll": 1000.6, "l1_max_fall": 138.55,
    "l6_max_cll": 1000, "l6_max_fall": 400, "l5": [138, 138, 0, 0],
    # Los target displays L8 (ver `RpuProps.l8_indices`).
    "l8_indices": [1],
    # El `SegmentUID` de Matroska, donde va la marca de procedencia. Viaja
    # con el resto de props —o sea en la cabecera del CONTENIDO— porque el
    # pipeline mueve el fichero (`.mkv.tmp` -> `.mkv` de la Fase H) y un
    # sidecar indexado por ruta se queda apuntando al nombre viejo.
    "segment_uid": "",
    # El título del contenedor, `None` mientras nadie lo haya tocado: es la
    # señal de «manda el escenario». Está aquí porque `mkvpropedit --set
    # title=` tiene que verse después en `mkvmerge -J`, y sin eso ningún test
    # puede comprobar qué sobrevive a una edición de metadatos.
    "title": None,
    # Un stream SIN RPU: lo que sale de sacar con ffmpeg un vídeo de un m2ts
    # que no es la Enhancement Layer. `extract-rpu` falla sobre él igual que
    # el real (ver `dovi_tool`), y es lo que deja ver qué stream se muestreó.
    "sin_rpu": False,
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
        return _con_l8_declarado(cab, path, sc)
    mp = meta_path(path)
    if mp.exists():
        d = dict(DEFAULT_PROPS)
        d.update(json.loads(mp.read_text()))
        return _con_l8_declarado(d, path, sc)
    d = dict(DEFAULT_PROPS)
    d.update(sc.get("rpus", {}).get(path.name, {}))
    return _con_l8_declarado(d, path, sc)


def _con_l8_declarado(props, path, sc):
    """Los targets L8 que el test declaró con `define_rpu_levels` para ESTE
    nombre pasan a las props, y con ellas a todo lo que salga del fichero:
    un RPU editado, inyectado o renombrado conserva sus targets, en el
    summary y en el export."""
    decl = ((sc or {}).get("levels", {}).get(path.name) or {}).get("l8_indices")
    if decl is not None:
        props = dict(props)
        props["l8_indices"] = [int(i) for i in decl]
        props["l8_declarado"] = True
    return props


def indices_l8(props):
    """Los target displays L8 de un RPU. Summary, export e `info -f` los leen
    de aquí: eran tres datos distintos del mismo fichero. Lo declarado manda;
    sin declarar, un RPU sin L8 no tiene ninguno."""
    if not props.get("l8_declarado") and not props.get("has_l8"):
        return []
    return [int(i) for i in (props.get("l8_indices") or [])]


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


def summary(props, sc=None):
    if props["profile"] == 8:
        profile_line = "Profile: 8"
    elif props["el_type"]:
        profile_line = "Profile: %d (%s)" % (props["profile"], props["el_type"])
    else:
        profile_line = "Profile: %d" % props["profile"]
    # Formato real de dovi_tool 2.3.3: ver `RpuProps.to_summary`.
    dm = 2 if props["cm_version"] == "v4.0" else 1
    top, bottom, left, right = props.get("l5") or [138, 138, 0, 0]
    lines = [
        "Parsing RPU file...",
        "",
        "Summary:",
        "  Frames: %d" % props["frames"],
        "  " + profile_line,
        "  DM version: %d (CM %s)" % (dm, props["cm_version"]),
        "  Scene/shot count: %d" % props["scenes"],
        "  RPU mastering display: 0.0001/1000 nits",
        "  RPU content light level (L1): MaxCLL: %.2f nits, MaxFALL: %.2f nits"
        % (props["l1_max_cll"], props["l1_max_fall"]),
        "  L6 metadata: Mastering display: 0.0001/1000 nits. "
        "MaxCLL: %d nits, MaxFALL: %d nits" % (props["l6_max_cll"], props["l6_max_fall"]),
        "  L5 offsets: top=%d, bottom=%d, left=%d, right=%d" % (top, bottom, left, right),
        "  L2 trims: 100 nits, 600 nits, 1000 nits",
    ]
    tabla = (sc or {}).get("l8_nits") or {}
    nits = sorted({int(tabla[str(i)]) for i in indices_l8(props) if str(i) in tabla})
    if nits:
        lines.append("  L8 trims: " + ", ".join("%d nits" % n for n in nits))
    if props["has_l8"]:
        # Ver `RpuProps.to_summary`: el formato real es `L9 MDP: ...` y de
        # L11 no hay linea. Las dos copias tienen que decir lo mismo y hay
        # un test que las cruza — esta se quedo atras y costo un ciclo.
        lines.append("  L9 MDP: DCI-P3 D65")
    return "\n".join(lines) + "\n"


def rows_l5(patron, frames):
    """Registros L5 de un `l5_pattern` — ver `FakeToolbox.define_rpu_levels`.

    Cada tramo es `[desde, hasta, [top, bottom, left, right]]` con los
    extremos INCLUSIVOS. Lo que hace útil esto es lo que NO emite: los
    frames que ningún tramo cubre se quedan sin registro, igual que un RPU
    real que omite el bloque L5 y deja que el decodificador asuma el neutro.
    El número de frame es el REAL, no el índice de la lista.
    """
    filas = []
    for tramo in patron:
        desde, hasta, offsets = tramo[0], tramo[1], tramo[2]
        top, bottom, left, right = offsets
        for f in range(max(0, int(desde)), min(int(hasta), int(frames) - 1) + 1):
            filas.append({
                "frame": f,
                "active_area_left_offset": left,
                "active_area_right_offset": right,
                "active_area_top_offset": top,
                "active_area_bottom_offset": bottom,
            })
    filas.sort(key=lambda r: r["frame"])
    return filas


def record(json_args=None):
    with (STATE / "calls.jsonl").open("a") as f:
        f.write(json.dumps({
            "binary": BINARY, "argv": ARGV, "json_args": json_args or {},
        }) + "\n")


def main():
    global DESPACHO
    DESPACHO = {"ffmpeg": ffmpeg, "ffprobe": ffprobe, "mkvmerge": mkvmerge,
                "mediainfo": mediainfo, "mkvextract": mkvextract,
                "mkvpropedit": mkvpropedit}
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

    for regla in sc.get("retraso_arg", []):
        if regla["binary"] == BINARY and any(regla["subcadena"] in a for a in ARGV):
            import time as _time
            salida = opt("-o")
            if regla.get("parcial") and salida:
                Path(salida).parent.mkdir(parents=True, exist_ok=True)
                Path(salida).write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 4096)
            _time.sleep(float(regla["segundos"]))

    if forced is None:
        for rule in sc.get("fail_arg", []):
            if rule["binary"] != BINARY:
                continue
            if any(rule["subcadena"] in a for a in ARGV):
                forced = rule
                break

    if forced is None:
        for rule in sc.get("fail_json", []):
            if rule["binary"] != BINARY or rule["subcommand"] != sub:
                continue
            if all(json_args.get(k) == v for k, v in rule["json_match"].items()):
                forced = rule
                break

    if forced and forced.get("tras_producir"):
        # Hace su trabajo y DESPUÉS falla: así queda el fichero de salida a
        # medias, que es lo que pasa cuando mkvmerge aborta durante el mux. Sin
        # esto no se puede comprobar que alguien limpia el parcial.
        #
        # `dovi_tool` no estaba en el despacho, así que con él `tras_producir`
        # no hacía nada: un demux que muere a mitad no dejaba BL/EL, y el test
        # de «el demux fallido no deja parciales» pasaba sin parciales que
        # limpiar (auditoría del 2026-10-17).
        DESPACHO["dovi_tool"] = lambda s: dovi_tool(s, sub, json_args)
        try:
            DESPACHO[BINARY](sc) if BINARY in DESPACHO else None
        except Exception:
            pass
        # Lo que escribió por stdout también es su trabajo: sin vaciarlo, morir
        # por señal se lo lleva del buffer y un `mkvmerge -J` que llegó a dar
        # su JSON parecería no haber dicho nada.
        sys.stdout.flush()

    if forced:
        sys.stderr.write(forced.get("stderr", "fallo simulado"))
        sys.stderr.flush()
        senal = forced.get("senal")
        if senal:
            # Morir por señal, no por código: `returncode` llega negativo.
            import signal as _sig
            os.kill(os.getpid(), int(senal))
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
    if BINARY == "mount":
        return mount(sc)
    if BINARY == "umount":
        return umount(sc)
    return 0


def mount(sc):
    """`mount -t udf -o ro,loop <iso> <punto>`: deja en el punto lo que el
    test declaró con `define_iso`. Un ISO sin declarar se monta vacío, como
    uno que no es un Blu-ray."""
    positional = [a for a in ARGV if not a.startswith("-") and a not in ("udf", "ro,loop")]
    if len(positional) < 2:
        sys.stderr.write("mount: bad usage\n")
        return 1
    iso, punto = Path(positional[0]), Path(positional[1])
    if not iso.exists():
        sys.stderr.write("mount: %s: special device does not exist.\n" % iso)
        return 32
    decl = sc.get("isos", {}).get(iso.name) or {}
    if decl.get("mpls") is not None:
        lista = punto / "BDMV" / "PLAYLIST"
        lista.mkdir(parents=True, exist_ok=True)
        (punto / "BDMV" / "STREAM").mkdir(parents=True, exist_ok=True)
        for nombre, tam in decl["mpls"].items():
            (lista / nombre).write_bytes(b"\x00" * int(tam))
    (STATE / "montados.txt").open("a").write(str(punto) + "\n")
    return 0


def umount(sc):
    """Vacía el punto de montaje: lo que el ISO mostraba deja de estar.

    SOLO si es un punto montado por el `mount` falso: el real sale con 32
    («not mounted») ante cualquier otra ruta. Vaciaba lo que le dieran, así
    que desmontar `/mnt/bd` entero en vez del punto pasaba en verde
    (revisión de Codex del 2026-10-17)."""
    positional = [a for a in ARGV if not a.startswith("-")]
    if not positional:
        return 1
    punto = Path(positional[-1])
    montados_f = STATE / "montados.txt"
    montados = montados_f.read_text().splitlines() if montados_f.exists() else []
    if str(punto) not in montados:
        sys.stderr.write("umount: %s: not mounted.\n" % punto)
        return 32
    montados_f.write_text("".join(m + "\n" for m in montados if m != str(punto)))
    if punto.exists():
        import shutil as _sh
        for hijo in punto.iterdir():
            _sh.rmtree(hijo, ignore_errors=True) if hijo.is_dir() else hijo.unlink()
    return 0


def props_conocidas(ruta, sc):
    """Las props de un fichero si el test las declaró (cabecera, sidecar o
    escenario), o None: `read_props` devuelve los valores POR DEFECTO para un
    fichero del que no se sabe nada, y eso no puede limitar a nadie."""
    if not ruta:
        return None
    cab = props_in_file(Path(ruta))
    if cab is not None:
        return cab
    if meta_path(Path(ruta)).exists() or Path(ruta).name in sc.get("rpus", {}):
        return read_props(ruta, sc)
    return None


def _falta(ruta):
    """El error del `dovi_tool` real con una entrada que no existe. Sin esto
    el fake contestaba con las props del escenario sobre un fichero que no
    estaba: un paso que leyera un artefacto equivocado pasaba en verde."""
    if ruta and ruta != "-" and not Path(ruta).exists():
        sys.stderr.write("Error: No such file or directory (os error 2)\n")
        return True
    return False


def dovi_tool(sc, sub, json_args):
    if sub in ("inject-rpu", "editor", "export") and _falta(opt("-i")):
        return 1
    if sub == "inject-rpu" and _falta(opt("--rpu-in")):
        return 1
    if sub == "mux" and (_falta(opt("--bl")) or _falta(opt("--el"))):
        return 1
    if sub == "info" and opt("-f") is not None and _falta(opt("-i")):
        return 1
    if sub == "editor" and json_args.get("allow_cmv4_transfer") \
            and _falta(json_args.get("source_rpu")):
        # El donante del transfer también es una entrada: sin comprobarlo, un
        # escenario que declarase props CMv4.0 para un nombre que no existe
        # producía un merge «correcto» (revisión de Codex, 2026-10-17).
        return 1
    if sub == "info" and opt("-f") is not None:
        # **`info -f N` NO es el summary: imprime UN frame en JSON.**
        # Cayendo en la rama de abajo escribía el summary, así que el
        # consumidor —que busca `"Level254"`— no encontraba nada nunca y
        # el test pasaba en verde sin poder ver el dato. El formato es el
        # real: la cabecera «Parsing RPU file...», y el nivel como CLAVE
        # del bloque dentro de `ext_metadata_blocks`.
        props = read_props(opt("-i"), sc)
        bloques = []
        if props.get("cm_version") == "v4.0":
            bloques.append('{"Level254": {"dm_mode": 0, "dm_version_index": 2}}')
        for idx in indices_l8(props)[:1]:
            bloques.append('{"Level8": {"target_display_index": %d}}' % idx)
        sys.stdout.write(
            "Parsing RPU file...\n{\n"
            '  "dovi_profile": %s,\n'
            '  "vdr_dm_data": {"cmv40_metadata": {"ext_metadata_blocks": [%s]}}\n}\n'
            % (props.get("profile", 7), ", ".join(bloques)))
        return 0

    if sub == "info":
        # `info --summary <path>` y `info -s -i <path>` son la misma consulta.
        target = opt("--summary") or opt("-i")
        if target is None:
            # `--summary` puede venir sin valor y el path llegar al final.
            positional = [a for a in ARGV if not a.startswith("-") and a != "info"]
            target = positional[-1] if positional else None
        if _falta(target):
            return 1
        sys.stdout.write(summary(read_props(target, sc), sc))
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
            # Solo los niveles de `rpu_levels` vienen del donante: el resto es
            # del RPU de entrada. El fake copiaba siempre lo mismo, así que
            # ningún test podía ver de DÓNDE salía el L1 de un merge FEL —y
            # la regla «sin confianza se conserva el L1 del disco» era falsa
            # sin que nada lo dijera (auditoría con Codex del 2026-10-17)—.
            # El default es el de dovi_tool: los niveles CMv4.0 más L3.
            niveles = set(json_args.get("rpu_levels") or [3, 8, 9, 11, 254])
            if 1 in niveles:
                props["l1_max_cll"] = donor.get("l1_max_cll", props.get("l1_max_cll"))
                props["l1_max_fall"] = donor.get("l1_max_fall", props.get("l1_max_fall"))
            if 5 in niveles:
                props["l5"] = donor.get("l5", props.get("l5"))
            if 6 in niveles:
                props["l6_max_cll"] = donor.get("l6_max_cll", props.get("l6_max_cll"))
                props["l6_max_fall"] = donor.get("l6_max_fall", props.get("l6_max_fall"))
            if 8 in niveles:
                props["has_l8"] = donor.get("has_l8", True)
            if 11 in niveles:
                props["has_l11"] = donor.get("has_l11", True)
            props["niveles_del_donante"] = sorted(niveles)
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
            # Y QUÉ se ha hecho, en orden. Contar frames no distingue quitar el
            # primero de quitar el último ni duplicar el primero de duplicar el
            # último —que es lo que desplaza o no la película—; con el rastro,
            # un test puede afirmar sobre la corrección que acabó en el RPU.
            props["ediciones"] = list(props.get("ediciones") or []) + [{
                k: json_args[k] for k in ("remove", "duplicate") if json_args.get(k)}]
        produce(out, props)
        return 0

    if sub == "inject-rpu":
        hevc_in = opt("-i")
        rpu_in = opt("--rpu-in")
        out = opt("-o")
        # El HEVC de salida contiene el RPU inyectado, con el frame count del
        # HEVC de entrada: un HEVC recortado da un stream recortado, lleve el
        # RPU los frames que lleve. El fake copiaba el RPU tal cual y un BL o
        # una EL truncada —el demux de Proyecto Salvación— salía entero.
        props = read_props(rpu_in, sc)
        del_hevc = props_conocidas(hevc_in, sc)
        if del_hevc is not None:
            props["frames"] = min(int(props.get("frames") or 0),
                                  int(del_hevc.get("frames") or 0))
        produce(out, props)
        sys.stdout.write("Processing input video for frame order info...\n")
        sys.stdout.write("Rewriting file with interleaved RPU NALs...\n")
        return 0

    if sub == "demux":
        positional = [a for a in ARGV if not a.startswith("-") and a != "demux"]
        src = positional[0] if positional else None
        if _falta(src):
            return 1
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
        # El dual-layer resultante lleva el RPU de la capa de mejora, y tantos
        # frames como la capa MÁS CORTA: un BL truncado no se arregla porque
        # la EL esté entera.
        props = read_props(el, sc)
        del_bl = props_conocidas(opt("--bl"), sc)
        if del_bl is not None:
            props["frames"] = min(int(props.get("frames") or 0),
                                  int(del_bl.get("frames") or 0))
        produce(out, props)
        return 0

    if sub == "extract-rpu":
        # `-` es la entrada por stdin, no una opción: sin la excepción se
        # filtraba como tal y el modo pipe de la Fase A nunca se reconocía.
        # Las opciones con valor se saltan con su valor: `--limit 720` dejaba
        # el `720` como primer posicional, el fake lo tomaba por el fichero de
        # entrada y devolvía los valores POR DEFECTO. Todo muestreo con
        # `--limit` —el de Tab 2 y el del MKV entregado de la Fase H— leía
        # un CMv2.9 inventado en vez del RPU del fichero.
        con_valor = {"--limit", "-l", "-o", "--rpu-out", "-m", "--mode", "--crop"}
        positional, saltar = [], False
        for a in ARGV:
            if saltar:
                saltar = False
                continue
            if a in con_valor:
                saltar = True
                continue
            if (a == "-" or not a.startswith("-")) and a != "extract-rpu":
                positional.append(a)
        src = positional[0] if positional else None
        out = opt("-o")
        if _falta(src):
            return 1
        if src == "-":
            # Modo pipe (Fase A): hay que sacar del pipe TODO lo que el
            # productor escribió o ffmpeg no puede terminar. Las props
            # llegan en la cabecera del stream, no por sidecar.
            datos = b""
            try:
                datos = sys.stdin.buffer.read()
            except Exception:
                pass
            props = props_from_stream(datos, sc)
        else:
            props = read_props(src, sc)
        if props.get("sin_rpu"):
            # dovi_tool 2.3.3 sobre un HEVC sin RPU, comprobado en el NAS:
            # código 1, ningún fichero de salida y esta línea.
            sys.stderr.write("Error: No RPU was found in input file\n")
            return 1
        produce(out, props)
        return 0

    if sub == "export":
        src = opt("-i")
        props = read_props(src, sc)
        n = min(int(props["frames"]), 200)

        # `-d scenes=<fichero>`: el real escribe **un índice de frame por
        # LÍNEA**, texto plano, aunque el export lleve `-f json` y el destino
        # acabe en `.json` (comprobado en el NAS: 2.585 líneas, la primera un
        # `0` suelto). El pipeline lo pide SIEMPRE, junto con `--levels`, así
        # que va antes de las dos ramas.
        #
        # El fake no lo escribía en ninguna de las dos, así que `scene_cuts`
        # salía 0 en todos los tests de integración y el bug de leerlo con
        # `json.load` no lo podía ver ninguno — el mismo agujero que el
        # `MaxCLL` sin unidad. Escribirlo como array JSON sería peor que no
        # escribirlo: haría pasar en verde un `json.load` que en el NAS falla
        # siempre.
        for a in ARGV:
            if a.startswith("scenes="):
                total = max(1, int(props["frames"]))
                cortes = max(1, min(int(props["scenes"]), total))
                paso = max(1, total // cortes)
                Path(a.split("=", 1)[1]).write_text(
                    "".join(f"{i * paso}\n" for i in range(cortes)))

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
                if lv == "level5" and niveles.get("l5_pattern"):
                    # Perfil L5 declarado por tramos: recorre el frame count
                    # ENTERO del RPU (sin el tope de 200) y deja sin registro
                    # los frames que ningún tramo cubre.
                    Path(dest).write_text(json.dumps(
                        rows_l5(niveles["l5_pattern"], props["frames"])))
                    continue
                rows = []
                for i in range(n):
                    if lv == "level1":
                        rows.append({"frame": i, "min_pq": 0,
                                     "avg_pq": 500, "max_pq": 2000 + i})
                    elif lv == "level5":
                        # Los del summary del mismo RPU: eran constantes.
                        top, bottom, left, right = props.get("l5") or [138, 138, 0, 0]
                        rows.append({"frame": i,
                                     "active_area_left_offset": left,
                                     "active_area_right_offset": right,
                                     "active_area_top_offset": top,
                                     "active_area_bottom_offset": bottom})
                    elif lv == "level6":
                        # `min_display_mastering_luminance` va en 0,0001 nits:
                        # 1 es el 0,0001/1000 que dice el summary (capturado
                        # de Supergirl, 2026-10-17). Faltaba, y el `min_nits`
                        # del perfil de luminancia salía 0 en toda la suite.
                        rows.append({"frame": i,
                                     "max_display_mastering_luminance": 1000,
                                     "min_display_mastering_luminance": 1,
                                     "max_content_light_level": int(props["l6_max_cll"]),
                                     "max_frame_average_light_level": int(props["l6_max_fall"])})
                    elif lv == "level2":
                        # Un registro por (frame, target), como el real. NO
                        # había rama para L2: el export no emitía nunca ni una
                        # fila, y los combos L2 y los trim targets salían
                        # vacíos en toda la suite. Los tres targets del summary
                        # (100/600/1000 nits = PQ 2081/2851/3079).
                        for pq in (niveles.get("l2_targets") or (2081, 2851, 3079)):
                            rows.append({
                                "frame": i, "target_max_pq": pq,
                                "trim_slope": 2048, "trim_offset": 2048,
                                "trim_power": 2048, "trim_chroma_weight": 2048,
                                "trim_saturation_gain": 2048, "ms_weight": 2048,
                            })
                    elif lv == "level8":
                        # Un registro por (frame, target display), como el
                        # real: en Zootopia salen 2 índices × 155.001 frames.
                        # Sin índices declarados, el RPU con L8 trae UNO (el
                        # 1, como Supergirl): el summary del mismo RPU dice
                        # «L8 trims», y un export vacío lo contradecía.
                        # Los MISMOS que anuncia el summary del fichero
                        # (`indices_l8`): ya llevan lo declarado para él.
                        for idx in indices_l8(props):
                            rows.append({
                                "frame": i, "length": 25,
                                "target_display_index": idx,
                                "trim_slope": 2312, "trim_offset": 2059,
                                "trim_power": 2034, "trim_chroma_weight": 2048,
                                "trim_saturation_gain": 2048, "ms_weight": 2048,
                            })
                    elif lv == "level3":
                        # Formato copiado de un bin del repo DoviTools:
                        # `{"frame":0,"min_pq_offset":2048,"max_pq_offset":2047,
                        #   "avg_pq_offset":1589}`. Ojo al `avg_pq_offset`, que
                        # en los RPUs reales **nunca** vale el neutro 2048.
                        n3 = niveles.get("l3_combos")
                        if n3:
                            rows.append({
                                "frame": i, "min_pq_offset": 2048,
                                "max_pq_offset": 2047,
                                "avg_pq_offset": 1589 + (i % int(n3)),
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
            "Width": str(v.get("width", 3840)),
            "Height": str(v.get("height", 2160)),
            # El PID en decimal: 4113 (0x1011) en la BL de los 81 discos del
            # NAS con MediaInfo. Es lo que se cruza con el `number` de mkvmerge.
            "ID": str(v.get("id", 4113)),
        })
        # Otros vídeos del m2ts: un picture-in-picture, o el «AVC 1920x1080»
        # que MediaInfo lista con el MISMO ID que la BL en 7 discos del NAS
        # (Juego de Tronos, La Momia) y que para ffmpeg no es otro stream.
        for x in v.get("extra", []):
            tracks.append({
                "@type": "Video",
                "StreamOrder": str(x.get("stream_order", "0-1")),
                "ID": str(x["id"]),
                "Format": x.get("format", "HEVC"),
                "Width": str(x.get("width", 1920)),
                "Height": str(x.get("height", 1080)),
            })
        # La Enhancement Layer de un Dolby Vision dual-layer: el MediaInfo real
        # la lista como un segundo vídeo HEVC a la mitad de resolución —en los
        # 39 discos dual-layer del NAS, siempre 1920x1080— y con el mismo
        # `HDR_Format` que la BL, así que ese campo no la distingue.
        if v.get("el"):
            tracks.append({
                "@type": "Video",
                "StreamOrder": str(v.get("el_stream_order", "0-1")),
                "ID": str(v.get("el_id", 4117)),
                "Format": "HEVC",
                "Width": "1920",
                "Height": "1080",
                "BitDepth": "10",
                "colour_primaries": "BT.2020",
                "transfer_characteristics": "PQ",
            })
            if v.get("el_id", 4117) is None:
                # `el_id=None`: MediaInfo no da el PID de la EL.
                del tracks[-1]["ID"]
        # Con varias pistas del mismo tipo, MediaInfo numera cada una.
        vids = [t for t in tracks if t["@type"] == "Video"]
        if len(vids) > 1:
            for n, t in enumerate(vids, 1):
                t["@typeorder"] = str(n)
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
    if positional and Path(positional[0]).exists():
        escritos = read_props(positional[0], sc).get("chapters")
        if escritos is not None:
            caps = escritos
    for i, (ts, titulo) in enumerate(caps, start=1):
        sys.stdout.write(f"CHAPTER{i:02d}={ts}\n")
        sys.stdout.write(f"CHAPTER{i:02d}NAME={titulo}\n")
    return 0


def _capitulos_de_xml(ruta):
    """Los capítulos de un XML de Matroska como `[[inicio, nombre], …]`, o
    None si el fichero no es aceptable para el mkvpropedit real.

    El real (comprobado con la v88 del NAS y la v102, 2026-10-17) rechaza con
    código 2 una `EditionEntry` sin ningún `ChapterAtom`: «At least one
    <ChapterAtom> element is needed». Es lo que hacía fallar «Aplicar
    cambios» de Tab 2 sobre cualquier MKV sin capítulos.
    """
    import xml.etree.ElementTree as ET
    try:
        raiz = ET.parse(ruta).getroot()
    except (OSError, ET.ParseError):
        return None
    ediciones = raiz.findall("EditionEntry")
    caps = []
    for ed in ediciones:
        atomos = list(ed.iter("ChapterAtom"))
        if not atomos:
            return None
        for atom in atomos:
            inicio = (atom.findtext("ChapterTimeStart") or "").strip()[:12]
            caps.append([inicio, atom.findtext("ChapterDisplay/ChapterString") or ""])
    return caps if ediciones else None


def _pistas_conocidas(destino, sc):
    """Las pistas del MKV (escenario o cabecera), o None si no se sabe."""
    spec = sc.get("mkvs", {}).get(destino.name)
    if spec is not None:
        return spec["tracks"]
    return read_props(destino, sc).get("tracks")


def mkvpropedit(sc):
    """`mkvpropedit` edita cabeceras in-place: no produce fichero nuevo.

    Entiende `--edit info --set segment-uid=0x...` (la marca de procedencia),
    `--set title=`, `--edit track:N --set name=|flag-default=|flag-forced=` y
    `--chapters`. Solo entendía las dos primeras, así que aplicar los nombres,
    los flags o los capítulos de Tab 2 y de la Fase E no cambiaba nada que el
    `mkvmerge -J` siguiente pudiera ver: invertir el `flag-forced` o poner
    todos los capítulos a cero pasaba en verde (17 de 17 mutaciones de Tab 2
    sobrevivían, revisión de Codex del 2026-10-17).

    Lo que se escribe va a la cabecera del CONTENIDO, como la firma: la Fase H
    renombra el fichero y un sidecar indexado por ruta se queda atrás.
    """
    positional = [a for a in ARGV if not a.startswith("-")]
    destino = Path(positional[0]) if positional else None
    if destino is None or not destino.exists():
        sys.stderr.write("Error: The file '%s' does not exist.\n" % (destino,))
        return 2
    props = read_props(destino, sc)
    pistas = _pistas_conocidas(destino, sc)
    editadas = dict(props.get("pistas_editadas") or {})
    cambios = {}
    seleccion = None
    i = 0
    while i < len(ARGV):
        arg = ARGV[i]
        siguiente = ARGV[i + 1] if i + 1 < len(ARGV) else ""
        if arg == "--edit":
            seleccion = siguiente
            if seleccion.startswith("track:") and seleccion[6:].isdigit() \
                    and pistas is not None and not 1 <= int(seleccion[6:]) <= len(pistas):
                sys.stderr.write("Error: No track corresponding to the edit "
                                 "specification '%s' was found.\n" % seleccion)
                return 2
            i += 2
            continue
        if arg == "--set" and "=" in siguiente:
            clave, valor = siguiente.split("=", 1)
            if clave == "segment-uid":
                cambios["segment_uid"] = valor.removeprefix("0x").lower()
            elif clave == "title":
                cambios["title"] = valor
            elif seleccion and seleccion.startswith("track:"):
                pista = dict(editadas.get(seleccion[6:]) or {})
                if clave == "name":
                    pista["track_name"] = valor
                elif clave == "flag-default":
                    pista["default"] = valor == "1"
                elif clave == "flag-forced":
                    pista["forced"] = valor == "1"
                editadas[seleccion[6:]] = pista
            i += 2
            continue
        if arg == "--chapters":
            if siguiente == "":
                cambios["chapters"] = []          # `--chapters ''` los borra
            else:
                caps = _capitulos_de_xml(siguiente)
                if caps is None:
                    sys.stderr.write("Error: The XML chapter file '%s' contains an "
                                     "error: At least one <ChapterAtom> element is "
                                     "needed.\n" % siguiente)
                    return 2
                cambios["chapters"] = caps
            i += 2
            continue
        i += 1
    if editadas:
        cambios["pistas_editadas"] = editadas
    if cambios:
        props.update(cambios)
        # Solo la primera línea: reescribir el fichero entero cambiaría su
        # tamaño, y hay tests que lo miran. El mkvpropedit real tampoco
        # mueve los datos.
        resto = destino.read_bytes()
        corte = resto.find(b"\n")
        cuerpo = resto[corte + 1:] if resto.startswith(b"#HARNESS-PROPS ") and corte >= 0 else resto
        destino.write_bytes(b"#HARNESS-PROPS " + json.dumps(props).encode("utf-8") + b"\n" + cuerpo)
        write_props(destino, props)
    sys.stdout.write("The changes are written to the file.\n")
    return 0


def ffprobe(sc):
    media = sc.get("media", {})
    positional = [a for a in ARGV if not a.startswith("-")]
    name = Path(positional[-1]).name if positional else ""
    info = media.get(name)
    if info is None:
        # Lo que el escenario declare para ese nombre gana —es la intención
        # del test—; si no declaró nada, el recuento es el del CONTENIDO, como
        # en el real. Sacarlo siempre del escenario (1000 por defecto) hacía
        # que un MKV con medio vídeo dentro contestara la película entera: un
        # mux que truncara pasaba en verde por 244 tests (revisión de Codex
        # de la suite, 2026-10-17).
        del_fichero = props_conocidas(positional[-1], sc) if positional else None
        info = ({"frames": del_fichero.get("frames", 1000),
                 "duration": del_fichero.get("duration", 7200.0)}
                if del_fichero is not None else {})
    joined = " ".join(ARGV)

    # Conteo de paquetes por pista de subtítulos: una línea por paquete con el
    # índice del stream. Es como Fase B separa forzados de completos.
    if "-show_packets" in ARGV:
        for idx, n in sorted(sc.get("pgs", {}).get(name, {}).items()):
            for _ in range(int(n)):
                sys.stdout.write(f"{idx}\n")
        return 0
    if "nb_frames" in joined:
        # El formato del `ffprobe` real (n7.1.5), capturado en el NAS: sobre
        # un MKV, `nb_frames` NO viene nunca —en JSON falta la clave y en CSV
        # sale `N/A,`, con la coma de la side data del DV— y el recuento
        # exacto está en la etiqueta `NUMBER_OF_FRAMES` que escribe mkvmerge.
        # Un HEVC crudo no trae ni lo uno ni lo otro. Este fake devolvía el
        # número a pelo para cualquier fichero, así que la comprobación
        # estricta de frames de la Fase H solo existía en los tests.
        # `frames=None` es un MKV sin etiquetas de estadísticas.
        frames = info.get("frames", 1000)
        # Por la extensión con el `.tmp` de trabajo quitado: el real mira el
        # contenido, y la Fase H sondea el `.mkv.tmp` antes de renombrarlo.
        es_mkv = name.lower().removesuffix(".tmp").endswith(".mkv")
        if "json" in joined:
            st = {}
            if es_mkv:
                st["side_data_list"] = [{}]
                # Las etiquetas solo si se piden: sin `stream_tags` el real
                # no las emite, y volver a pedir solo `nb_frames` pasaba.
                if frames is not None and "stream_tags" in joined:
                    st["tags"] = {"NUMBER_OF_FRAMES": "%d" % int(frames),
                                  "_STATISTICS_TAGS": "BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES"}
            sys.stdout.write(json.dumps({"programs": [], "stream_groups": [],
                                         "streams": [st]}, indent=4) + "\n")
        else:
            sys.stdout.write("N/A,\n" if es_mkv else "N/A\n")
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
    entrada = opt("-i")
    if entrada and entrada != "-" and not entrada.startswith("pipe:") \
            and not Path(entrada).exists():
        sys.stderr.write("%s: No such file or directory\n" % entrada)
        return 1
    props = read_props(entrada, sc)
    # `-map` sobre un m2ts con sus vídeos declarados. El fake lo ignoraba y
    # devolvía la EL saliera el stream que saliera, así que ningún test podía
    # ver que se muestreaba el vídeo equivocado. Los dos errores son los del
    # ffmpeg de producción (n7.1.5), copiados de un TS sintético en el NAS.
    mapa = opt("-map")
    videos = None
    if mapa:
        videos = _videos_del_ts(sc, Path(opt("-i") or "").name)
        if videos is None:
            videos = _videos_del_mkv(sc, entrada, props)
    if videos is not None:
        elegido = _resolver_map(videos, mapa)
        if elegido is None:
            sys.stderr.write("Stream map '' matches no streams.\nTo ignore this, "
                             "add a trailing '?' to the map.\n")
            return 234
        # Lo que llega al filtro es la SALIDA: con `-c:v copy`, el códec del
        # stream; recodificando, el del codificador. Y recodificar con libx265
        # no conserva los NAL del RPU: sale un HEVC sin Dolby Vision.
        # Sin `-c:v` (ni `-c`) ffmpeg RECODIFICA con el codificador por
        # defecto del formato de salida: el fake lo daba por `copy`, y una
        # muestra de la EL sin `-c:v copy` —que en el real sale sin RPU—
        # pasaba en verde (mutación P2-9 de la auditoría del 2026-10-17).
        codificador = opt("-c:v") or opt("-c") or "libx265"
        salida = (elegido["format"] if codificador == "copy" else
                  {"libx265": "HEVC", "libx264": "AVC"}.get(codificador, codificador))
        if salida != "HEVC" and opt("-bsf:v") == "hevc_mp4toannexb":
            sys.stderr.write(
                "[hevc_mp4toannexb @ 0x557b15fb4d40] Codec 'h264' (27) is not "
                "supported by the bitstream filter 'hevc_mp4toannexb'. "
                "Supported codecs are: hevc (173)\n")
            return 234
        if not elegido["el"] or codificador != "copy":
            props = dict(props, sin_rpu=True)
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
    # Progreso en el FORMATO REAL. La línea que había aquí no llevaba `size=`
    # ni `time=`, y son justo los dos campos de los que el pipeline saca el
    # porcentaje: sin ellos, un test podía dar por bueno un camino que en el
    # NAS dejaba la barra clavada en 0 % durante cinco minutos (pasó con el
    # perfil de luminancia, que no escribe fichero de salida y por tanto solo
    # tenía el `size=` de ffmpeg para estimar el total).
    #
    # Formato copiado de un job real del NAS:
    #   frame=207166 fps=1038 q=-1.0 size=72402713KiB time=02:24:00.54 \
    #     bitrate=68644.1kbits/s speed=43.3x
    # y la última con `Lsize=` en vez de `size=`.
    dur = float(props.get("duration") or 7200.0)
    frames_tot = int(props.get("frames") or 1000)
    # `-t N` corta la entrada a N segundos: el progreso del real termina en
    # `time=00:00:N`. El fake lo ignoraba y anunciaba la película entera, así
    # que quitar el `-t 30` de un muestreo —volcar decenas de GB para leer 720
    # frames— no cambiaba nada que un test pudiera ver (revisión de Codex de
    # la suite, 2026-10-17).
    if opt("-t") is not None:
        try:
            limite = float(opt("-t"))
        except ValueError:
            limite = dur
        if limite < dur:
            frames_tot = max(1, int(frames_tot * limite / dur))
            dur = limite
    # Con el muxer `tee` (dos salidas) el real no sabe qué tamaño dar:
    # `size=N/A` en el progreso y `Lsize=N/A` al final (CLAUDE.md: «a ffmpeg
    # no se le puede preguntar cuando escribe con tee»). El fake daba el
    # número igual, así que un consumidor que se fiara del `size=` con `tee`
    # pasaba en verde y en el NAS se quedaba sin barra.
    con_tee = opt("-f") == "tee"
    for paso in range(1, 5):
        frac = paso / 4.0
        seg = dur * frac
        tam = "N/A" if con_tee else "%dKiB" % int(70_000_000 * frac)
        sys.stderr.write(
            "frame=%6d fps=1038 q=-1.0 %s=%s time=%02d:%02d:%05.2f "
            "bitrate=68644.1kbits/s speed=43.3x\n" % (
                int(frames_tot * frac),
                "Lsize" if paso == 4 else "size",
                tam,
                int(seg // 3600), int((seg % 3600) // 60), seg % 60,
            ))
        sys.stderr.flush()
    return 0


def _number_por_defecto(name, tracks, i):
    """El `number` de `mkvmerge -J` cuando el escenario no lo da.

    En un MPEG-TS (`.mpls`, `.m2ts`) es el PID, con la numeración de un UHD
    Blu-ray: vídeo 0x1011 (y 0x1015 el segundo, que es el de la EL), audio
    desde 0x1100 y PGS desde 0x1200. En un MKV, el número de pista.
    """
    if Path(name).suffix.lower() not in (".mpls", ".m2ts", ".ts"):
        return i + 1
    tipo = tracks[i]["type"]
    k = sum(1 for t in tracks[:i] if t["type"] == tipo)
    base = {"video": 0x1011, "audio": 0x1100}.get(tipo, 0x1200)
    return base + (4 * k if tipo == "video" else k)


def _videos_del_ts(sc, name):
    """Los vídeos de un m2ts tal como los ve ffmpeg: uno por PID, en el orden
    del PMT. Salen de lo declarado con `define_mediainfo`, que es la
    descripción del fichero. `None` si el escenario no los detalló (sin `el`
    ni `extra`): entonces el ffmpeg falso no resuelve el `-map`, como antes."""
    if Path(name).suffix.lower() not in (".m2ts", ".ts"):
        return None
    v = (sc.get("mediainfo", {}).get(name) or {}).get("video")
    if not isinstance(v, dict) or not ("el" in v or "extra" in v):
        return None

    def orden(so):
        return int(str(so).split("-")[-1] or 0)

    videos = [{"pid": int(v.get("id", 4113)), "format": v.get("format", "HEVC"),
               "el": False, "orden": 0}]
    for x in v.get("extra", []):
        videos.append({"pid": int(x["id"]), "format": x.get("format", "HEVC"),
                       "el": False, "orden": orden(x.get("stream_order", "0-1"))})
    if v.get("el"):
        el_id = v.get("el_id", 4117)
        # Sin `ID` en MediaInfo el stream sigue existiendo y tiene su PID; lo
        # que no hay es forma de pedirlo por PID desde el test.
        videos.append({"pid": int(el_id) if el_id is not None else None, "format": "HEVC",
                       "el": True, "orden": orden(v.get("el_stream_order", "0-1"))})
    unicos = {}
    for x in sorted(videos, key=lambda x: x["orden"]):
        unicos.setdefault(x["pid"] if x["pid"] is not None else id(x), x)
    return list(unicos.values())


def _videos_del_mkv(sc, ruta, props):
    """Los vídeos de un MKV tal como los ve ffmpeg, o None si no es un MKV.

    Salen de las pistas declaradas con `define_mkv` o de las que escribió el
    mux falso en la cabecera; sin ninguna de las dos, un MKV tiene un vídeo.
    El RPU viaja con cada uno (el mkvmerge actual combina BL+EL en la misma
    pista), así que lo que se modela es que el `-map` exista y que el códec de
    SALIDA sea `copy`: la validación de `-map` y de la recodificación solo
    existía para los m2ts, y quitar los siete `-c:v copy` del pipeline CMv4.0
    —recodificar con libx265 durante días y perder el RPU en el NAS—
    sobrevivía a 222 tests (revisión de Codex de la suite, 2026-10-17)."""
    if not ruta or not Path(ruta).name.lower().removesuffix(".tmp").endswith(".mkv"):
        return None
    spec = sc.get("mkvs", {}).get(Path(ruta).name)
    pistas = spec["tracks"] if spec is not None else props.get("tracks")
    if not pistas:
        pistas = [{"type": "video", "codec": "HEVC/H.265/MPEG-H"}]
    videos = []
    for i, p in enumerate(x for x in pistas if x.get("type") == "video"):
        codec = str(p.get("codec", "HEVC")).upper()
        formato = ("HEVC" if "HEVC" in codec or "H.265" in codec else
                   "AVC" if "AVC" in codec or "H.264" in codec else codec)
        videos.append({"pid": None, "format": formato, "el": True, "orden": i})
    return videos


def _resolver_map(videos, mapa):
    """El vídeo que selecciona `-map`, o None si no casa con ninguno.

    Las dos formas que usa la app: `0:v:N` (el N-ésimo vídeo) y `0:i:PID`
    (por PID, en decimal o `0x…`; ffmpeg acepta también `0:#PID`)."""
    if mapa.startswith("0:v:"):
        n = int(mapa[4:])
        return videos[n] if n < len(videos) else None
    for pre in ("0:i:", "0:#"):
        if mapa.startswith(pre):
            pid = int(mapa[len(pre):], 0)
            return next((x for x in videos if x["pid"] == pid), None)
    return None


def ficheros_de_playlist(mpls):
    """Los m2ts que reproduce un MPLS, como los lista `mkvmerge -J` en su
    `playlist_file`: en el orden de los PlayItems y CON repeticiones
    (`open_multi` de mkvtoolnix, sin deduplicar). `None` si no es un MPLS
    de verdad, o sea, si el test no lo construyó como tal."""
    try:
        data = Path(mpls).read_bytes()
    except OSError:
        return None
    if len(data) < 40 or data[:4] != b"MPLS":
        return None
    try:
        p = int.from_bytes(data[8:12], "big") + 6
        n = int.from_bytes(data[p:p + 2], "big")
        p += 4
        stream = Path(mpls).parent.parent / "STREAM"
        out = []
        for _ in range(n):
            largo = int.from_bytes(data[p:p + 2], "big")
            clip = data[p + 2:p + 7].decode("ascii")
            f = stream / (clip + ".m2ts")
            if f.exists():
                out.append(str(f))
            p += 2 + largo
        return out
    except (UnicodeDecodeError, ValueError):
        return None


# Opciones de mkvmerge que llevan un valor detrás: ese valor no es una entrada.
_CON_VALOR = {
    "-o", "--output", "--title", "--chapters", "--track-order", "--track-name",
    "--language", "--default-track", "--default-track-flag", "--forced-track",
    "--forced-display-flag", "--audio-tracks", "-a", "--subtitle-tracks", "-s",
    "--video-tracks", "-d", "--attachments", "--global-tags", "--tags",
    "--segment-uid", "--sync", "--default-duration", "--timestamps", "--split",
    "--cues", "--compression", "--aspect-ratio", "--display-dimensions",
    "--cropping", "--attach-file", "--attachment-name", "--attachment-mime-type",
    "--chapter-language", "--chapter-charset", "--ui-language", "--track-tags",
    "--identification-format", "-F",
}
_SIN_VIDEO = ("--no-video", "-D")
_SIN_AUDIO = ("--no-audio", "-A")
_SIN_SUBS = ("--no-subtitles", "-S")


def _entradas_del_mux():
    """`[(ruta, opciones)]` en orden. Las opciones de mkvmerge se aplican al
    fichero que las SIGUE; las que tienen valor se guardan como lista."""
    entradas, pendientes, i = [], {}, 0
    while i < len(ARGV):
        a = ARGV[i]
        if a in _CON_VALOR:
            if a not in ("-o", "--output") and i + 1 < len(ARGV):
                pendientes.setdefault(a, []).append(ARGV[i + 1])
            i += 2
            continue
        if a.startswith("-"):
            pendientes[a] = True
            i += 1
            continue
        entradas.append((a, pendientes))
        pendientes, i = {}, i + 1
    return entradas


def _por_id(valores):
    """`["2:Castellano…", "1:Inglés"]` → `{2: "Castellano…", 1: "Inglés"}`."""
    out = {}
    for v in valores or []:
        tid, _, resto = str(v).partition(":")
        if tid.strip().isdigit():
            out[int(tid)] = resto
    return out


def _pistas_del_mux(entradas, sc):
    """Las pistas del MKV de salida y la entrada de la que sale el vídeo."""
    seleccion = {"video": ("--video-tracks", "-d"), "audio": ("--audio-tracks", "-a"),
                 "subtitles": ("--subtitle-tracks", "-s")}
    excluye = {"video": _SIN_VIDEO, "audio": _SIN_AUDIO, "subtitles": _SIN_SUBS}
    pistas, ruta_video = [], None
    for fi, (ruta, op) in enumerate(entradas):
        if ruta.endswith(".hevc"):
            del_fichero = [{"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H",
                            "dimensions": "3840x2160", "fps": 23.976}]
        else:
            spec = (sc.get("mkvs", {}).get(Path(ruta).name)
                    or {"tracks": [{"type": "video", "codec": "HEVC/H.265/MPEG-H",
                                    "dimensions": "3840x2160", "fps": 23.976}]})
            del_fichero = [dict(t, id=t.get("id", i)) for i, t in enumerate(spec["tracks"])]
        nombres = _por_id(op.get("--track-name"))
        idiomas = _por_id(op.get("--language"))
        por_defecto = _por_id(op.get("--default-track") or op.get("--default-track-flag"))
        forzadas = _por_id(op.get("--forced-track") or op.get("--forced-display-flag"))
        for t in del_fichero:
            tipo = t["type"]
            if any(f in op for f in excluye.get(tipo, ())):
                continue
            elegidas = [v for f in seleccion.get(tipo, ()) for v in op.get(f, [])]
            if elegidas:
                ids = {int(x) for v in elegidas for x in str(v).split(",") if x.strip().isdigit()}
                if t["id"] not in ids:
                    continue
            nueva = {k: v for k, v in t.items() if k not in ("id", "multiplexed_tracks")}
            if t["id"] in nombres:
                nueva["track_name"] = nombres[t["id"]]
            if t["id"] in idiomas:
                nueva["language"] = idiomas[t["id"]]
            if t["id"] in por_defecto:
                nueva["default"] = por_defecto[t["id"]] in ("yes", "1", "true")
            if t["id"] in forzadas:
                nueva["forced"] = forzadas[t["id"]] in ("yes", "1", "true")
            nueva["_origen"] = [fi, t["id"]]
            if tipo == "video" and ruta_video is None:
                ruta_video = ruta
            pistas.append(nueva)
    # `--track-order F:ID,…` reordena; lo que no menciona va detrás, en orden.
    orden = [v for _, op in entradas for v in op.get("--track-order", [])]
    if orden:
        claves = []
        for par in ",".join(orden).split(","):
            f, _, tid = par.partition(":")
            if f.strip().isdigit() and tid.strip().isdigit():
                claves.append([int(f), int(tid)])
        rango = {tuple(c): i for i, c in enumerate(claves)}
        pistas.sort(key=lambda t: rango.get(tuple(t["_origen"]), len(rango)))
    for i, t in enumerate(pistas):
        t["id"] = i
        t.pop("_origen", None)
    return pistas, ruta_video


def _capitulos_del_j(ruta, name, del_fichero, sc):
    caps = del_fichero.get("chapters")
    if caps is None:
        caps = sc.get("chapters", {}).get(name, [])
    return [{"num_entries": len(caps)}] if caps else []


def mkvmerge(sc):
    # `-J` y `--identify --identification-format json` son EQUIVALENTES en
    # mkvmerge, y el pipeline usa las dos: Tab 2 y la Fase A van con `-J`, la
    # Fase E de Tab 1 con `--identify`. El fake solo entendía `-J`, así que una
    # llamada con `--identify` caía a la rama del mux, devolvía líneas de
    # progreso donde se esperaba JSON y el mapa de pistas salía VACÍO — un test
    # de la Fase E pasaba en verde sin seleccionar ni ordenar nada.
    if "-J" in ARGV or "--identify" in ARGV:
        positional = [a for a in ARGV if not a.startswith("-")]
        name = Path(positional[-1]).name if positional else ""
        spec = sc.get("mkvs", {}).get(name)
        if spec is None and positional:
            # Un MKV que escribió el mux falso lleva sus pistas dentro: es lo
            # que el `-J` del real leería del fichero. Lo que el escenario
            # declare explícitamente gana, porque es la intención del test.
            hechas = read_props(positional[-1], sc).get("tracks")
            if hechas is not None:
                spec = {"tracks": hechas, "title": "", "duration_s": 7200.0}
        if spec is None:
            # Sin declarar: un MKV mínimo válido (una pista de vídeo).
            spec = {"tracks": [{"type": "video", "codec": "HEVC/H.265/MPEG-H",
                                "dimensions": "3840x2160", "fps": 23.976}],
                    "title": "", "duration_s": 7200.0}
        del_fichero = read_props(positional[-1], sc) if positional else {}
        editadas = del_fichero.get("pistas_editadas") or {}
        out = []
        for i, t in enumerate(spec["tracks"]):
            t = dict(t, **editadas.get(str(i + 1), {}))
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
            # Las etiquetas de estadísticas que mkvmerge escribe en cada pista
            # al muxear, con su forma real (`"155506"`, `"01:48:05.896000000"`:
            # cadenas). Solo si la pista las declara: un MKV que no hizo
            # mkvmerge no las trae.
            if t.get("frames") is not None:
                props["tag_number_of_frames"] = str(int(t["frames"]))
            if t.get("track_duration_s"):
                seg = float(t["track_duration_s"])
                props["tag_duration"] = "%02d:%02d:%012.9f" % (
                    int(seg // 3600), int(seg % 3600 // 60), seg % 60)
            if t.get("channels"):
                props["audio_channels"] = t["channels"]
            if t.get("sample_rate"):
                props["audio_sampling_frequency"] = t["sample_rate"]
            if t.get("multiplexed_tracks"):
                props["multiplexed_tracks"] = t["multiplexed_tracks"]
            # `number`: el mkvmerge real lo emite siempre. En un MPEG-TS es el
            # PID —4113 la BL de todos los discos del NAS— y en un MKV, el
            # número de pista. Es lo que deja ver qué pista OCULTÓ mkvmerge:
            # la EL que empareja con su BL no sale en la lista.
            props["number"] = t.get("number", _number_por_defecto(name, spec["tracks"], i))
            out.append({"id": t.get("id", i), "type": t["type"],
                        "codec": t.get("codec", ""), "properties": props})
        # El `SegmentUID`. mkvmerge lo emite SIEMPRE, así que sin firma no
        # se devuelve vacío sino un valor que parece aleatorio —derivado del
        # nombre para que sea estable entre llamadas—. Con `""` el test de
        # «un MKV ajeno no da falso positivo» pasaría por el motivo
        # equivocado: por no haber UID, no por ser otro.
        uid = spec.get("segment_uid")
        if uid is None:
            uid = del_fichero.get("segment_uid")
        if not uid:
            uid = hashlib.md5(name.encode("utf-8")).hexdigest()
        # El título que alguien haya escrito con `mkvpropedit --set title=`
        # gana al del escenario: es lo que hace que un test pueda comprobar
        # qué sobrevive a una edición de metadatos.
        titulo = del_fichero.get("title")
        if titulo is None:
            titulo = spec["title"]
        propiedades = {
            "title": titulo,
            "duration": int(spec["duration_s"] * 1e9),
            "segment_uid": uid,
        }
        if spec.get("playlist_size") is not None:
            propiedades.update({
                "playlist": True,
                "playlist_duration": int(spec["duration_s"] * 1e9),
                "playlist_size": spec["playlist_size"],
            })
        # Un MPLS de verdad trae SIEMPRE su `playlist_file`. Sin él, quien lo
        # lee caía a la convención de nombre (`00801.mpls` → `00801.m2ts`) y
        # ningún test veía qué pasa cuando el primer fichero del playlist no
        # es el del episodio.
        lista = ficheros_de_playlist(positional[-1]) if positional else None
        if lista is not None:
            propiedades["playlist"] = True
            propiedades["playlist_file"] = lista
        sys.stdout.write(json.dumps({
            "container": {"supported": True, "properties": propiedades},
            "tracks": out,
            # Como el real: `[{"num_entries": N}]` por edición, `[]` sin
            # capítulos. Los escritos con mkvpropedit ganan al escenario.
            "chapters": _capitulos_del_j(positional[-1] if positional else "", name,
                                         del_fichero, sc),
        }))
        return 0
    out = opt("-o")
    if out and sc.get("mkvmerge_sin_salida"):
        out = None       # sale con 0 y no escribe nada
    if out:
        # El MKV se CONSTRUYE con lo que el argv pide, como el real: cada
        # opción de entrada se aplica al fichero que la sigue, el vídeo sale de
        # la primera entrada sin `--no-video` y su RPU es el del MKV. Antes
        # heredaba el RPU del primer `.hevc` que apareciera en el argv,
        # estuviera o no detrás de un `--no-video`, y el `-J` posterior leía
        # las pistas del escenario: un remux que tomara el vídeo del MKV origen
        # —o que dejara fuera su audio— salía idéntico a uno bueno.
        entradas = _entradas_del_mux()
        faltan = [r for r, _ in entradas if not Path(r).exists()]
        if faltan:
            sys.stderr.write("Error: The file '%s' could not be opened for "
                             "reading: open file error.\n" % faltan[0])
            return 2
        pistas, ruta_video = _pistas_del_mux(entradas, sc)
        props = read_props(ruta_video, sc) if ruta_video else dict(DEFAULT_PROPS)
        props["tracks"] = pistas
        if opt("--title") is not None:
            props["title"] = opt("--title")
        produce(out, props)
    # Progreso en el formato REAL de `--gui-mode`. Las fases D y E traducen
    # "#GUI#progress 45%" a "Progress: 45%" (contrato del parser del panel de
    # cola) y descartan el resto de líneas `#GUI#`; con una sola línea al 100 %
    # ninguna de las dos cosas se ejercitaba.
    sys.stdout.write("#GUI#begin_scanning_playlists\n")
    for pct in (0, 25, 50, 75, 100):
        sys.stdout.write("#GUI#progress %d%%\n" % pct)
    sys.stdout.write("#GUI#end_scanning_playlists\n")
    return 0


sys.exit(main())
'''
