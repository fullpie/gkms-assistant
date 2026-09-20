"""Standalone Plan2 exam-scoring context bootstrapped from ExamSaveData.

This module owns only the score-application boundary that was missing between
the Plan2 effect adapters and the serialized audition state.  It deliberately
does not mutate the central horizon: callers explicitly carry the immutable
context returned by each operation.

``raw_parameter`` means the Int32 value presented to native ``AddParameter``.
For a Lesson effect, ``CalculateAddingParameter`` remains upstream and its
result is the raw value accepted here.  Review's dynamic end-turn result enters
the same boundary.  Auditions apply battle permille conversion; lessons use
the native identity path and retain their empty serialized turn schedule.
Per-hit limits and float32/ceil semantics are delegated to
:mod:`gkms_tool.native_exam_formula`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Final, Mapping

from .audition_local_save_state import LocalSaveExamState
from .exam_context import CurrentTurnBoundary, CurrentTurnState, close_current_turn
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    ParameterApplication,
    ParameterApplicationStatus,
    ProduceParameterType,
    apply_parameter_add,
)
from .plan2_exam_mode import (
    DEFAULT_PLAN2_AUDITION_EXAM_MODE,
    PLAN2_AUDITION_EXAM_TYPE,
    PLAN2_LESSON_EXAM_TYPE,
    Plan2ExamMode,
    Plan2ExamModeError,
    resolve_plan2_exam_mode,
)


PLAN2_EXAM_SAVE_BATTLE_SCORING_SCHEMA_VERSION: Final = 1
PLAN2_EXAM_SAVE_BATTLE_SCORING_ADAPTER_ID: Final = (
    "plan2.exam_save.battle_scoring"
)
AUDITION_EXAM_TYPE: Final = PLAN2_AUDITION_EXAM_TYPE
LESSON_EXAM_TYPE: Final = PLAN2_LESSON_EXAM_TYPE


class Plan2BattleScoringError(ValueError):
    """The supplied ExamSaveData scoring evidence is incomplete or invalid."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class Plan2ScoreKind(Enum):
    LESSON = "lesson"
    REVIEW = "review"


class Plan2ScoreIntegrationPoint(Enum):
    """Known callers that still need central wiring."""

    SCALAR_DIRECT = "scalar_direct"
    REVIEW_DYNAMIC = "review_dynamic"
    STATUS_CHILD = "status_child"
    TIMER = "timer"
    ITEM = "item"


PLAN2_SCORE_INTEGRATION_POINTS: Final = tuple(Plan2ScoreIntegrationPoint)


def _i32(
    value: object,
    label: str,
    *,
    minimum: int = INT32_MIN,
) -> int:
    if (
        type(value) is not int
        or value < INT32_MIN
        or value > INT32_MAX
        or value < minimum
    ):
        raise Plan2BattleScoringError("invalid-int32", label)
    return value


def _border(value: object, label: str) -> int:
    resolved = _i32(value, label)
    if resolved < -1:
        raise Plan2BattleScoringError("invalid-border", label)
    return resolved


def _parameter_type(value: object, label: str) -> ProduceParameterType:
    if isinstance(value, bool):
        raise Plan2BattleScoringError("invalid-parameter-type", label)
    try:
        resolved = ProduceParameterType(value)
    except (TypeError, ValueError) as error:
        raise Plan2BattleScoringError("invalid-parameter-type", label) from error
    if resolved not in {
        ProduceParameterType.VOCAL,
        ProduceParameterType.DANCE,
        ProduceParameterType.VISUAL,
    }:
        raise Plan2BattleScoringError("invalid-parameter-type", label)
    return resolved


