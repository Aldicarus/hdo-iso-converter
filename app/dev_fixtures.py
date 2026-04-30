"""
dev_fixtures.py — Fixtures para desarrollo local (DEV_MODE=1)

Permite iterar la UI sin tener acceso a ISOs reales, dovi_tool, ffmpeg o
mkvmerge. Cuando DEV_MODE!=1 (default en producción), todo el módulo
queda inerte: las funciones siguen importables pero los endpoints del
backend no las invocan, así que cero impacto runtime.

Activación:
  - Variable de entorno DEV_MODE=1 en .env.local
  - Lanzar con ./run_local.sh (que carga .env.local antes de uvicorn)

Proporciona fixtures para los 3 tabs:
  - Tab 1 (ISO→MKV): DEV_FAKE_ISOS, build_fake_session, seed_dev_sessions
  - Tab 2 (Editar MKV): DEV_FAKE_MKV_FILES, build_fake_mkv_analysis,
    build_fake_mkv_apply
  - Tab 3 (CMv4.0): DEV_FAKE_RPU_FILES, build_fake_per_frame_data,
    build_fake_cmv40_session

Este módulo es feature estable, no temporal. Permite demos sin discos
físicos y testing rápido de UI changes. Si quieres una build de
producción 100% pelada, simplemente no setees DEV_MODE.
"""
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Activación ────────────────────────────────────────────────────────────────
DEV_MODE: bool = os.getenv("DEV_MODE", "0") == "1"

# ── ISOs fake ─────────────────────────────────────────────────────────────────
DEV_FAKE_ISOS: list[str] = [
    "Zootopia 2 (2025) UHD BluRay.iso",
    "Inside Out 3 (2025) UHD BluRay.iso",
    "Toy Story 5 (2025) UHD BluRay.iso",
    "The Incredibles 3 (2026) UHD BluRay.iso",
    "Moana 2 (2024) UHD BluRay.iso",
    "Frozen III (2025) UHD BluRay.iso",
    "Coco 2 (2027) UHD BluRay.iso",
    "Cars 4 (2026) UHD BluRay.iso",
]

# ── Texto BDInfo de referencia (ejemplo real del usuario) ─────────────────────
# Se usa _build_fake_bdinfo() para construir un BDInfoResult fake directamente.
# Solo se omite el análisis real (mkvmerge -J).
_BDINFO_TEMPLATE = """\
DISC INFO:

Disc Title:     {title}
Disc Size:      50,862,288,055 bytes
Protection:     AACS2
Extras:         Ultra HD, BD-Java
BDInfo:         0.7.5.5

PLAYLIST REPORT:

Name:                   00803.MPLS
Length:                 1:47:44.833 (h:m:s.ms)
Size:                   49,670,350,848 bytes
Total Bitrate:          61.47 Mbps

(*) Indicates included stream hidden by this playlist.

VIDEO:

MPEG-H HEVC Video       38519 kbps          2160p / 23.976 fps / 16:9 / Main 10 @ Level 5.1 @ High / 10 bits / HDR10 / BT.2020
* MPEG-H HEVC Video     4156 kbps           1080p / 23.976 fps / 16:9 / Main 10 @ Level 5.1 @ High / 10 bits / Dolby Vision / BT.2020

AUDIO:

Dolby TrueHD/Atmos Audio        English         5386 kbps       7.1 / 48 kHz /  4746 kbps / 24-bit (AC3 Embedded: 5.1 / 48 kHz /   640 kbps / DN -27dB)
Dolby Digital Audio             English          320 kbps       2.0 / 48 kHz /   320 kbps / DN -27dB
Dolby Digital Plus Audio        French          1024 kbps       7.1 / 48 kHz /  1024 kbps / DN -27dB (AC3 Embedded: 5.1-EX / 48 kHz /   576 kbps / DN -27dB)
Dolby TrueHD/Atmos Audio        Spanish         5511 kbps       7.1 / 48 kHz /  4871 kbps / 24-bit (AC3 Embedded: 5.1 / 48 kHz /   640 kbps / DN -31dB)
Dolby Digital Plus Audio        Japanese        1024 kbps       7.1 / 48 kHz /  1024 kbps / DN -27dB (AC3 Embedded: 5.1-EX / 48 kHz /   576 kbps / DN -27dB)
Dolby Digital Audio             Japanese         320 kbps       2.0 / 48 kHz /   320 kbps / DN -27dB

SUBTITLES:

Presentation Graphics           English         54.060 kbps
Presentation Graphics           French          43.235 kbps
Presentation Graphics           Spanish         36.783 kbps
Presentation Graphics           Japanese        30.057 kbps
Presentation Graphics           French           1.537 kbps
Presentation Graphics           Spanish          0.508 kbps
Presentation Graphics           Japanese         1.848 kbps
"""


