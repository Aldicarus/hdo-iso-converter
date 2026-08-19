"""
cmv40_strategy.py — la matriz de workflows CMv4.0, como datos.

Qué hace cada fase del pipeline depende de tres entradas: el workflow del
source (`p7_fel` / `p7_mel` / `p8`), la clase del bin target
(`target_type`) y si los trust gates pasaron. Esa combinación decidía el
comportamiento en unos treinta puntos de ramificación repartidos por las
fases C, F, G y H, y en cada uno había que volver a razonar la misma tabla.

Dos problemas concretos de tenerlo así:

1. **Añadir un workflow eran ~30 sitios** sin lista de verificación de si
   te habías dejado alguno.

2. **El texto y la decisión se calculaban por separado.** Cada fase
   ramificaba una vez para emitir su `📋 Plan` y otra vez, decenas de
   líneas más abajo, para decidir de verdad. Nada en el código obligaba a
   que coincidieran. Fue exactamente el fallo de "Te van a matar"
   (2026-08-15): la pista se llamaba "P8.1 CMv4.0" y MediaInfo leía
   `dvhe.07`.

Aquí la decisión y su explicación salen del **mismo objeto**, así que no
pueden divergir: si cambias lo que hace una rama, el texto que el usuario
lee en el log cambia con ella.

El módulo es puro — sin IO, sin subprocess, sin tocar la sesión — así que
la matriz completa se puede recorrer en un test (ver
`tests/test_cmv40_strategy.py`).

Lo que NO se decide aquí, a propósito:

  * **Los `rpu_levels` del merge.** Dependen del `el_type` que
    `dovi_tool info` lee del RPU real, no del `source_workflow` que
    arrastra la sesión — es la marca autoritativa y puede no coincidir.
    Los elige `_merge_cmv40_into_p7` con el RPU delante.
  * **Si un RPU necesita convertirse a Profile 8.1.** Aquí se dice que la
    rama lo REQUIERE (`needs_profile8`); comprobar si ya lo es le toca a
    `_ensure_profile8_rpu`.
"""
from dataclasses import dataclass, field

# Nombres de artefactos en el workdir. Están aquí porque quién produce y
# quién consume cada uno es parte de la matriz: Fase F escribe
# `BL_injected.hevc` en los dos workflows single-layer y Fase G lo lee.
SOURCE_HEVC = "source.hevc"
BL_HEVC = "BL.hevc"
EL_HEVC = "EL.hevc"
SOURCE_INJECTED = "source_injected.hevc"
EL_INJECTED = "EL_injected.hevc"
BL_INJECTED = "BL_injected.hevc"
DV_DUAL = "DV_dual.hevc"

WORKFLOWS = ("p7_fel", "p7_mel", "p8")

# Bins cuyo RPU sirve de donante de levels pero cuya estructura no encaja
# con un source single-layer: hay que mergear en vez de inyectar directo.
_NEEDS_MERGE_TARGETS = ("trusted_p7_fel_final", "trusted_p7_mel_final", "generic")