def _enum(value: object, enum_type: type[Enum], label: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise Plan2BattleScoringError(f"invalid-{label}", repr(value)) from error


@dataclass(frozen=True, slots=True)
class Plan2ExamSaveBattleScoringContext:
    """Exact exam mode and mutable score fields carried between calls.

    The public name is retained for compatibility with the already-central
    Plan2 scoring integration.  Lesson mode carries the native empty turn
    schedule and resolves its fixed parameter type through ``exam_mode``.
    """

    schema_version: int
    current_turn: int
    limit_turn: int
    turn_parameter_types: tuple[ProduceParameterType, ...]
    vocal_bonus_permille: int
    dance_bonus_permille: int
    visual_bonus_permille: int
    judge_parameter: int
    limit_border: int = -1
    clear_border: int = -1
    current_turn_total_add_parameter: int = 0
    judge_parameter_vocal: int = 0
    judge_parameter_dance: int = 0
    judge_parameter_visual: int = 0
    exam_end_complete: bool = False
    exam_mode: Plan2ExamMode = DEFAULT_PLAN2_AUDITION_EXAM_MODE
    # ``limit_turn`` is the stage's base limit. ExamExtraTurn extends the
    # serialized schedule and the legal scoring boundary without rewriting
    # that Master-owned base value.
    extra_turn: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version
            != PLAN2_EXAM_SAVE_BATTLE_SCORING_SCHEMA_VERSION
        ):
            raise Plan2BattleScoringError(
                "schema-version-mismatch", str(self.schema_version)
            )
        current_turn = _i32(self.current_turn, "current_turn", minimum=1)
        limit_turn = _i32(self.limit_turn, "limit_turn", minimum=1)
        extra_turn = _i32(self.extra_turn, "extra_turn", minimum=0)
        total_turns = limit_turn + extra_turn
        if not isinstance(self.exam_mode, Plan2ExamMode):
            raise Plan2BattleScoringError("invalid-exam-mode")
        raw_schedule = tuple(self.turn_parameter_types)
        schedule = tuple(
            _parameter_type(value, f"turn_parameter_types[{index}]")
            for index, value in enumerate(raw_schedule)
        )
        if self.exam_mode.is_lesson:
            if schedule:
                raise Plan2BattleScoringError(
                    "lesson-schedule-not-empty", str(len(schedule))
                )
        elif len(schedule) not in {limit_turn, total_turns}:
            raise Plan2BattleScoringError(
                "schedule-length-mismatch",
                f"limit_turn={limit_turn}; extra_turn={extra_turn}; "
                f"schedule={len(schedule)}",
            )
        if current_turn > total_turns:
            raise Plan2BattleScoringError(
                "schedule-current-turn-unmapped", str(current_turn)
            )
        object.__setattr__(self, "turn_parameter_types", schedule)

        _i32(self.vocal_bonus_permille, "vocal_bonus_permille", minimum=0)
        _i32(self.dance_bonus_permille, "dance_bonus_permille", minimum=0)
        _i32(self.visual_bonus_permille, "visual_bonus_permille", minimum=0)
        score = _i32(self.judge_parameter, "judge_parameter", minimum=0)
        limit = _border(self.limit_border, "limit_border")
        clear = _border(self.clear_border, "clear_border")
        _i32(
            self.current_turn_total_add_parameter,
            "current_turn_total_add_parameter",
            minimum=0,
        )
        _i32(self.judge_parameter_vocal, "judge_parameter_vocal", minimum=0)
        _i32(self.judge_parameter_dance, "judge_parameter_dance", minimum=0)
        _i32(self.judge_parameter_visual, "judge_parameter_visual", minimum=0)
        if type(self.exam_end_complete) is not bool:
            raise Plan2BattleScoringError("invalid-boolean", "exam_end_complete")

    @property
    def current_parameter_type(self) -> ProduceParameterType:
        if self.exam_mode.is_lesson:
            return self.exam_mode.parameter_type
        return self.turn_parameter_types[self.current_turn - 1]

    @property
    def current_bonus_permille(self) -> int:
        if self.exam_mode.is_lesson:
            return 1000
        if self.current_parameter_type is ProduceParameterType.VOCAL:
            return self.vocal_bonus_permille
        if self.current_parameter_type is ProduceParameterType.DANCE:
            return self.dance_bonus_permille
        return self.visual_bonus_permille

    def application_status(self, *, slump: bool) -> ParameterApplicationStatus:
        """Project the exact fields read by native ``AddParameter``."""

        if type(slump) is not bool:
            raise Plan2BattleScoringError("invalid-boolean", "slump")
        return ParameterApplicationStatus(
            judge_parameter=self.judge_parameter,
            limit_border=self.limit_border,
            clear_border=self.clear_border,
            current_turn_total_add_parameter=(
                self.current_turn_total_add_parameter
            ),
            slump=slump,
            is_battle=self.exam_mode.is_battle,
            current_parameter_type=self.current_parameter_type,
            battle_bonus_permille_vocal=self.vocal_bonus_permille,
            battle_bonus_permille_dance=self.dance_bonus_permille,
            battle_bonus_permille_visual=self.visual_bonus_permille,
            judge_parameter_vocal=self.judge_parameter_vocal,
            judge_parameter_dance=self.judge_parameter_dance,
            judge_parameter_visual=self.judge_parameter_visual,
        )