def _build_fake_bdinfo():
    """Construye un BDInfoResult fake con datos extendidos de MediaInfo/dovi_tool."""
    from models import (
        BDInfoResult, DoviInfo, HdrMetadata, MediaInfoResult, MediaInfoTrack,
        RawAudioTrack, RawSubtitleTrack, VideoTrack,
    )
    hdr = HdrMetadata(
        hdr_format="HDR10", color_primaries="BT.2020",
        transfer_characteristics="PQ", bit_depth=10,
        max_cll=576, max_fall=242,
        mastering_display_luminance="min: 0.0001 cd/m2, max: 1000 cd/m2",
    )
    dovi = DoviInfo(
        profile=7, el_type="FEL", cm_version="v2.9",
        has_l1=True, has_l2=True, has_l5=True, has_l6=True,
        scene_count=7, frame_count=721,
        raw_summary="Summary:\n  Frames: 721\n  Profile: 7 (FEL)\n  DM version: 1 (CM v2.9)\n  Scene/shot count: 7",
    )
    return BDInfoResult(
        video_tracks=[
            VideoTrack(codec="MPEG-H HEVC Video", bitrate_kbps=38519,
                       description="2160p / 23.976 fps / HDR10", is_el=False,
                       hdr=hdr, dovi=dovi),
            VideoTrack(codec="MPEG-H HEVC Video", bitrate_kbps=4156,
                       description="1080p / 23.976 fps / Dolby Vision", is_el=True),
        ],
        audio_tracks=[
            RawAudioTrack(codec="Dolby TrueHD/Atmos Audio", language="English", bitrate_kbps=5386,
                          description="7.1 / 48 kHz / 4746 kbps / 24-bit",
                          format_commercial="Dolby TrueHD with Dolby Atmos",
                          channel_layout="L R C LFE Ls Rs Lb Rb", compression_mode="Lossless"),
            RawAudioTrack(codec="Dolby Digital Audio", language="English", bitrate_kbps=320,
                          description="2.0 / 48 kHz / 320 kbps",
                          format_commercial="Dolby Digital", compression_mode="Lossy"),
            RawAudioTrack(codec="Dolby Digital Plus Audio", language="French", bitrate_kbps=1024,
                          description="7.1 / 48 kHz / 1024 kbps",
                          format_commercial="Dolby Digital Plus", compression_mode="Lossy"),
            RawAudioTrack(codec="Dolby TrueHD/Atmos Audio", language="Spanish", bitrate_kbps=5511,
                          description="7.1 / 48 kHz / 4871 kbps / 24-bit",
                          format_commercial="Dolby TrueHD with Dolby Atmos",
                          channel_layout="L R C LFE Ls Rs Lb Rb", compression_mode="Lossless"),
            RawAudioTrack(codec="Dolby Digital Plus Audio", language="Japanese", bitrate_kbps=1024,
                          description="7.1 / 48 kHz / 1024 kbps",
                          format_commercial="Dolby Digital Plus", compression_mode="Lossy"),
            RawAudioTrack(codec="Dolby Digital Audio", language="Japanese", bitrate_kbps=320,
                          description="2.0 / 48 kHz / 320 kbps",
                          format_commercial="Dolby Digital", compression_mode="Lossy"),
        ],
        subtitle_tracks=[
            RawSubtitleTrack(language="English", bitrate_kbps=54.060, description="", resolution="1920x1080"),
            RawSubtitleTrack(language="French", bitrate_kbps=43.235, description="", resolution="1920x1080"),
            RawSubtitleTrack(language="Spanish", bitrate_kbps=36.783, description="", resolution="1920x1080"),
            RawSubtitleTrack(language="Japanese", bitrate_kbps=30.057, description="", resolution="1920x1080"),
            RawSubtitleTrack(language="French", bitrate_kbps=1.537, description=""),
            RawSubtitleTrack(language="Spanish", bitrate_kbps=0.508, description=""),
            RawSubtitleTrack(language="Japanese", bitrate_kbps=1.848, description=""),
        ],
        duration_seconds=6464.833,
        has_fel=True,
        fel_bitrate_kbps=4156,
        fel_reason="Dolby Vision Profile 7 (FEL) detectado via dovi_tool — CM v2.9",
        vo_language="English",
        main_mpls="00803.mpls",
        main_m2ts="00000.m2ts",
    )