# ══════════════════════════════════════════════════════════════════════
#  Entradas
# ══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class WorkflowInputs:
    """Lo único de la sesión que la matriz mira."""
    source_workflow: str = "p7_fel"
    target_type: str = ""
    target_trust_ok: bool = False
    trust_override: str = "auto"
    user_acknowledged: bool = False

    @classmethod
    def from_session(cls, session) -> "WorkflowInputs":
        return cls(
            source_workflow=session.source_workflow or "p7_fel",
            target_type=session.target_type or "",
            target_trust_ok=bool(session.target_trust_ok),
            trust_override=session.trust_override or "auto",
            user_acknowledged=bool(session.user_acknowledged_degradation),
        )

    @property
    def trust_effective(self) -> bool:
        """Trust que el pipeline honra: gates OK y sin revisión manual forzada.

        Es la condición que decide saltarse la Fase D y los datos del chart.
        Estaba escrita a mano en varios sitios del backend y siete veces en
        `app.js`.
        """
        return self.target_trust_ok and self.trust_override != "force_interactive"

    @property
    def skip_sync_review(self) -> bool:
        """¿Puede el pipeline saltarse la revisión visual de la Fase D?

        Dos vías la habilitan: que los gates hayan pasado (trust efectivo) o
        que el usuario haya aceptado la degradación de un gate que la Fase D
        no puede arreglar. **Ninguna sobrevive a `force_interactive`.**

        Ese último punto arregla una divergencia real: el orquestador hacía
        `trusted_auto or user_acked` —con el ACK dado se saltaba la Fase D
        aunque el usuario hubiera pedido revisión manual— mientras el
        frontend exigía además que no hubiera `force_interactive`. Con
        user_acked=True y force_interactive, uno avanzaba y el otro no.

        Manda la lectura del frontend, que es la coherente: pedir revisar el
        sync a mano y aceptar que el grading diverge son decisiones
        distintas, y aceptar la segunda no anula la primera.
        """
        if self.trust_override == "force_interactive":
            return False
        return self.target_trust_ok or self.user_acknowledged

    @property
    def drop_in(self) -> bool:
        """Bin P7 FEL CMv4.0 ya cocinado sobre un source P7 FEL, con gates OK.

        Permite inyectar sobre BL+EL sin demux ni mux: ahorra ~90 GB de I/O
        temporal y las dos operaciones más largas del pipeline.
        """
        return (
            self.source_workflow == "p7_fel"
            and self.target_type == "trusted_p7_fel_final"
            and self.trust_effective
        )

    @property
    def single_layer_output(self) -> bool:
        """El stream resultante no tiene capa de mejora.

        `p7_mel` descarta el EL (MEL no aporta imagen) y `p8` nunca lo tuvo.
        Un RPU Profile 7 en un fichero así lo anunciaría como dual-layer.
        """
        return self.source_workflow in ("p7_mel", "p8")

    @property
    def target_needs_merge(self) -> bool:
        """El bin no encaja como reemplazo directo del RPU del source."""
        return self.target_type in _NEEDS_MERGE_TARGETS


# ══════════════════════════════════════════════════════════════════════
#  Planes por fase
# ══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ExtractPlan:
    """Fase C — separar capas y preparar los datos del chart."""
    needs_demux: bool
    demux_label: str
    skip_per_frame_data: bool
    skipped_markers: tuple[str, ...]
    plan_parts: tuple[str, ...]
    skip_reason: str
    discards_el: bool          # el EL MEL no aporta imagen: se borra tras demux
    demux_artifacts: tuple[str, ...]   # lo que el demux deja utilizable

    @property
    def plan_text(self) -> str:
        return "[Fase C] 📋 Plan: " + " y ".join(self.plan_parts) + "."

    @property
    def result_text(self) -> str:
        """`🎯 Resultado` de la fase: lo que queda listo para Fase F/G."""
        partes: list[str] = []
        if self.needs_demux:
            partes.append(" + ".join(self.demux_artifacts))
        if not self.skip_per_frame_data:
            partes.append("per_frame_data.json para el chart")
        if not partes:
            partes.append("sin artefactos intermedios — la cadena drop-in "
                          "usará directamente source.hevc")
        return "[Fase C] 🎯 Resultado: " + ", ".join(partes) + "."


@dataclass(frozen=True)
class InjectPlan:
    """Fase F — qué RPU se inyecta en qué HEVC."""
    required_input: str
    required_input_hint: str
    hevc_input: str
    hevc_output: str
    needs_merge: bool
    needs_profile8: bool
    inject_label: str
    plan_text: str
    result_text: str
    skipped_markers: tuple[str, ...] = ()

    @property
    def missing_input_error(self) -> str:
        return f"{self.required_input} no existe — {self.required_input_hint}"


@dataclass(frozen=True)
class RemuxPlan:
    """Fase G — ensamblar el MKV final."""
    needs_dovi_mux: bool
    mux_inputs: tuple[str, ...]
    hevc_for_mkv: str
    video_track_name: str
    prewarm_validation: bool
    plan_text: str


@dataclass(frozen=True)
class ValidatePlan:
    """Fase H — cómo se comprueba el resultado antes del rename."""
    fast_path: bool
    expected_el_type: str | None
    plan_text: str