def _opaque_i32(
    opaque: Mapping[str, object],
    field: str,
    *,
    minimum: int = INT32_MIN,
) -> int:
    if field not in opaque:
        raise Plan2BattleScoringError("root-runtime-field-missing", field)
    return _i32(opaque[field], f"ExamSaveData.{field}", minimum=minimum)


def plan2_battle_scoring_context_from_exam_save(
    state: LocalSaveExamState,
) -> Plan2ExamSaveBattleScoringContext:
    """Bootstrap the central score application from one Plan2 ExamSaveData."""

    if not isinstance(state, LocalSaveExamState):
        raise TypeError("state must be LocalSaveExamState")
    try:
        exam_mode = resolve_plan2_exam_mode(
            state.exam_type,
            state.step_type_value,
        )
    except Plan2ExamModeError as error:
        raise Plan2BattleScoringError(error.code, error.detail) from error
    runtime = state.root_runtime
    if runtime is None:
        raise Plan2BattleScoringError("root-runtime-missing")
    raw_opaque = runtime.opaque_fields.to_value()
    if not isinstance(raw_opaque, Mapping):  # protected by the source type
        raise Plan2BattleScoringError("root-runtime-invalid")

    return Plan2ExamSaveBattleScoringContext(
        schema_version=PLAN2_EXAM_SAVE_BATTLE_SCORING_SCHEMA_VERSION,
        current_turn=state.current_turn,
        limit_turn=state.limit_turn,
        turn_parameter_types=tuple(state.turn_parameter_types),
        vocal_bonus_permille=state.vocal_bonus_permille,
        dance_bonus_permille=state.dance_bonus_permille,
        visual_bonus_permille=state.visual_bonus_permille,
        judge_parameter=state.score,
        exam_mode=exam_mode,
        limit_border=_opaque_i32(raw_opaque, "limitBorder"),
        clear_border=_opaque_i32(raw_opaque, "clearBorder"),
        current_turn_total_add_parameter=_opaque_i32(
            raw_opaque,
            "currentTurnTotalAddParameter",
            minimum=0,
        ),
        judge_parameter_vocal=_opaque_i32(
            raw_opaque, "parameterVocal", minimum=0
        ),
        judge_parameter_dance=_opaque_i32(
            raw_opaque, "parameterDance", minimum=0
        ),
        judge_parameter_visual=_opaque_i32(
            raw_opaque, "parameterVisual", minimum=0
        ),
        exam_end_complete=runtime.is_exam_end_complete,
        extra_turn=state.extra_turn,
    )


def plan2_exam_scoring_context_from_exam_save(
    state: LocalSaveExamState,
) -> Plan2ExamSaveBattleScoringContext:
    """Mode-neutral alias for new callers; the original API remains stable."""

    return plan2_battle_scoring_context_from_exam_save(state)


@dataclass(frozen=True, slots=True)
class Plan2CurrentTurnScoreRequest:
    raw_parameter: int
    score_kind: Plan2ScoreKind
    integration_point: Plan2ScoreIntegrationPoint
    slump: bool

    def __post_init__(self) -> None:
        _i32(self.raw_parameter, "raw_parameter", minimum=0)
        object.__setattr__(
            self,
            "score_kind",
            _enum(self.score_kind, Plan2ScoreKind, "score-kind"),
        )
        object.__setattr__(
            self,
            "integration_point",
            _enum(
                self.integration_point,
                Plan2ScoreIntegrationPoint,
                "integration-point",
            ),
        )
        if type(self.slump) is not bool:
            raise Plan2BattleScoringError("invalid-boolean", "slump")