def build_fake_session(iso_path: str):
    """
    Construye una Session completa sin BDInfoCLI ni QTS.

    Usa datos BDInfo fake hardcoded y ejecuta apply_rules() como en producción.
    """
    from phases.phase_b import apply_rules, generate_auto_chapters
    from models import Session
    from storage import make_session_id, save_session

    bdinfo_result = _build_fake_bdinfo()
    audio_dcp     = "audio dcp" in iso_path.lower()
    session_id = make_session_id(iso_path)
    session    = Session(id=session_id, iso_path=iso_path)

    session.bdinfo_result = bdinfo_result
    session.has_fel       = bdinfo_result.has_fel
    session.audio_dcp     = audio_dcp

    rules_result             = apply_rules(bdinfo_result, iso_path, audio_dcp)
    session.included_tracks  = rules_result["included_tracks"]
    session.discarded_tracks = rules_result["discarded_tracks"]
    session.mkv_name         = rules_result["mkv_name"]
    session.mkv_name_manual  = False

    duration = bdinfo_result.duration_seconds or 6464.0  # 1h47m44s
    session.chapters               = generate_auto_chapters(duration)
    session.chapters_auto_generated = True
    session.chapters_auto_reason   = (
        "[DEV] Capítulos generados automáticamente — BDInfoCLI no disponible en modo desarrollo"
    )

    session.status = "pending"
    save_session(session)
    return session


# ── Seed de proyectos fake ────────────────────────────────────────────────────

_SEED_MOVIES: list[tuple[str, str, str]] = [
    # (iso_name, status, days_ago)
    ("Zootopia 2 (2025) UHD BluRay.iso",        "done",    0),
    ("Inside Out 3 (2025) UHD BluRay.iso",       "pending", 1),
    ("Toy Story 5 (2025) UHD BluRay.iso",        "done",    3),
    ("The Incredibles 3 (2026) UHD BluRay.iso",  "error",   5),
    ("Moana 2 (2024) UHD BluRay.iso",            "pending", 7),
    ("Frozen III (2025) UHD BluRay.iso",         "done",   12),
    ("Coco 2 (2027) UHD BluRay.iso",             "pending",18),
    ("Cars 4 (2026) UHD BluRay.iso",             "done",   30),
]


# ── MKVs fake para Tab 2 ─────────────────────────────────────────────────────

