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

El módulo es puro — sin subprocess, sin red y sin tocar la sesión — así
que la matriz completa se puede recorrer en un test (ver
`tests/test_cmv40_strategy.py`, 48 combinaciones en 0,2 s).

Lo único que lee es el catálogo de traducción, y por lo mismo: el texto que
el usuario ve tiene que salir del MISMO objeto que la decisión, así que
sacarlo a otro módulo rompería justo el invariante que este fichero existe
para sostener. `tr()` cachea el JSON en la primera llamada — ni subprocess ni
red. Para que los tests no dependan de la redacción, cada `InjectPlan` lleva
además su `plan_key`: **lo estable es la clave, no la frase.**

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

from i18n import t as tr

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
    def drop_in_posible(self) -> bool:
        """Las condiciones del drop-in que NO dependen de los trust gates.

        Sirve para PREDECIR la ruta antes de que la Fase B evalúe los gates,
        que es cuando `target_trust_ok` todavía no existe. Las dos
        estructurales se saben mucho antes: el `target_type` lo fija el
        pre-flight y el `source_workflow`, la Fase A.

        No es el drop-in: un bin `trusted_p7_fel_final` cuyos gates no pasen
        va por merge. Quien tenga los gates delante debe usar `drop_in`.
        """
        return (
            self.source_workflow == "p7_fel"
            and self.target_type == "trusted_p7_fel_final"
            and self.trust_override != "force_interactive"
        )

    @property
    def drop_in(self) -> bool:
        """Bin P7 FEL CMv4.0 ya cocinado sobre un source P7 FEL, con gates OK.

        Permite inyectar sobre BL+EL sin demux ni mux: ahorra ~90 GB de I/O
        temporal y las dos operaciones más largas del pipeline.
        """
        return self.drop_in_posible and self.target_trust_ok

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
        # El separador va al catálogo porque es gramática: «y», «and», «i».
        return ("[Fase C] 📋 Plan: "
                + f" {tr('comun.y')} ".join(self.plan_parts) + ".")

    @property
    def result_text(self) -> str:
        """`🎯 Resultado` de la fase: lo que queda listo para Fase F/G."""
        partes: list[str] = []
        if self.needs_demux:
            partes.append(" + ".join(self.demux_artifacts))
        if not self.skip_per_frame_data:
            partes.append(tr('cmv40_strategy.res_c_per_frame'))
        if not partes:
            partes.append(tr('cmv40_strategy.res_c_sin_artefactos'))
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
    plan_key: str        # lo estable para un test: la clave, no la frase
    result_text: str
    skipped_markers: tuple[str, ...] = ()

    @property
    def missing_input_error(self) -> str:
        return tr('cmv40_strategy.falta_entrada',
                  entrada=self.required_input,
                  consejo=self.required_input_hint)


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
        parts.append(tr('cmv40_strategy.parte_c_demux'))
    if not skip_pfd:
        parts.append(tr('cmv40_strategy.parte_c_per_frame'))
    if not parts:
        parts.append(tr('cmv40_strategy.parte_c_nada'))

    if inp.drop_in:
        skip_reason = "[Fase C] " + tr('cmv40_strategy.skip_c_drop_in')
    else:
        skip_reason = "[Fase C] " + tr('cmv40_strategy.skip_c_p8')

    es_fel = inp.source_workflow == "p7_fel"
    return ExtractPlan(
        needs_demux=needs_demux,
        demux_label=("BL + EL" if es_fel
                     else tr('cmv40_strategy.demux_label_mel')),
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
            required_input_hint=tr('cmv40_strategy.hint_fase_a_drop_in'),
            hevc_input=SOURCE_HEVC,
            hevc_output=SOURCE_INJECTED,
            needs_merge=False,
            needs_profile8=False,
            inject_label=tr('cmv40_strategy.label_f_drop_in'),
            plan_text=("[Fase F] 📋 Plan: "
                       + tr('cmv40_strategy.plan_f_drop_in')),
            plan_key='cmv40_strategy.plan_f_drop_in',
            result_text=tr('cmv40_strategy.res_f_drop_in'),
            skipped_markers=("merge_cmv40_transfer",),
        )

    if inp.source_workflow == "p7_fel":
        return InjectPlan(
            required_input=EL_HEVC,
            required_input_hint=tr('cmv40_strategy.hint_fase_c'),
            hevc_input=EL_HEVC,
            hevc_output=EL_INJECTED,
            needs_merge=True,
            needs_profile8=False,
            inject_label=tr('cmv40_strategy.label_f_p7fel_merge'),
            plan_text=("[Fase F] 📋 Plan: "
                       + tr('cmv40_strategy.plan_f_p7fel_merge')),
            plan_key='cmv40_strategy.plan_f_p7fel_merge',
            result_text=tr('cmv40_strategy.res_f_p7fel_merge'),
        )

    if inp.source_workflow == "p7_mel":
        if inp.target_needs_merge:
            plan_key = 'cmv40_strategy.plan_f_p7mel_merge'
            inject_label = tr('cmv40_strategy.label_f_p7mel_merge')
        else:
            plan_key = 'cmv40_strategy.plan_f_p7mel_directo'
            inject_label = tr('cmv40_strategy.label_f_p7mel_directo')
        return InjectPlan(
            required_input=BL_HEVC,
            required_input_hint=tr('cmv40_strategy.hint_fase_c'),
            hevc_input=BL_HEVC,
            hevc_output=BL_INJECTED,
            needs_merge=inp.target_needs_merge,
            needs_profile8=True,
            inject_label=inject_label,
            plan_text="[Fase F] 📋 Plan: " + tr(plan_key),
            plan_key=plan_key,
            result_text=tr('cmv40_strategy.res_f_p7mel'),
        )

    # p8: el source ya es single-layer, se inyecta sobre él mismo. El output
    # reutiliza el slot BL_injected porque Fase G lee ese nombre para las dos
    # ramas single-layer.
    if inp.target_needs_merge:
        plan_key = 'cmv40_strategy.plan_f_p8_merge'
        inject_label = tr('cmv40_strategy.label_f_p8_merge')
    else:
        plan_key = 'cmv40_strategy.plan_f_p8_directo'
        inject_label = tr('cmv40_strategy.label_f_p8_directo')
    return InjectPlan(
        required_input=SOURCE_HEVC,
        required_input_hint=tr('cmv40_strategy.hint_fase_a'),
        hevc_input=SOURCE_HEVC,
        hevc_output=BL_INJECTED,
        needs_merge=inp.target_needs_merge,
        needs_profile8=True,
        inject_label=inject_label,
        plan_text="[Fase F] 📋 Plan: " + tr(plan_key),
        plan_key=plan_key,
        result_text=tr('cmv40_strategy.res_f_p8'),
    )