@dataclass(frozen=True)
class WorkflowPlan:
    """Plan completo del job para las cuatro fases que ramifican."""
    inputs: WorkflowInputs
    extract: ExtractPlan
    inject: InjectPlan
    remux: RemuxPlan
    validate: ValidatePlan

    @property
    def drop_in(self) -> bool:
        return self.inputs.drop_in

    def to_dict(self) -> dict:
        """Lo que la UI necesita saber del plan, para que no lo re-derive.

        `app.js` calculaba por su cuenta el trust efectivo (once veces, en dos
        variantes sintácticas distintas), el drop-in, si el target necesita
        merge y si hay demux o mux. Cada réplica es una copia de una regla
        que vive aquí, y se desincroniza en silencio — la misma clase de
        problema que el `📋 Plan` divergiendo de la decisión.

        Solo van booleanos y nombres de artefactos: los textos largos del
        plan ya viajan por el log, y esto se serializa en cada GET de la
        sesión.
        """
        return {
            "drop_in": self.inputs.drop_in,
            "trust_effective": self.inputs.trust_effective,
            "skip_sync_review": self.inputs.skip_sync_review,
            "single_layer_output": self.inputs.single_layer_output,
            "target_needs_merge": self.inputs.target_needs_merge,
            "extract": {
                "needs_demux": self.extract.needs_demux,
                "skip_per_frame_data": self.extract.skip_per_frame_data,
                "discards_el": self.extract.discards_el,
            },
            "inject": {
                "needs_merge": self.inject.needs_merge,
                "needs_profile8": self.inject.needs_profile8,
                "hevc_input": self.inject.hevc_input,
                "hevc_output": self.inject.hevc_output,
            },
            "remux": {
                "needs_dovi_mux": self.remux.needs_dovi_mux,
                "hevc_for_mkv": self.remux.hevc_for_mkv,
                "video_track_name": self.remux.video_track_name,
                "prewarm_validation": self.remux.prewarm_validation,
            },
            "validate": {
                "fast_path": self.validate.fast_path,
                "expected_el_type": self.validate.expected_el_type,
            },
        }


# ══════════════════════════════════════════════════════════════════════
#  Resolución
# ══════════════════════════════════════════════════════════════════════

def _extract_plan(inp: WorkflowInputs) -> ExtractPlan:
    needs_demux = inp.source_workflow in ("p7_fel", "p7_mel") and not inp.drop_in
    skip_pfd = inp.trust_effective

    markers: list[str] = []
    if inp.drop_in:
        markers += ["demux_dual_layer", "mux_dual_layer"]
    if skip_pfd:
        markers.append("per_frame_data_skipped")

    parts: list[str] = []
    if needs_demux:
        parts.append("separar el HEVC dual-layer en BL.hevc + EL.hevc (dovi_tool demux)")
    if not skip_pfd:
        parts.append("generar per_frame_data.json con la luminancia por frame "
                     "de source y target (para el chart de Fase D)")
    if not parts:
        parts.append("no hacer nada — tanto el demux como el per-frame se saltan "
                     "porque el target es trusted drop-in")

    if inp.drop_in:
        skip_reason = (
            "[Fase C] ⏭ Demux omitido — drop-in FEL: inject-rpu irá "
            "directo sobre source.hevc (BL+EL), no hace falta separar capas. "
            "Ahorro ~90 GB I/O."
        )
    else:
        skip_reason = (
            "[Fase C] Workflow P8: sin demux necesario (source ya es single-layer)"
        )

    es_fel = inp.source_workflow == "p7_fel"
    return ExtractPlan(
        needs_demux=needs_demux,
        demux_label="BL + EL" if es_fel else "BL (EL MEL será ignorado)",
        skip_per_frame_data=skip_pfd,
        skipped_markers=tuple(markers),
        plan_parts=tuple(parts),
        skip_reason=skip_reason,
        discards_el=inp.source_workflow == "p7_mel",
        demux_artifacts=(BL_HEVC, EL_HEVC) if es_fel else (BL_HEVC,),
    )