DEV_FAKE_MKV_FILES: list[str] = [
    # ── Formato "humano" con paréntesis + tags [DV FEL]… (estilo HDO)
    "Zootrópolis 2 (2025) [DV FEL] [Audio DCP] [CMv4.0].mkv",
    "Oppenheimer (2023) [DV MEL] [TrueHD 7.1] [CMv2.9].mkv",
    "La jungla de cristal (1988) [DV FEL] [Atmos].mkv",             # Die Hard — requiere TMDb
    "El Rey León (2019) [DV P8] [Audio Dolby].mkv",                  # The Lion King 2019
    "Vaiana 2 (2024) [DV FEL] [CMv4.0 Restored].mkv",                # Moana 2 — TMDb
    "Del revés 2 (2024) [DV FEL] [Atmos].mkv",                       # Inside Out 2 — TMDb

    # ── Formato "scene" dotted con tags después del año
    "Solo.en.casa.1990.UHD.BluRay.2160p.HDR.DV.x265.mkv",            # Home Alone — TMDb
    "La.Jungla.de.Cristal.II.1990.REMUX.2160p.DV.FEL.mkv",           # Die Hard 2 — TMDb + romanos II
    "Buscando.a.Nemo.2003.UHD.BluRay.2160p.DV.P7.mkv",               # Finding Nemo — TMDb
    "Los.Increíbles.2.2018.BD.FEL.CMv2.9.mkv",                       # Incredibles 2

    # ── Caso conocido NO factible (BD-FEL exclusivo según REC_9999)
    "Gladiator (2000) [BD FEL] [HDR10 MaxCLL].mkv",

    # ── Caso conocido sin entrada en la hoja (prueba del fallback "unknown")
    "Película Ficticia (2050) [DV FEL].mkv",

    # ── Formatos originales (mantenidos para compat con otras pruebas)
    "Zootopia 2 (2025) UHD BluRay.mkv",
    "Toy Story 5 (2025) UHD BluRay.mkv",
    "Moana 2 (2024) UHD BluRay.mkv",
]