@dataclass(frozen=True, slots=True)
class Plan2CurrentTurnScoreApplication:
    request: Plan2CurrentTurnScoreRequest
    parameter_type: ProduceParameterType
    bonus_permille: int
    application: ParameterApplication
    after: Plan2ExamSaveBattleScoringContext


def apply_plan2_current_turn_score(
    context: Plan2ExamSaveBattleScoringContext,
    request: Plan2CurrentTurnScoreRequest,
) -> Plan2CurrentTurnScoreApplication:
    """Apply one raw Lesson/Review score hit using the current schedule slot."""

    if not isinstance(context, Plan2ExamSaveBattleScoringContext):
        raise TypeError("context must be Plan2ExamSaveBattleScoringContext")
    if not isinstance(request, Plan2CurrentTurnScoreRequest):
        raise TypeError("request must be Plan2CurrentTurnScoreRequest")
    if context.exam_end_complete:
        raise Plan2BattleScoringError("exam-already-complete")

    parameter_type = context.current_parameter_type
    bonus_permille = context.current_bonus_permille
    application = apply_parameter_add(
        request.raw_parameter,
        status=context.application_status(slump=request.slump),
    )
    after = replace(
        context,
        judge_parameter=application.after,
        current_turn_total_add_parameter=(
            application.current_turn_total_add_parameter
        ),
        judge_parameter_vocal=application.judge_parameter_vocal,
        judge_parameter_dance=application.judge_parameter_dance,
        judge_parameter_visual=application.judge_parameter_visual,
    )
    return Plan2CurrentTurnScoreApplication(
        request=request,
        parameter_type=parameter_type,
        bonus_permille=bonus_permille,
        application=application,
        after=after,
    )


def apply_plan2_current_turn_lesson_score(
    context: Plan2ExamSaveBattleScoringContext,
    raw_parameter: int,
    *,
    slump: bool,
    integration_point: Plan2ScoreIntegrationPoint = (
        Plan2ScoreIntegrationPoint.SCALAR_DIRECT
    ),
) -> Plan2CurrentTurnScoreApplication:
    return apply_plan2_current_turn_score(
        context,
        Plan2CurrentTurnScoreRequest(
            raw_parameter=raw_parameter,
            score_kind=Plan2ScoreKind.LESSON,
            integration_point=integration_point,
            slump=slump,
        ),
    )


def apply_plan2_current_turn_review_score(
    context: Plan2ExamSaveBattleScoringContext,
    raw_parameter: int,
    *,
    slump: bool,
    integration_point: Plan2ScoreIntegrationPoint = (
        Plan2ScoreIntegrationPoint.REVIEW_DYNAMIC
    ),
) -> Plan2CurrentTurnScoreApplication:
    return apply_plan2_current_turn_score(
        context,
        Plan2CurrentTurnScoreRequest(
            raw_parameter=raw_parameter,
            score_kind=Plan2ScoreKind.REVIEW,
            integration_point=integration_point,
            slump=slump,
        ),
    )


def carry_plan2_battle_application_status(
    context: Plan2ExamSaveBattleScoringContext,
    status: ParameterApplicationStatus,
) -> Plan2ExamSaveBattleScoringContext:
    """Adopt one native leaf executor's final ``AddParameter`` status.

    Leaf executors already perform the exact per-hit loop.  This bridge keeps
    that execution authoritative while rejecting a status that was created
    from a different turn, battle configuration, or score snapshot.
    """

    if not isinstance(context, Plan2ExamSaveBattleScoringContext):
        raise TypeError("context must be Plan2ExamSaveBattleScoringContext")
    if not isinstance(status, ParameterApplicationStatus):
        raise TypeError("status must be ParameterApplicationStatus")
    expected = context.application_status(slump=status.slump)
    fixed_fields = (
        "limit_border",
        "clear_border",
        "is_battle",
        "current_parameter_type",
        "battle_bonus_permille_vocal",
        "battle_bonus_permille_dance",
        "battle_bonus_permille_visual",
    )
    for field in fixed_fields:
        if getattr(status, field) != getattr(expected, field):
            raise Plan2BattleScoringError(
                "application-status-context-mismatch", field
            )
    if status.judge_parameter < context.judge_parameter:
        raise Plan2BattleScoringError(
            "application-status-score-regressed",
            f"before={context.judge_parameter}; after={status.judge_parameter}",
        )
    if (
        status.current_turn_total_add_parameter
        < context.current_turn_total_add_parameter
    ):
        raise Plan2BattleScoringError(
            "application-status-turn-total-regressed"
        )
    return replace(
        context,
        judge_parameter=status.judge_parameter,
        current_turn_total_add_parameter=(
            status.current_turn_total_add_parameter
        ),
        judge_parameter_vocal=status.judge_parameter_vocal,
        judge_parameter_dance=status.judge_parameter_dance,
        judge_parameter_visual=status.judge_parameter_visual,
    )