def _inject_plan(inp: WorkflowInputs) -> InjectPlan:
    if inp.drop_in:
        return InjectPlan(
            required_input=SOURCE_HEVC,
            required_input_hint="ejecuta Fase A primero (drop-in opera sobre BL+EL)",
            hevc_input=SOURCE_HEVC,
            hevc_output=SOURCE_INJECTED,
            needs_merge=False,
            needs_profile8=False,
            inject_label="Inyectando RPU trusted directo sobre BL+EL (drop-in)",
            plan_text=(
                "[Fase F] 📋 Plan: target P7 FEL CMv4.0 ya cocinado y gates trusted → "
                "ruta DROP-IN. Inyectamos el RPU target directamente en source.hevc "
                "(BL+EL intactos, sin demux previo ni mux posterior). Es la vía más "
                "rápida y limpia — el byte-identical del RPU queda garantizado."
            ),
            result_text=(
                "BL+EL intactos con el RPU CMv4.0 inyectado — stream dual-layer "
                "íntegro listo para multiplexar."
            ),
            skipped_markers=("merge_cmv40_transfer",),
        )

    if inp.source_workflow == "p7_fel":
        return InjectPlan(
            required_input=EL_HEVC,
            required_input_hint="ejecuta Fase C primero",
            hevc_input=EL_HEVC,
            hevc_output=EL_INJECTED,
            needs_merge=True,
            needs_profile8=False,
            inject_label="Inyectando RPU merged en EL (preserva FEL)",
            plan_text=(
                "[Fase F] 📋 Plan: source P7 FEL + target P8.x (retail/generated) → "
                "MERGE clásico. Transferimos L3/L8-L11 del target al RPU P7 del source "
                "preservando la FEL, luego inyectamos el RPU merged en EL.hevc. "
                "Resultado: P7 FEL con trims CMv4.0."
            ),
            result_text=(
                "EL con el RPU merged inyectado; BL.hevc original sin tocar — "
                "stream dual-layer P7 FEL listo para combinar."
            ),
        )

    if inp.source_workflow == "p7_mel":
        if inp.target_needs_merge:
            plan_text = (
                "[Fase F] 📋 Plan: source P7 MEL + target P7/generic → descartamos "
                "el EL MEL del source y mergeamos los levels CMv4.0 del target "
                "en el RPU del source preservando profile. Inyectamos el RPU "
                "merged en BL.hevc. Resultado: MKV single-layer P8.1 CMv4.0."
            )
            inject_label = "Inyectando RPU merged en BL (MEL descartado → P8.1 CMv4.0)"
        else:
            plan_text = (
                "[Fase F] 📋 Plan: source P7 MEL + target P8 retail → descartamos "
                "EL MEL e inyectamos el RPU target directamente en BL.hevc. "
                "Resultado: MKV single-layer P8.1 CMv4.0 — mismo profile, sin merge."
            )
            inject_label = "Inyectando RPU target en BL (MEL descartado → P8.1)"
        return InjectPlan(
            required_input=BL_HEVC,
            required_input_hint="ejecuta Fase C primero",
            hevc_input=BL_HEVC,
            hevc_output=BL_INJECTED,
            needs_merge=inp.target_needs_merge,
            needs_profile8=True,
            inject_label=inject_label,
            plan_text=plan_text,
            result_text=(
                "BL con el RPU inyectado (EL MEL descartado) — stream single-layer "
                "P8.1 CMv4.0 listo para remuxar."
            ),
        )

    # p8: el source ya es single-layer, se inyecta sobre él mismo. El output
    # reutiliza el slot BL_injected porque Fase G lee ese nombre para las dos
    # ramas single-layer.
    if inp.target_needs_merge:
        plan_text = (
            "[Fase F] 📋 Plan: source P8.1 + target P7/generic → mergeamos los "
            "levels CMv4.0 del target (L3/L8/L9/L11) en el RPU P8 del source. El output "
            "hereda el profile P8.1 del source (no se mezclan capas, solo metadata). "
            "Inyectamos el RPU merged en source.hevc. Resultado: P8.1 CMv4.0."
        )
        inject_label = "Inyectando RPU merged en source.hevc (P8.1 CMv4.0)"
    else:
        plan_text = (
            "[Fase F] 📋 Plan: source P8.1 + target P8 retail → mismo profile, "
            "inyectamos el RPU target directamente en source.hevc (reemplaza el "
            "RPU CMv2.9 existente). Resultado: P8.1 con CMv4.0 refinado."
        )
        inject_label = "Inyectando RPU target en source.hevc (P8 → P8.1 CMv4.0)"
    return InjectPlan(
        required_input=SOURCE_HEVC,
        required_input_hint="ejecuta Fase A primero",
        hevc_input=SOURCE_HEVC,
        hevc_output=BL_INJECTED,
        needs_merge=inp.target_needs_merge,
        needs_profile8=True,
        inject_label=inject_label,
        plan_text=plan_text,
        result_text=(
            "HEVC single-layer con el RPU CMv4.0 inyectado — listo para "
            "remuxar."
        ),
    )