def build_fake_mkv_analysis(file_name: str) -> dict:
    """Construye un MkvAnalysisResult fake para Tab 2 con datos extendidos.

    Por defecto simula un Blu-ray UHD P7 FEL con CMv2.9 (caso típico). Para
    probar la UI con otras casuísticas, usa sufijos en el filename:
    - "*CMv4.0 retail*"    → CMv4.0 con perfil retail/transferido (azul claro)
    - "*CMv4.0 native*"    → CMv4.0 con perfil de colorista (azul fuerte)
    - "*CMv4.0 generated*" → CMv4.0 sintético algorítmico (naranja)
    """
    from models import MkvAnalysisResult, MkvTrackInfo, Chapter, HdrMetadata, DoviInfo

    fn_lower = file_name.lower()

    # ── Construcción del DoviInfo según el filename ───────────────────
    if "cmv4.0 native" in fn_lower or "cmv4.0 colorist" in fn_lower:
        # CMv4.0 nativo: todos los niveles de autoría + múltiples L8 trims
        dovi = DoviInfo(
            profile=7, el_type="FEL", cm_version="v4.0",
            has_l1=True, has_l2=True, has_l3=True, has_l5=True, has_l6=True,
            has_l8=True, has_l9=True, has_l10=True, has_l11=True,
            l8_trim_count=4,
            l1_max_cll=1000.6, l1_max_fall=92.36,
            l6_max_cll=998, l6_max_fall=185,
            scene_count=890, frame_count=173456,
            raw_summary="[fake] CMv4.0 nativo con firma de colorista",
        )
    elif "cmv4.0 generated" in fn_lower or "cmv4.0 synthetic" in fn_lower:
        # Generated: solo L8 con 1 trim, sin L3/L9/L10/L11
        dovi = DoviInfo(
            profile=7, el_type="FEL", cm_version="v4.0",
            has_l1=True, has_l2=True, has_l5=True, has_l6=True,
            has_l8=True, l8_trim_count=1,
            l1_max_cll=720.0, l1_max_fall=80.0,
            l6_max_cll=1000, l6_max_fall=185,
            scene_count=890, frame_count=173456,
            raw_summary="[fake] CMv4.0 generated algorítmicamente",
        )
    elif "cmv4.0 retail" in fn_lower or "cmv4.0" in fn_lower:
        # Retail/transferred: 2-3 niveles de autoría + L8 con varios trims
        dovi = DoviInfo(
            profile=7, el_type="FEL", cm_version="v4.0",
            has_l1=True, has_l2=True, has_l5=True, has_l6=True,
            has_l8=True, has_l9=True, has_l10=True,
            l8_trim_count=3,
            l1_max_cll=1000.6, l1_max_fall=92.36,
            l6_max_cll=998, l6_max_fall=185,
            scene_count=890, frame_count=173456,
            raw_summary="[fake] CMv4.0 retail transferido desde WEB-DL",
        )
    else:
        # Por defecto: CMv2.9 P7 FEL (caso típico de Blu-ray UHD sin upgrade)
        dovi = DoviInfo(
            profile=7, el_type="FEL", cm_version="v2.9",
            has_l1=True, has_l2=True, has_l5=True, has_l6=True,
            l1_max_cll=720.0, l1_max_fall=80.0,
            l6_max_cll=1000, l6_max_fall=185,
            scene_count=890, frame_count=173456,
            raw_summary="[fake] CMv2.9 P7 FEL original del Blu-ray",
        )

    result = MkvAnalysisResult(
        file_path=f"/mnt/output/{file_name}",
        file_name=file_name,
        file_size_bytes=48_500_000_000,
        duration_seconds=6464.833,
        title=file_name.replace(".mkv", ""),
        has_fel=True,
        hdr=HdrMetadata(
            hdr_format="HDR10", color_primaries="BT.2020",
            transfer_characteristics="PQ", bit_depth=10,
            max_cll=576, max_fall=242,
            mastering_display_luminance="min: 0.0001 cd/m2, max: 1000 cd/m2",
        ),
        dovi=dovi,
        tracks=[
            MkvTrackInfo(id=0, type="video", codec="HEVC/H.265/MPEG-H",
                         language="und", pixel_dimensions="3840x2160",
                         bitrate_kbps=38519, bit_depth=10, color_primaries="BT.2020",
                         hdr_format="HDR10"),
            MkvTrackInfo(id=1, type="video", codec="HEVC/H.265/MPEG-H",
                         language="und", pixel_dimensions="1920x1080",
                         bitrate_kbps=4156),
            MkvTrackInfo(id=2, type="audio", codec="TrueHD Atmos",
                         language="spa", name="Castellano TrueHD Atmos 7.1 (DCP 9.1.6)",
                         flag_default=True, channels=8, sample_rate=48000,
                         bitrate_kbps=5386, format_commercial="Dolby TrueHD with Dolby Atmos",
                         channel_layout="L R C LFE Ls Rs Lb Rb", compression_mode="Lossless"),
            MkvTrackInfo(id=3, type="audio", codec="TrueHD Atmos",
                         language="eng", name="Inglés TrueHD Atmos 7.1",
                         flag_default=False, channels=8, sample_rate=48000,
                         bitrate_kbps=5386, format_commercial="Dolby TrueHD with Dolby Atmos",
                         channel_layout="L R C LFE Ls Rs Lb Rb", compression_mode="Lossless"),
            # Subs con packet_count realistas: forzados <500, completos ≥500
            MkvTrackInfo(id=4, type="subtitles", codec="HDMV PGS",
                         language="spa", name="Castellano Forzados (PGS)",
                         flag_default=True, flag_forced=True,
                         pixel_dimensions="1920x1080", packet_count=142, bitrate_kbps=2),
            MkvTrackInfo(id=5, type="subtitles", codec="HDMV PGS",
                         language="eng", name="Inglés Completos (PGS)",
                         flag_default=False, flag_forced=False,
                         pixel_dimensions="1920x1080", packet_count=2980, bitrate_kbps=41),
            MkvTrackInfo(id=6, type="subtitles", codec="HDMV PGS",
                         language="spa", name="Castellano Completos (PGS)",
                         flag_default=False, flag_forced=False,
                         pixel_dimensions="1920x1080", packet_count=2643, bitrate_kbps=38),
            # Este viene sin flag forzado puesto → inferido por packet_count < 500
            MkvTrackInfo(id=7, type="subtitles", codec="HDMV PGS",
                         language="eng", name="",
                         flag_default=False, flag_forced=False,
                         pixel_dimensions="1920x1080", packet_count=89, bitrate_kbps=1),
        ],
        chapters=[
            Chapter(number=1, timestamp="00:00:00.000", name="Capítulo 01", name_custom=False),
            Chapter(number=2, timestamp="00:04:32.147", name="Capítulo 02", name_custom=False),
            Chapter(number=3, timestamp="00:11:15.800", name="Capítulo 03", name_custom=False),
            Chapter(number=4, timestamp="00:22:03.444", name="Capítulo 04", name_custom=False),
            Chapter(number=5, timestamp="00:33:18.920", name="Capítulo 05", name_custom=False),
            Chapter(number=6, timestamp="00:45:07.610", name="Capítulo 06", name_custom=False),
            Chapter(number=7, timestamp="00:56:41.250", name="Capítulo 07", name_custom=False),
            Chapter(number=8, timestamp="01:08:22.780", name="Capítulo 08", name_custom=False),
            Chapter(number=9, timestamp="01:19:55.330", name="Capítulo 09", name_custom=False),
            Chapter(number=10, timestamp="01:31:40.100", name="Capítulo 10", name_custom=False),
            Chapter(number=11, timestamp="01:42:08.500", name="Capítulo 11", name_custom=False),
        ],
    )
    return result.model_dump()


