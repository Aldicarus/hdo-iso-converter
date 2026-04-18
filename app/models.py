"""
models.py — Modelos de datos de ISO2MKVFEL (Pydantic v2)

Jerarquía de modelos:

  BDInfoResult                     ← resultado del parseo de BDInfoCLI (Fase A)
    ├── VideoTrack[]                ← pistas de vídeo tal como las reporta BDInfo
    ├── RawAudioTrack[]             ← pistas de audio en bruto (antes de aplicar reglas)
    └── RawSubtitleTrack[]          ← pistas de subtítulos en bruto

  Session                          ← unidad de trabajo persistente
    ├── BDInfoResult                ← resultado de Fase A
    ├── IncludedAudioTrack[]        ← pistas de audio seleccionadas (Fase B / C)
    ├── IncludedSubtitleTrack[]     ← pistas de subtítulos seleccionadas (Fase B / C)
    ├── DiscardedTrack[]            ← pistas descartadas con razón (Fase B / C)
    └── Chapter[]                  ← capítulos editables (Fase B / C)

  AnalyzeRequest                   ← payload POST /api/analyze
  SessionUpdateRequest             ← payload PUT /api/sessions/{id}
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════════════
#  RESULTADO DE BDINFO (Fase A)
# ══════════════════════════════════════════════════════════════════════

class HdrMetadata(BaseModel):
    """Metadata HDR10 / Dolby Vision extraída por MediaInfo."""

    hdr_format: str = ""
    """Formato HDR (ej: 'HDR10', 'Dolby Vision', 'HDR10+', 'HLG')."""

    color_primaries: str = ""
    """Primarios de color (ej: 'BT.2020')."""

    transfer_characteristics: str = ""
    """Curva de transferencia (ej: 'PQ', 'HLG')."""

    bit_depth: int = 0
    """Profundidad de bits (ej: 10, 12)."""

    max_cll: int | None = None
    """Maximum Content Light Level (cd/m²)."""

    max_fall: int | None = None
    """Maximum Frame Average Light Level (cd/m²)."""

    mastering_display_luminance: str = ""
    """Luminancia del display de masterizado (ej: 'min: 0.0001 cd/m2, max: 1000 cd/m2')."""


class DoviInfo(BaseModel):
    """Análisis RPU de Dolby Vision via dovi_tool."""

    profile: int = 0
    """Perfil DV (4, 5, 7, 8)."""

    el_type: str = ""
    """Tipo de Enhancement Layer: 'FEL' o 'MEL'."""

    cm_version: str = ""
    """Content Mapping version (ej: 'v2.9', 'v4.0')."""

    has_l1: bool = False
    """L1: MaxCLL/MaxFALL presente."""

    has_l2: bool = False
    """L2: Trim metadata presente."""

    has_l5: bool = False
    """L5: Active area / letterbox offsets."""

    has_l6: bool = False
    """L6: MaxCLL/MaxFALL fallback."""

    scene_count: int = 0
    frame_count: int = 0

    raw_summary: str = ""
    """Output completo de dovi_tool info --summary."""


class MediaInfoTrack(BaseModel):
    """Datos por pista extraídos de MediaInfo --Output=JSON."""

    track_type: str
    """Tipo: 'video', 'audio', 'text'."""

    stream_order: int = -1
    """Orden del stream en el m2ts."""

    bitrate_kbps: int = 0
    """Bitrate real en kbps."""

    format_commercial: str = ""
    """Nombre comercial (ej: 'Dolby TrueHD with Dolby Atmos')."""

    channels: int = 0
    channel_layout: str = ""
    """Layout de canales (ej: 'L R C LFE Ls Rs Lb Rb')."""

    compression_mode: str = ""
    """'Lossless' o 'Lossy'."""

    bit_depth: int = 0
    color_primaries: str = ""
    transfer_characteristics: str = ""

    resolution: str = ""
    """Resolución de subtítulos (ej: '1920x1080')."""


class MediaInfoResult(BaseModel):
    """Resultado completo del análisis MediaInfo sobre un m2ts o MKV."""

    source_path: str = ""
    """Ruta al fichero analizado."""

    source_size_bytes: int = 0
    tracks: list[MediaInfoTrack] = []
    raw_json: dict | None = None
    """JSON completo de MediaInfo para diagnóstico."""


class VideoTrack(BaseModel):
    """
    Pista de vídeo tal como la reporta el análisis (mkvmerge + MediaInfo).

    La pista principal (BL, Base Layer) tiene ``is_el=False``.
    La Enhancement Layer de Dolby Vision tiene ``is_el=True``.
    """

    codec: str
    """Nombre del codec (ej: 'MPEG-H HEVC Video')."""

    bitrate_kbps: int
    """Bitrate en kbps. 0 si no disponible (mkvmerge no lo reporta)."""

    description: str
    """Descripción (ej: '2160p / 23.976 fps / HDR10 / BT.2020')."""

    is_el: bool = False
    """True si es la Enhancement Layer de Dolby Vision."""

    hdr: HdrMetadata | None = None
    """Metadata HDR10 del BL (de MediaInfo). None si no disponible."""

    dovi: DoviInfo | None = None
    """Análisis Dolby Vision RPU (de dovi_tool). None si no disponible."""


class RawAudioTrack(BaseModel):
    """
    Pista de audio en bruto, tal como la reporta BDInfoCLI.

    Estos datos se usan en Fase B para aplicar las reglas de selección y
    construir los literales de pista. No se modifican directamente.

    Ref: spec §4.1, §5.1
    """

    codec: str
    """Campo Codec de BDInfo. El string exacto varía entre versiones;
    siempre se parsea por subcadenas (ej: 'Dolby TrueHD/Atmos Audio',
    'DTS-HD Master Audio', 'Dolby Digital Plus Audio')."""

    language: str
    """Campo Language de BDInfo en inglés (ej: 'Spanish', 'English', 'French').
    La VO se identifica como el idioma de la primera pista del disco."""

    bitrate_kbps: int
    """Bitrate total de la pista en kbps."""

    description: str
    """Campo Description (ej: '7.1+11 objects / 48 kHz / 4304 kbps / 24-bit').
    Se usa para extraer canales y detectar Atmos en pistas DD+."""

    format_commercial: str = ""
    """Nombre comercial de MediaInfo (ej: 'Dolby TrueHD with Dolby Atmos').
    Detección definitiva de Atmos/DTS:X. Vacío si MediaInfo no disponible."""

    channel_layout: str = ""
    """Layout de canales (ej: 'L R C LFE Ls Rs Lb Rb')."""

    compression_mode: str = ""
    """'Lossless' o 'Lossy'."""


class RawSubtitleTrack(BaseModel):
    """
    Pista de subtítulos PGS en bruto, tal como la reporta BDInfoCLI.

    El bitrate se usa para clasificar entre forzados (Forma A, < 3 kbps)
    y completos (≥ 3 kbps). Ref: spec §5.2.3
    """

    language: str
    """Campo Language de BDInfo. El código 'qad' indica Audio Description
    y la pista se descarta automáticamente."""

    bitrate_kbps: float
    """Bitrate en kbps con decimales (ej: 1.115, 35.498).
    Umbral de forzados Forma A: < 3 kbps."""

    description: str
    """Campo Description (habitualmente vacío para PGS)."""

    resolution: str = ""
    """Resolución del subtítulo PGS (ej: '1920x1080'). De MediaInfo."""

    packet_count: int = 0
    """Número de paquetes PES de la pista PGS (medido por ffprobe -count_packets).

    Es el proxy más fiable del volumen de subtítulo real (eventos de pantalla):
    - Forzado típico: <500 paquetes
    - Completo típico: ~7.000-11.000 paquetes
    - Audiodescripción: paquetes > 1.3× mediana del idioma

    0 si ffprobe no pudo medirlo (fallback a heurística de patrones)."""


class BDInfoResult(BaseModel):
    """
    Resultado completo del parseo del report de BDInfoCLI.

    Generado en Fase A y almacenado en la sesión. Es inmutable una vez
    creado (las ediciones del usuario se aplican sobre los modelos
    de Fase B, no sobre este resultado).

    Ref: spec §4
    """

    video_tracks: list[VideoTrack]
    """Todas las pistas de vídeo del disco, incluyendo la EL si existe."""

    audio_tracks: list[RawAudioTrack]
    """Todas las pistas de audio en el orden original del disco.
    La primera pista determina la VO (Original Version)."""

    subtitle_tracks: list[RawSubtitleTrack]
    """Todas las pistas de subtítulos PGS del disco."""

    duration_seconds: float
    """Duración total de la película en segundos, extraída del campo
    'Length' del playlist en el report de BDInfo."""

    has_fel: bool
    """True si se detecta FEL (Full Enhancement Layer) de Dolby Vision.
    Criterio primario: bitrate EL > 1000 kbps."""

    fel_bitrate_kbps: int | None = None
    """Bitrate de la EL en kbps, o None si no hay capa de mejora."""

    fel_reason: str = ""
    """Mensaje legible con la lógica de detección FEL/MEL para mostrar en la UI.
    Ej: 'FEL detectado: bitrate EL = 3957 kbps > umbral 1000 kbps'"""

    vo_language: str
    """Idioma de la primera pista de audio del disco en formato BDInfo (ej: 'English', 'Spanish').
    Dato raw de referencia — la VO real se determina en Fase B con _detect_vo_language:
    primero English, fallback Spanish, emergencia con advertencia."""

    main_mpls: str = ""
    """Nombre del fichero MPLS principal seleccionado en Fase A (ej: '00800.mpls').
    Se reutiliza en Fase D para garantizar que ambas fases procesan el mismo playlist."""

    mkvmerge_raw: dict | None = None
    """JSON completo de mkvmerge -J tal como lo devolvió la herramienta.
    Se preserva para diagnóstico — permite ver las pistas originales sin
    heurísticas aplicadas (bitrate, idiomas, codecs reales)."""

    mediainfo_result: MediaInfoResult | None = None
    """Resultado de MediaInfo sobre el m2ts principal. None si no disponible."""

    main_m2ts: str = ""
    """Nombre del fichero m2ts principal (el más grande en BDMV/STREAM/)."""


# ══════════════════════════════════════════════════════════════════════
#  PISTAS PROCESADAS (Fase B → resultado editable en Fase C)
# ══════════════════════════════════════════════════════════════════════

class TrackType(str):
    """Constantes de tipo de pista (usadas como discriminador en la UI)."""
    AUDIO    = "audio"
    SUBTITLE = "subtitle"


class IncludedAudioTrack(BaseModel):
    """
    Pista de audio seleccionada para el MKV final.

    Generada por Fase B a partir de RawAudioTrack. El usuario puede
    modificar ``label``, ``flag_default``, ``flag_forced`` y ``position``
    en Fase C (pantalla de revisión).

    Ref: spec §5.1, §5.1.5, §5.1.7
    """

    track_type: Literal["audio"] = "audio"
    """Discriminador de tipo fijo. Permite distinguir audio de subtítulos
    en listas polimórficas."""

    position: int
    """Posición 0-indexed en el MKV final (después del vídeo implícito).
    Se recalcula automáticamente al reordenar con drag & drop."""

    raw: RawAudioTrack
    """Datos originales de BDInfo. Se preservan para mostrar en tooltips
    y para el matching de pistas en Fase E."""

    language_literal: str
    """Nombre del idioma en español (ej: 'Castellano', 'Inglés', 'Francés').
    Ref: spec §5.1.1"""

    codec_literal: str
    """Descripción del codec con canales (ej: 'TrueHD Atmos 7.1', 'DTS-HD MA 5.1').
    Ref: spec §5.1.2, §5.1.3"""

    label: str
    """Literal completo que se escribe como nombre de pista en el MKV.
    Ej: 'Castellano TrueHD Atmos 7.1', 'Inglés DTS-HD MA 5.1 (DCP 9.1.6)'.
    El usuario puede editarlo libremente en Fase C."""

    flag_default: bool
    """Marca MKV 'default': indica al reproductor qué pista activar por defecto.
    Solo una pista de audio debe tener default=True (la castellana)."""

    flag_forced: bool = False
    """Marca MKV 'forced'. Habitualmente False para audio."""

    selection_reason: str
    """Explicación legible de por qué se seleccionó esta pista.
    Ej: 'Seleccionada: mejor calidad para Spanish. TrueHD Atmos > DD+ > DTS-HD MA > DTS > DD'"""


class IncludedSubtitleTrack(BaseModel):
    """
    Pista de subtítulos PGS seleccionada para el MKV final.

    Generada por Fase B a partir de RawSubtitleTrack. El usuario puede
    modificar ``label``, ``flag_default``, ``flag_forced`` y ``position``
    en Fase C.

    Ref: spec §5.2, §5.2.4, §5.2.5
    """

    track_type: Literal["subtitle"] = "subtitle"
    """Discriminador de tipo fijo."""

    position: int
    """Posición 0-indexed en el MKV final (a continuación de las pistas de audio)."""

    raw: RawSubtitleTrack
    """Datos originales de BDInfo."""

    language_literal: str
    """Nombre del idioma en español (ej: 'Castellano', 'Inglés')."""

    subtitle_type: Literal["forced", "complete"]
    """Clasificación de la pista:
    - 'forced': subtítulos forzados (Forma A, bitrate < 3 kbps con pista completa presente).
    - 'complete': subtítulos completos (única pista del idioma o bitrate ≥ 3 kbps)."""

    label: str
    """Literal que se escribe en el MKV. Ej: 'Castellano Forzados PGS', 'Inglés Completos PGS'."""

    flag_default: bool
    """True solo para los forzados castellanos (primera pista de subtítulos)."""

    flag_forced: bool
    """True para pistas clasificadas como 'forced' (Forma A)."""

    selection_reason: str
    """Explicación legible de la clasificación aplicada."""


# Alias de unión para usar en listas polimórficas
IncludedTrack = IncludedAudioTrack | IncludedSubtitleTrack


class DiscardedTrack(BaseModel):
    """
    Pista descartada por las reglas automáticas de Fase B.

    Se muestra en la sección 'Pistas descartadas' de la pantalla de revisión.
    El usuario puede recuperarla con el botón 'Recuperar', que la convierte
    en un IncludedAudioTrack / IncludedSubtitleTrack básico.
    """

    track_type: Literal["audio", "subtitle"]
    """Tipo de pista descartada."""

    raw: RawAudioTrack | RawSubtitleTrack
    """Datos originales de BDInfo para mostrar al usuario."""

    discard_reason: str
    """Explicación legible de por qué se descartó.
    Ej: 'Descartada: idioma French no es Castellano ni VO (English)'
        'Descartada: código de idioma qad (Audio Description)'"""


# ══════════════════════════════════════════════════════════════════════
#  CAPÍTULOS
# ══════════════════════════════════════════════════════════════════════

class Chapter(BaseModel):
    """
    Un capítulo del MKV.

    Los capítulos provienen del MPLS del disco (extraídos por mkvextract en Fase D).
    Si el disco no tiene capítulos, se generan automáticamente cada 10 minutos
    empezando en el primer intervalo (minuto 10).
    El usuario puede añadir, mover, renombrar y eliminar capítulos en Fase C.

    Ref: spec §5.3, §6.4
    """

    number: int
    """Número de capítulo, siempre consecutivo y ordenado por timestamp.
    Se recalcula automáticamente en cualquier operación de edición."""

    timestamp: str
    """Tiempo de inicio en formato 'HH:MM:SS.mmm' (ej: '01:23:45.678')."""

    name: str
    """Nombre del capítulo. Si el disco tiene nombres se usan los originales;
    si no, se genera 'Capítulo 01', 'Capítulo 02', etc."""

    name_custom: bool = False
    """True si el usuario ha editado el nombre manualmente.
    Cuando es False, el nombre se recalcula automáticamente al reordenar capítulos.
    Si el usuario borra el nombre, vuelve a False y se auto-genera."""


# ══════════════════════════════════════════════════════════════════════
#  REGISTRO DE EJECUCIÓN
# ══════════════════════════════════════════════════════════════════════

class ExecutionRecord(BaseModel):
    """
    Registro inmutable de una ejecución del pipeline D+E.

    Se genera al finalizar cada ejecución (éxito o error) y se añade a
    ``Session.execution_history``. Permite al usuario revisar el historial
    de ejecuciones de un proyecto sin perder datos de ejecuciones anteriores.
    """

    run_number: int
    """Número secuencial de ejecución dentro de la sesión (1-based)."""

    started_at: datetime
    """Timestamp de inicio de la ejecución (UTC)."""

    finished_at: datetime | None = None
    """Timestamp de finalización (UTC). None si fue interrumpida."""

    status: str = "error"
    """Resultado: 'done' o 'error'."""

    error_message: str | None = None
    """Mensaje de error si status='error'."""

    output_mkv_path: str | None = None
    """Ruta del MKV final generado (solo si status='done')."""

    phase_elapsed: dict[str, float | None] = {}
    """Tiempo en segundos de cada fase: {"mount": 2.3, "extract": 345.6,
    "unmount": 1.2, "write": 12.4}. None si la fase no se ejecutó."""

    output_log: list[str] = []
    """Copia completa del log de esta ejecución."""


# ══════════════════════════════════════════════════════════════════════
#  SESIÓN (unidad de trabajo persistente)
# ══════════════════════════════════════════════════════════════════════

class SessionStatus(str):
    """Estados posibles de una sesión a lo largo de su ciclo de vida."""
    PENDING = "pending"   # Fases A y B completadas, esperando confirmación del usuario
    RUNNING = "running"   # Fases D y E en ejecución
    DONE    = "done"      # MKV final generado con éxito
    ERROR   = "error"     # Error durante la ejecución


class Session(BaseModel):
    """
    Unidad de trabajo completa que representa la conversión de un ISO a MKV.

    Se persiste como JSON en ``/config/{session_id}.json`` y puede recuperarse
    en cualquier momento para revisión, edición o relanzamiento.

    Ciclo de vida:
      1. Se crea al llamar a POST /api/analyze (status='pending')
      2. El usuario edita pistas/capítulos en Fase C (status='pending')
      3. Se ejecuta con POST /api/sessions/{id}/execute (status='running')
      4. Finaliza con status='done' o 'error'

    Ref: spec §9
    """

    id: str
    """Identificador único. Formato: '{titulo}_{año}_{timestamp_unix}'.
    Ej: 'El_Rey_de_Reyes_2025_1714000000'"""

    iso_path: str
    """Ruta absoluta al ISO de origen (dentro de /mnt/isos)."""

    iso_fingerprint: str = ""
    """Huella del ISO: SHA-256 del primer 1 MB + tamaño del fichero.
    Permite detectar el mismo disco independientemente de la ruta o nombre."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    """Timestamp de creación de la sesión (UTC)."""

    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    """Timestamp de la última modificación (UTC). Se actualiza en cada save_session."""

    status: str = "pending"
    """Estado actual. Ver SessionStatus para los valores posibles."""

    # ── Resultado de Fase A ───────────────────────────────────────
    bdinfo_result: BDInfoResult | None = None
    """Resultado del parseo de BDInfoCLI. None hasta que Fase A completa."""

    # ── Resultado de Fase B + ediciones de Fase C ─────────────────
    has_fel: bool = False
    """True si se detectó FEL. Afecta al nombre del MKV y puede editarse
    manualmente con el toggle de la pantalla de revisión."""

    audio_dcp: bool = False
    """True si el nombre del ISO contiene 'Audio DCP'. Afecta al sufijo
    de las pistas TrueHD Atmos y al nombre del MKV."""

    mkv_name: str = ""
    """Nombre del fichero MKV de salida (sin ruta).
    Formato automático: '{Título} ({Año}) [DV FEL] [Audio DCP].mkv'"""

    mkv_name_manual: bool = False
    """True si el usuario ha editado el nombre manualmente. En este caso,
    los cambios de FEL/Audio DCP no sobreescriben el nombre."""

    audio_mode: str = "filtered"
    """Modo de selección de audio: 'filtered' (solo Castellano + VO) o
    'keep_all' (todas las pistas con labels automáticos, sin reordenar)."""

    subtitle_mode: str = "filtered"
    """Modo de selección de subtítulos: 'filtered' (Castellano + VO + Inglés
    con detección de AD) o 'keep_all' (todas las pistas con labels automáticos)."""

    included_tracks: list[IncludedAudioTrack | IncludedSubtitleTrack] = []
    """Pistas ordenadas que se incluirán en el MKV final. El orden aquí
    es el orden real de las pistas en el fichero MKV de salida."""

    discarded_tracks: list[DiscardedTrack] = []
    """Pistas excluidas. Se muestran al usuario con botón 'Recuperar'."""

    chapters: list[Chapter] = []
    """Lista de capítulos editable. Se escribe como XML al MKV en Fase E."""

    chapters_auto_generated: bool = False
    """True si los capítulos son generados automáticamente (no del disco)."""

    chapters_auto_reason: str = ""
    """Mensaje explicativo que se muestra en la UI cuando chapters_auto_generated=True."""

    vo_warning: str = ""
    """Advertencia cuando la VO no puede determinarse automáticamente (disco sin inglés ni español).
    Si no está vacío, se muestra en la pantalla de revisión y se requiere selección manual."""

    # ── Estado de ejecución ───────────────────────────────────────
    last_executed: datetime | None = None
    """Timestamp de la última ejecución exitosa (UTC)."""

    execution_started_at: datetime | None = None
    """Timestamp de inicio de la ejecución (UTC). Se usa junto con
    last_executed para calcular la duración total."""

    output_mkv_path: str | None = None
    """Ruta completa del MKV final generado. None hasta que Fase E completa."""

    error_message: str | None = None
    """Mensaje de error si status='error'."""

    output_log: list[str] = []
    """Líneas de output del proceso en curso (mkvmerge, mkvpropedit, etc.).
    Se envían en tiempo real por WebSocket y se persisten para reconexión.
    Al completar la ejecución, se copia a un ExecutionRecord en execution_history."""

    execution_history: list[ExecutionRecord] = []
    """Historial de ejecuciones anteriores. Cada ejecución (éxito o error)
    se registra como un ExecutionRecord con tiempos por fase y log completo."""


# ══════════════════════════════════════════════════════════════════════
#  PAYLOADS DE API
# ══════════════════════════════════════════════════════════════════════

class AnalyzeRequest(BaseModel):
    """Payload de POST /api/analyze."""

    iso_path: str
    """Ruta relativa al ISO dentro de /mnt/isos.
    Ej: 'El Rey de Reyes (2025) [FullBluRay].iso'"""


class QueueReorderRequest(BaseModel):
    """Payload de POST /api/queue/reorder."""

    ordered_ids: list[str]
    """Lista de session_ids en el nuevo orden deseado."""


class SessionUpdateRequest(BaseModel):
    """
    Payload de PUT /api/sessions/{id}.

    Todos los campos son opcionales (partial update). Solo se actualizan
    los campos presentes en el body.
    """

    has_fel: bool | None = None
    audio_dcp: bool | None = None
    mkv_name: str | None = None
    mkv_name_manual: bool | None = None
    included_tracks: list[IncludedAudioTrack | IncludedSubtitleTrack] | None = None
    discarded_tracks: list[DiscardedTrack] | None = None
    chapters: list[Chapter] | None = None


# ══════════════════════════════════════════════════════════════════════
#  TAB 2 — EDITAR MKV (modelos ephemeral, sin persistencia en disco)
# ══════════════════════════════════════════════════════════════════════

class MkvTrackInfo(BaseModel):
    """Pista de un MKV existente tal como la reporta mkvmerge -J."""

    id: int
    """ID de pista en mkvmerge (0-indexed)."""

    type: Literal["video", "audio", "subtitles"]
    """Tipo de pista."""

    codec: str
    """Nombre del codec (ej: 'HEVC/H.265/MPEG-H', 'TrueHD Atmos', 'HDMV PGS')."""

    language: str = ""
    """Código ISO 639-2 (ej: 'spa', 'eng'). Vacío si no especificado."""

    name: str = ""
    """Nombre actual de la pista en el MKV (puede estar vacío)."""

    flag_default: bool = False
    flag_forced: bool = False

    channels: int | None = None
    """Número de canales de audio (None para vídeo/subtítulos)."""

    sample_rate: int | None = None
    """Frecuencia de muestreo en Hz (None para vídeo/subtítulos)."""

    pixel_dimensions: str = ""
    """Resolución del vídeo (ej: '3840x2160'). Vacío para audio/subs."""

    # Campos enriquecidos por MediaInfo (opcionales, vacíos si no disponible)
    bitrate_kbps: int = 0
    """Bitrate real de la pista (de MediaInfo)."""

    format_commercial: str = ""
    """Nombre comercial (ej: 'Dolby TrueHD with Dolby Atmos')."""

    channel_layout: str = ""
    compression_mode: str = ""
    bit_depth: int = 0
    color_primaries: str = ""
    hdr_format: str = ""


class MkvAnalysisResult(BaseModel):
    """Resultado del análisis de un MKV existente con mkvmerge -J + MediaInfo."""

    file_path: str
    """Ruta absoluta al MKV."""

    file_name: str
    """Nombre del fichero (sin ruta)."""

    file_size_bytes: int = 0
    """Tamaño del fichero en bytes."""

    duration_seconds: float = 0.0
    """Duración en segundos."""

    title: str = ""
    """Título del contenedor MKV (segment info title)."""

    tracks: list[MkvTrackInfo] = []
    """Todas las pistas del MKV."""

    chapters: list[Chapter] = []
    """Capítulos del MKV."""

    has_fel: bool = False
    """True si se detecta Enhancement Layer HEVC 1080p."""

    hdr: HdrMetadata | None = None
    """Metadata HDR del vídeo principal (de MediaInfo)."""

    dovi: DoviInfo | None = None
    """Info Dolby Vision (de MediaInfo sobre MKV — básica, sin dovi_tool)."""

    mediainfo_raw: dict | None = None
    """JSON completo de MediaInfo para diagnóstico."""


class MkvEditTrack(BaseModel):
    """Edición de una pista individual en el MKV."""

    id: int
    """ID de pista original (mkvmerge)."""

    name: str | None = None
    """Nuevo nombre (None = no cambiar)."""

    flag_default: bool | None = None
    flag_forced: bool | None = None



class MkvEditRequest(BaseModel):
    """Payload de POST /api/mkv/apply."""

    file_path: str
    """Ruta absoluta al MKV a editar."""

    title: str | None = None
    """Nuevo título del contenedor (None = no cambiar)."""

    audio_tracks: list[MkvEditTrack] = []
    subtitle_tracks: list[MkvEditTrack] = []

    chapters: list[Chapter] | None = None
    """Nuevos capítulos (None = no cambiar)."""


# ══════════════════════════════════════════════════════════════════════
#  TAB 3 — CMV4.0 BD (inyección de RPU Dolby Vision CMv4.0)
# ══════════════════════════════════════════════════════════════════════

class CMv40Phase(str):
    """Estados del pipeline CMv4.0, ordenados secuencialmente."""
    CREATED          = "created"
    SOURCE_ANALYZED  = "source_analyzed"
    TARGET_PROVIDED  = "target_provided"
    EXTRACTED        = "extracted"
    SYNC_VERIFIED    = "sync_verified"
    SYNC_CORRECTED   = "sync_corrected"
    INJECTED         = "injected"
    REMUXED          = "remuxed"
    VALIDATED        = "validated"
    DONE             = "done"
    ERROR            = "error"
    CANCELLED        = "cancelled"


# Orden secuencial de fases (para comparaciones de progreso)
CMV40_PHASES_ORDER = [
    "created", "source_analyzed", "target_provided", "extracted",
    "sync_verified", "sync_corrected", "injected", "remuxed", "validated", "done",
]


class CMv40PhaseRecord(BaseModel):
    """Registro de ejecución de una fase del pipeline CMv4.0."""

    phase: str
    """Nombre de la fase (ej: 'analyze_source', 'extract', 'inject')."""

    started_at: datetime
    finished_at: datetime | None = None
    status: str = "running"
    """'running' | 'done' | 'error' | 'cancelled'"""

    error_message: str | None = None
    elapsed_seconds: float | None = None
    output_log: list[str] = []


class CMv40Session(BaseModel):
    """
    Proyecto CMv4.0 — convierte un MKV con CMv2.9 a CMv4.0 inyectando
    un RPU externo sincronizado.

    Persistencia: /config/cmv40/{id}.json
    Artefactos (HEVC, RPU.bin): /mnt/tmp/cmv40/{id}/

    Ciclo de vida por fases (ver CMv40Phase):
      created → source_analyzed → target_provided → extracted
      → sync_verified → (sync_corrected) → injected → remuxed → validated → done
    """

    id: str
    """Identificador único. Formato: 'cmv40_{titulo}_{año}_{timestamp}'."""

    # ── Fuente (MKV con CMv2.9) ──────────────────────────────────
    source_mkv_path: str
    """Ruta absoluta al MKV origen."""

    source_mkv_name: str
    """Nombre del fichero MKV (sin ruta)."""

    source_dv_info: DoviInfo | None = None
    """Análisis DV del MKV origen (de dovi_tool info sobre RPU extraído)."""

    source_frame_count: int = 0
    """Número total de frames del vídeo origen."""

    source_fps: float = 23.976
    """FPS del vídeo origen (típico en UHD BD: 23.976). Usado para conversiones s↔frames en la UI."""

    source_video_codec: str = ""
    source_duration_seconds: float = 0.0

    ffmpeg_wall_seconds: float = 0.0
    """Wall-time medido de la extracción ffmpeg HEVC del source (Fase A).
    Usado como ancla para estimar el ETA de fases silenciosas posteriores
    (extract-rpu, demux, inject, mux) — I/O del NAS es el bottleneck común."""

    # ── Target (RPU CMv4.0) ──────────────────────────────────────
    target_rpu_source: str = ""
    """Tipo de fuente: 'path' (fichero en /mnt/cmv40_rpus/) o 'mkv' (extraído de otro MKV)."""

    target_rpu_path: str = ""
    """Ruta al .bin de origen (antes de copiarlo al workdir)."""

    target_dv_info: DoviInfo | None = None
    """Análisis DV del RPU target."""

    target_frame_count: int = 0

    # ── Estado del pipeline ───────────────────────────────────────
    phase: str = "created"
    """Fase actual. Ver CMv40Phase."""

    artifacts_dir: str = ""
    """Ruta absoluta al workdir de artefactos (/mnt/tmp/cmv40/{id})."""

    output_mkv_name: str = ""
    """Nombre del MKV final (generado: {title} [DV FEL CMv4.0].mkv)."""

    output_mkv_path: str = ""
    """Ruta final del MKV en /mnt/output/ (solo tras validación)."""

    # ── Sincronización ────────────────────────────────────────────
    sync_delta: int = 0
    """Diferencia de frames detectada: target_frame_count - source_frame_count.
    Positivo = target tiene frames de más (eliminar). Negativo = target tiene de menos (duplicar)."""

    sync_offset_detected: int | None = None
    """Offset detectado automáticamente por cross-correlation (None = no calculado)."""

    sync_config: dict | None = None
    """editor_config.json aplicado (remove/duplicate). None si no hubo corrección."""

    # ── Metadata del proyecto ─────────────────────────────────────
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    output_log: list[str] = []
    """Log acumulado de todas las fases del pipeline."""

    phase_history: list[CMv40PhaseRecord] = []
    """Historial de ejecuciones de cada fase."""

    error_message: str = ""
    """Último mensaje de error (si phase == 'error')."""

    archived: bool = False
    """True si se ejecutó 'cleanup' — los artefactos intermedios se borraron.
    No se pueden rehacer fases. El proyecto queda en modo solo lectura."""

    running_phase: str | None = None
    """Fase ejecutándose ahora mismo ('analyze_source', 'extract', 'inject', 'remux').
    Cuando != None, la UI muestra modo modal con log + cancelar. Al terminar se limpia."""