def _remux_plan(inp: WorkflowInputs) -> RemuxPlan:
    if inp.drop_in:
        return RemuxPlan(
            needs_dovi_mux=False,
            mux_inputs=(),
            hevc_for_mkv=SOURCE_INJECTED,
            video_track_name="HEVC DV P7 FEL CMv4.0",
            prewarm_validation=False,
            plan_text=(
                "[Fase G] 📋 Plan: ensamblar el MKV final. source_injected.hevc ya es "
                "BL+EL dual-layer con el RPU CMv4.0 inyectado (drop-in) — solo "
                "necesitamos mkvmerge para añadir audio/subs/capítulos del origen. "
                "Saltamos el dovi_tool mux (innecesario, el stream ya está íntegro)."
            ),
        )

    if inp.source_workflow == "p7_fel":
        return RemuxPlan(
            needs_dovi_mux=True,
            mux_inputs=(BL_HEVC, EL_INJECTED),
            hevc_for_mkv=DV_DUAL,
            video_track_name="HEVC DV P7 FEL CMv4.0",
            prewarm_validation=True,
            plan_text=(
                "[Fase G] 📋 Plan: ensamblar el MKV final. Workflow P7 FEL con merge "
                "— primero dovi_tool mux combina BL.hevc + EL_injected.hevc en un "
                "HEVC dual-layer, luego mkvmerge añade audio/subs/capítulos del origen."
            ),
        )

    if inp.source_workflow == "p7_mel":
        plan_text = (
            "[Fase G] 📋 Plan: ensamblar el MKV final single-layer. El EL MEL "
            "se descarta (no aporta) → mkvmerge directo sobre BL_injected.hevc "
            "con audio/subs/capítulos del origen. Resultado: P8.1 CMv4.0 ligero."
        )
        track = "HEVC DV P8.1 CMv4.0 (from P7 MEL)"
    else:
        plan_text = (
            "[Fase G] 📋 Plan: ensamblar el MKV final. Source era P8.1 single-layer "
            "→ mkvmerge directo sobre BL_injected.hevc con audio/subs/"
            "capítulos del origen."
        )
        track = "HEVC DV P8.1 CMv4.0"
    return RemuxPlan(
        needs_dovi_mux=False,
        mux_inputs=(),
        hevc_for_mkv=BL_INJECTED,
        video_track_name=track,
        prewarm_validation=True,
        plan_text=plan_text,
    )


def _validate_plan(inp: WorkflowInputs) -> ValidatePlan:
    if inp.drop_in:
        return ValidatePlan(
            fast_path=True,
            expected_el_type="FEL",
            plan_text=(
                "[Fase H] 📋 Plan (fast path drop-in FEL): el RPU del MKV es bit-a-bit "
                "el RPU_target.bin — inject-rpu lo copió íntegro sin tocarlo. El bin ya "
                "pasó pre-flight + Fase B con CMv4.0 confirmado y trust gates OK, así "
                "que la cadena upstream garantiza Profile 7 FEL CMv4.0 en el output. "
                "Validamos integridad del MKV con mkvmerge -J y frame count con ffprobe; "
                "saltamos el extract-rpu completo (ahorra ~5-8 min en UHD)."
            ),
        )
    return ValidatePlan(
        fast_path=False,
        # Solo p7_fel conserva capa de mejora; en el resto el el_type del RPU
        # final no está fijado por el workflow.
        expected_el_type="FEL" if inp.source_workflow == "p7_fel" else None,
        plan_text=(
            "[Fase H] 📋 Plan: validar el resultado antes de mover el MKV al output "
            "final. Leemos el RPU del HEVC resultante, confirmamos que tiene CMv4.0 "
            "y que el frame count coincide con el source. Si todo OK, rename atómico "
            ".tmp → .mkv (instantáneo, mismo filesystem) y cleanup de artefactos "
            "intermedios."
        ),
    )


def resolve_plan(session) -> WorkflowPlan:
    """Plan de las cuatro fases que ramifican, para una sesión concreta."""
    return plan_for(WorkflowInputs.from_session(session))


def plan_for(inp: WorkflowInputs) -> WorkflowPlan:
    """Igual que `resolve_plan` pero desde las entradas crudas — el que usan
    los tests para recorrer la matriz sin construir sesiones."""
    return WorkflowPlan(
        inputs=inp,
        extract=_extract_plan(inp),
        inject=_inject_plan(inp),
        remux=_remux_plan(inp),
        validate=_validate_plan(inp),
    )