def build_fake_mkv_apply(body) -> dict:
    """Simula la respuesta de POST /api/mkv/apply sin ejecutar mkvpropedit."""
    lines = []
    lines.append(f"[DEV] Simulando mkvpropedit sobre: {body.file_path}")
    for t in body.audio_tracks:
        lines.append(f"  Track {t.id}: name='{t.name}' default={t.flag_default}")
    for t in body.subtitle_tracks:
        lines.append(f"  Track {t.id}: name='{t.name}' default={t.flag_default} forced={t.flag_forced}")
    if body.chapters is not None:
        lines.append(f"  Chapters: {len(body.chapters)} capítulos")
    lines.append("Done. El fichero ha sido modificado.")
    return {
        "ok": True,
        "new_path": body.file_path,
        "output": "\n".join(lines),
    }


# ── Fixtures fake CMv4.0 (Tab 3) ─────────────────────────────────────────────

DEV_FAKE_RPU_FILES: list[dict] = [
    {"name": "Zootopia 2 (2025) CMv4.0.bin", "path": "/mnt/cmv40_rpus/Zootopia 2 (2025) CMv4.0.bin", "size_bytes": 4_521_300},
    {"name": "Inside Out 3 (2025) CMv4.0.bin", "path": "/mnt/cmv40_rpus/Inside Out 3 (2025) CMv4.0.bin", "size_bytes": 3_812_400},
    {"name": "Moana 2 (2024) CMv4.0.bin", "path": "/mnt/cmv40_rpus/Moana 2 (2024) CMv4.0.bin", "size_bytes": 4_102_800},
]


def build_fake_per_frame_data(source_frames: int = 137_952, offset: int = 40) -> dict:
    """
    Genera per_frame_data.json fake con un offset entre source y target.

    Para mantener el payload razonable en DEV se muestrea cada STEP frames
    pero se preservan los números de frame reales en el campo 'frame'.
    """
    import math
    STEP = 20  # 1 datapoint cada 20 frames ≈ 6898 puntos para 137k frames

    # Genera la serie source (muestreada)
    src_series: list[dict] = []
    for i in range(0, source_frames, STEP):
        if i < 120:
            src_maxcll = 850 + (i % 30) * 5
        elif math.sin(i / 500) > 0.6:
            src_maxcll = 400 + (i % 100) * 2
        else:
            src_maxcll = 80 + (i % 60)
        src_series.append({
            "frame": i,
            "src_maxcll": src_maxcll,
            "src_maxfall": src_maxcll * 0.15,
        })

    # Genera el target: desplazado `offset` frames (positivo = target adelantado)
    target_frames = source_frames + offset
    data: list[dict] = []
    for point in src_series:
        i = point["frame"]
        entry = {
            "frame": i,
            "src_maxcll": point["src_maxcll"],
            "src_maxfall": point["src_maxfall"],
            "tgt_maxcll": 0,
            "tgt_maxfall": 0,
        }
        # Target en la posición i tiene el valor del source en i-offset
        tgt_src_frame = i - offset
        if 0 <= tgt_src_frame < source_frames:
            # Buscar el datapoint source más cercano por frame
            closest = min(src_series, key=lambda p: abs(p["frame"] - tgt_src_frame))
            entry["tgt_maxcll"]  = closest["src_maxcll"]
            entry["tgt_maxfall"] = closest["src_maxfall"]
        data.append(entry)

    return {
        "source_frames": source_frames,
        "target_frames": target_frames,
        "sample_step": STEP,
        "data": data,
        "suggested_offset": {
            "offset": offset,
            "confidence": 0.95,
            "reason": f"Offset={offset} frames (confianza=95%, RMS error=2.1)",
        },
    }