def _remux_plan(inp: WorkflowInputs) -> RemuxPlan:
    if inp.drop_in:
        return RemuxPlan(
            needs_dovi_mux=False,
            mux_inputs=(),
            hevc_for_mkv=SOURCE_INJECTED,
            video_track_name="HEVC DV P7 FEL CMv4.0",
            prewarm_validation=False,
            plan_text="[Fase G] 📋 Plan: " + tr(
                'cmv40_strategy.plan_g_drop_in', hevc=SOURCE_INJECTED),
        )

    if inp.source_workflow == "p7_fel":
        return RemuxPlan(
            needs_dovi_mux=True,
            mux_inputs=(BL_HEVC, EL_INJECTED),
            hevc_for_mkv=DV_DUAL,
            video_track_name="HEVC DV P7 FEL CMv4.0",
            prewarm_validation=True,
            plan_text="[Fase G] 📋 Plan: " + tr(
                'cmv40_strategy.plan_g_p7fel', bl=BL_HEVC, el=EL_INJECTED),
        )

    if inp.source_workflow == "p7_mel":
        plan_text = "[Fase G] 📋 Plan: " + tr(
            'cmv40_strategy.plan_g_p7mel', hevc=BL_INJECTED)
        track = "HEVC DV P8.1 CMv4.0 (from P7 MEL)"
    else:
        plan_text = "[Fase G] 📋 Plan: " + tr(
            'cmv40_strategy.plan_g_p8', hevc=BL_INJECTED)
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
                "[Fase H] 📋 Plan "
                + tr('cmv40_strategy.plan_h_drop_in_rama') + ": "
                + tr('cmv40_strategy.plan_h_drop_in')),
        )
    return ValidatePlan(
        fast_path=False,
        # Solo p7_fel conserva capa de mejora; en el resto el el_type del RPU
        # final no está fijado por el workflow.
        expected_el_type="FEL" if inp.source_workflow == "p7_fel" else None,
        plan_text="[Fase H] 📋 Plan: " + tr('cmv40_strategy.plan_h_merge'),
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


def va_por_drop_in(session) -> bool:
    """¿Va esta sesión por la ruta drop-in? La respuesta que puede darse HOY.

    Existe para que nadie vuelva a escribir la condición a mano fuera de
    aquí. `recommend_action` la tenía replicada con otras reglas —perfil
    coincidente en las TRES combinaciones (FEL/FEL, MEL/MEL, P8/P8) más L2
    idéntico, sin mirar `target_type` ni los gates— y prometía «~30
    segundos» en jobs que acababan haciendo el merge completo. Medido sobre
    el `/config` del NAS: **10 de los 41 proyectos con recomendación**, y
    los 8 terminados salieron con `output_workflow=restore_merge`.

    Los gates parten la respuesta en dos momentos, y por eso no basta con
    `plan.drop_in`:

    - **antes de la Fase B** no existe `target_trust_ok`, así que lo único
      honesto es la predicción estructural (`drop_in_posible`). Es lo que
      hace ya el frontend en `_cmv40PlanAutoSteps` para no sumarle al ETA un
      demux fantasma de ~13 min;
    - **después** manda el dato real, gates incluidos: un bin
      `trusted_p7_fel_final` que no los pase va por merge.

    La señal de «los gates ya se evaluaron» es que `target_trust_gates` esté
    poblado — lo escribe `_analyze_target_rpu` y nadie más. Se prefiere al
    orden de fases porque no depende de por dónde vaya el pipeline.
    """
    inp = WorkflowInputs.from_session(session)
    if getattr(session, "target_trust_gates", None):
        return inp.drop_in
    return inp.drop_in_posible