@dataclass(frozen=True, slots=True)
class Plan2BattleTurnAdvance:
    boundary: CurrentTurnBoundary
    logged_turn: int
    logged_parameter_type: ProduceParameterType
    logged_add_parameter: int
    after_log_reset: Plan2ExamSaveBattleScoringContext
    after_boundary: Plan2ExamSaveBattleScoringContext


def close_plan2_exam_save_battle_turn(
    context: Plan2ExamSaveBattleScoringContext,
    boundary: CurrentTurnBoundary,
) -> Plan2BattleTurnAdvance:
    """Log/reset the current total, then advance or complete the exam."""

    if not isinstance(context, Plan2ExamSaveBattleScoringContext):
        raise TypeError("context must be Plan2ExamSaveBattleScoringContext")
    try:
        resolved_boundary = CurrentTurnBoundary(boundary)
    except (TypeError, ValueError) as error:
        raise Plan2BattleScoringError(
            "invalid-turn-boundary", repr(boundary)
        ) from error
    if context.exam_end_complete:
        raise Plan2BattleScoringError("exam-already-complete")
    if (
        resolved_boundary is CurrentTurnBoundary.NEXT_TURN
        and context.current_turn >= context.limit_turn + context.extra_turn
    ):
        raise Plan2BattleScoringError(
            "schedule-exhausted", str(context.current_turn)
        )

    native = close_current_turn(
        CurrentTurnState(
            current_turn=context.current_turn,
            current_turn_total_add_parameter=(
                context.current_turn_total_add_parameter
            ),
            exam_end_complete=context.exam_end_complete,
        ),
        resolved_boundary,
    )
    after_log_reset = replace(
        context,
        current_turn=native.after_log_reset.current_turn,
        current_turn_total_add_parameter=(
            native.after_log_reset.current_turn_total_add_parameter
        ),
        exam_end_complete=native.after_log_reset.exam_end_complete,
    )
    after_boundary = replace(
        context,
        current_turn=native.after_boundary.current_turn,
        current_turn_total_add_parameter=(
            native.after_boundary.current_turn_total_add_parameter
        ),
        exam_end_complete=native.after_boundary.exam_end_complete,
    )
    return Plan2BattleTurnAdvance(
        boundary=resolved_boundary,
        logged_turn=native.logged_turn,
        logged_parameter_type=context.current_parameter_type,
        logged_add_parameter=native.logged_add_parameter,
        after_log_reset=after_log_reset,
        after_boundary=after_boundary,
    )


__all__ = [
    "AUDITION_EXAM_TYPE",
    "LESSON_EXAM_TYPE",
    "PLAN2_EXAM_SAVE_BATTLE_SCORING_ADAPTER_ID",
    "PLAN2_EXAM_SAVE_BATTLE_SCORING_SCHEMA_VERSION",
    "PLAN2_SCORE_INTEGRATION_POINTS",
    "Plan2BattleScoringError",
    "Plan2BattleTurnAdvance",
    "Plan2CurrentTurnScoreApplication",
    "Plan2CurrentTurnScoreRequest",
    "Plan2ExamSaveBattleScoringContext",
    "Plan2ScoreIntegrationPoint",
    "Plan2ScoreKind",
    "apply_plan2_current_turn_lesson_score",
    "apply_plan2_current_turn_review_score",
    "apply_plan2_current_turn_score",
    "carry_plan2_battle_application_status",
    "close_plan2_exam_save_battle_turn",
    "plan2_battle_scoring_context_from_exam_save",
    "plan2_exam_scoring_context_from_exam_save",
]