def build_fake_cmv40_session(mkv_name: str) -> dict:
    """Construye un CMv40Session fake en fase 'created' para Tab 3."""
    from models import CMv40Session, CMv40Phase
    from storage import make_cmv40_session_id
    sid = make_cmv40_session_id(f"/mnt/output/{mkv_name}")
    s = CMv40Session(
        id=sid,
        source_mkv_path=f"/mnt/output/{mkv_name}",
        source_mkv_name=mkv_name,
        output_mkv_name=mkv_name.replace(".mkv", " [CMv4.0].mkv"),
        artifacts_dir=f"/mnt/tmp/cmv40/{sid}",
        phase=CMv40Phase.CREATED,
    )
    return s.model_dump()


# ── Seed de proyectos fake ────────────────────────────────────────────────────

def seed_dev_sessions(config_dir: Path) -> None:
    """
    Genera sesiones fake en config_dir si no existen ya.
    Solo se llama al arrancar en DEV_MODE y la lista está vacía o incompleta.
    Elimina duplicados por iso_path antes de crear nuevas sesiones.
    """
    from phases.phase_b import apply_rules, generate_auto_chapters
    from models import Session
    from storage import make_session_id, save_session, list_sessions, delete_session

    # Eliminar duplicados: para cada iso_path, conservar solo la sesión más reciente
    all_sessions = list_sessions()
    seen_iso: dict[str, str] = {}  # iso_path → session_id más reciente
    def _aware(dt):
        """Convierte datetime naive a UTC-aware para poder comparar con aware."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    for s in sorted(all_sessions, key=lambda x: _aware(x.updated_at)):
        if s.iso_path in seen_iso:
            delete_session(seen_iso[s.iso_path])  # borrar la más antigua
        seen_iso[s.iso_path] = s.id

    existing_iso_paths = set(seen_iso.keys())

    for iso_name, status, days_ago in _SEED_MOVIES:
        iso_path   = f"/mnt/isos/{iso_name}"
        if iso_path in existing_iso_paths:
            continue  # ya existe una sesión para este ISO, no duplicar

        session_id = make_session_id(iso_path)

        bdinfo_result = _build_fake_bdinfo()

        audio_dcp  = False
        session    = Session(id=session_id, iso_path=iso_path)

        # Retroceder timestamps para simular historial realista
        ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
        session.created_at = ts
        session.updated_at = ts

        session.bdinfo_result = bdinfo_result
        session.has_fel       = bdinfo_result.has_fel
        session.audio_dcp     = audio_dcp

        rules_result             = apply_rules(bdinfo_result, iso_path, audio_dcp)
        session.included_tracks  = rules_result["included_tracks"]
        session.discarded_tracks = rules_result["discarded_tracks"]
        session.mkv_name         = rules_result["mkv_name"]

        duration = bdinfo_result.duration_seconds or 6464.0
        session.chapters               = generate_auto_chapters(duration)
        session.chapters_auto_generated = True
        session.chapters_auto_reason   = "[DEV] Capítulos automáticos"

        session.status = status
        if status == "error":
            session.error_message = "[DEV] Error simulado para pruebas"

        # Guardar sin actualizar updated_at (lo ponemos manualmente arriba)
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / f"{session_id}.json"
        path.write_text(session.model_dump_json(indent=2), encoding="utf-8")

    print(f"[DEV] Sesiones de prueba listas en {config_dir}")
