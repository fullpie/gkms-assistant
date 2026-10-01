"""Pure catalog/evaluator for the local ``StartPlay`` trigger boundary.

The Android v3.2.3 enum calls this phase
``ProduceExamPhaseType_StartPlay``.  ``ProduceExamPhaseType_ExamStartPlay``
is a useful description, but it is not an enum token in the local metadata.
This module keeps that distinction explicit and never aliases an unknown
phase into a runtime event.

Only one non-trivial field shape is independently resolved here:
``PreservationUp`` with no value payload (current stance type only), plus the
same field with one value in synthetic evaluator probes (current stance level
``>=`` that value).  All other Master rows remain in the catalog but are
fail-closed.  No engine, GUI, coverage, or outer-flow code is imported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence


# ---------------------------------------------------------------------------
# Canonical native tokens and the local event boundary

PHASE_START_PLAY = "ProduceExamPhaseType_StartPlay"
PHASE_EXAM_START_PLAY_REQUESTED = "ProduceExamPhaseType_ExamStartPlay"
# Compatibility name for callers that use the task's prose spelling.  The
# value is deliberately the real native token, not the non-existent token.
PHASE_EXAM_START_PLAY = PHASE_START_PLAY
PHASE_EXAM_START_TURN = "ProduceExamPhaseType_ExamStartTurn"
PHASE_START_EXAM_PLAY = "ProduceExamPhaseType_StartExamPlay"
PHASE_EXAM_TURN_TIMER = "ProduceExamPhaseType_ExamTurnTimer"
PHASE_EXAM_TURN_INTERVAL = "ProduceExamPhaseType_ExamTurnInterval"

PHASE_UNKNOWN_MOVE = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"

CHECK_NOT = "ProduceExamTriggerCheckType_Not"

FIELD_CARD_PLAY_AGGRESSIVE_UP = (
    "ProduceExamFieldStatusType_CardPlayAggressiveUp"
)
FIELD_CARD_SEARCH_COUNT_UP = "ProduceExamFieldStatusType_CardSearchCountUp"
FIELD_CONCENTRATION_UP = "ProduceExamFieldStatusType_ConcentrationUp"
FIELD_FULL_POWER_UP = "ProduceExamFieldStatusType_FullPowerUp"
FIELD_PARAMETER_BUFF = "ProduceExamFieldStatusType_ParameterBuff"
FIELD_PARAMETER_BUFF_UP = "ProduceExamFieldStatusType_ParameterBuffUp"
FIELD_PLAY_CARD_LESSON = "ProduceExamFieldStatusType_PlayCardLesson"
FIELD_PRESERVATION_UP = "ProduceExamFieldStatusType_PreservationUp"
FIELD_REMAINING_TURN = "ProduceExamFieldStatusType_RemainingTurn"
FIELD_STAMINA_CONSUMPTION_DOWN = (
    "ProduceExamFieldStatusType_StaminaConsumptionDown"
)
FIELD_STAMINA_LESS_MULTIPLE = "ProduceExamFieldStatusType_StaminaLessMultiple"
FIELD_STANCE_CHANGE_COUNT_UP = "ProduceExamFieldStatusType_StanceChangeCountUp"

STANCE_PRESERVATION = "preservation"

# This is the proven phase order for the start-of-turn trigger stream.  The
# ordinary draw is a separate command boundary; these are the trigger phase
# boundaries after it is settled.
START_TURN_TRIGGER_PHASE_ORDER: tuple[str, ...] = (
    PHASE_EXAM_START_TURN,
    PHASE_START_EXAM_PLAY,
    PHASE_START_PLAY,
    PHASE_EXAM_TURN_TIMER,
    PHASE_EXAM_TURN_INTERVAL,
)


class TriggerResolution(str, Enum):
    """Whether this independent catalog can evaluate the row."""

    INDEPENDENT_RESOLVED = "independent-resolved"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class StartPlayState:
    """Only the current status values used by the resolved predicate."""

    stance: str
    stance_level: int

    def __post_init__(self) -> None:
        if not isinstance(self.stance, str):
            raise TypeError("stance must be a string")
        if type(self.stance_level) is not int:
            raise TypeError("stance_level must be a plain integer")


@dataclass(frozen=True, slots=True)
class StartPlayEvent:
    """An explicit event boundary.

    ``stance_before``, ``stance_after`` and ``stance_delta`` are optional
    trace annotations.  The StartPlay predicate intentionally does not read
    them; native ``IsFieldStatusTriggerStatusEffect`` reads the current
    status type/step from the calculation context.
    """

    phase: str
    stance_before: str | None = None
    stance_after: str | None = None
    stance_delta: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, str):
            raise TypeError("phase must be a string")
        for name in ("stance_before", "stance_after"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string or None")
        if self.stance_delta is not None and type(self.stance_delta) is not int:
            raise TypeError("stance_delta must be a plain integer or None")


@dataclass(frozen=True, slots=True)
class ExamStartPlayTrigger:
    """Immutable copy of one ``produce_exam_trigger`` Master row."""

    id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...]
    field_status_check_types: tuple[str, ...]
    field_status_types: tuple[str, ...]
    field_status_values: tuple[int, ...]
    field_status_produce_card_search_ids: tuple[str, ...]
    produce_card_search_id: str
    upper_search_count: int
    lower_search_count: int
    card_move_position_type: str
    effect_types: tuple[str, ...]
    lesson_type: str
    resolution: TriggerResolution = field(init=False)
    unresolved_reasons: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("trigger id must be a non-empty string")

        for name in (
            "phase_types",
            "phase_values",
            "field_status_check_types",
            "field_status_types",
            "field_status_values",
            "field_status_produce_card_search_ids",
            "effect_types",
        ):
            value = getattr(self, name)
            if isinstance(value, (str, bytes)):
                raise TypeError(f"{name} must be a sequence, not text")
            try:
                value = tuple(value)
            except TypeError as exc:
                raise TypeError(f"{name} must be a sequence") from exc
            object.__setattr__(self, name, value)

        for name in ("phase_values", "field_status_values"):
            if any(type(item) is not int for item in getattr(self, name)):
                raise TypeError(f"{name} must contain plain integers")
        for name in (
            "phase_types",
            "field_status_check_types",
            "field_status_types",
            "field_status_produce_card_search_ids",
            "effect_types",
        ):
            if any(not isinstance(item, str) for item in getattr(self, name)):
                raise TypeError(f"{name} must contain strings")
        for name in (
            "produce_card_search_id",
            "card_move_position_type",
            "lesson_type",
        ):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        for name in ("upper_search_count", "lower_search_count"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be a plain integer")

        reasons = _unsupported_shape_reasons(self)
        object.__setattr__(
            self,
            "resolution",
            (
                TriggerResolution.INDEPENDENT_RESOLVED
                if not reasons
                else TriggerResolution.UNRESOLVED
            ),
        )
        object.__setattr__(self, "unresolved_reasons", reasons)

    @property
    def phase(self) -> str:
        return self.phase_types[0] if len(self.phase_types) == 1 else ",".join(self.phase_types)

    @property
    def field_status_type(self) -> str | None:
        return self.field_status_types[0] if len(self.field_status_types) == 1 else None

    @property
    def value1(self) -> int | None:
        """The single field-status threshold, when the row has one."""

        return self.field_status_values[0] if len(self.field_status_values) == 1 else None

    @property
    def value2(self) -> int | None:
        """There is no second field-status value in this Master contract."""

        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase_types": list(self.phase_types),
            "phase_values": list(self.phase_values),
            "field_status_check_types": list(self.field_status_check_types),
            "field_status_types": list(self.field_status_types),
            "field_status_values": list(self.field_status_values),
            "field_status_produce_card_search_ids": list(
                self.field_status_produce_card_search_ids
            ),
            "produce_card_search_id": self.produce_card_search_id,
            "upper_search_count": self.upper_search_count,
            "lower_search_count": self.lower_search_count,
            "card_move_position_type": self.card_move_position_type,
            "effect_types": list(self.effect_types),
            "lesson_type": self.lesson_type,
            "resolution": self.resolution.value,
            "unresolved_reasons": list(self.unresolved_reasons),
        }


def _unsupported_shape_reasons(
    trigger: ExamStartPlayTrigger,
) -> tuple[str, ...]:
    """Classify only the exact typed boundary this module can prove."""

    reasons: list[str] = []
    if trigger.phase_types != (PHASE_START_PLAY,):
        reasons.append("phase-is-not-canonical-start-play")
    if trigger.phase_values:
        reasons.append("phase-values-unsupported")
    if trigger.field_status_check_types not in ((), (CHECK_NOT,)):
        reasons.append("unknown-or-multiple-field-status-check")
    if len(trigger.field_status_types) > 1:
        reasons.append("multiple-field-status-types")
    if len(trigger.field_status_values) > 1:
        reasons.append("multiple-field-status-values")
    if trigger.field_status_produce_card_search_ids:
        reasons.append("field-status-card-search-unsupported")
    if trigger.produce_card_search_id:
        reasons.append("card-search-unsupported")
    if trigger.upper_search_count != 0 or trigger.lower_search_count != 0:
        reasons.append("search-count-unsupported")
    if trigger.card_move_position_type != PHASE_UNKNOWN_MOVE:
        reasons.append("card-move-filter-unsupported")
    if trigger.effect_types:
        reasons.append("effect-type-filter-unsupported")
    if trigger.lesson_type != LESSON_UNKNOWN:
        reasons.append("lesson-filter-not-proven-in-this-boundary")

    if not trigger.field_status_types and trigger.field_status_values:
        reasons.append("field-status-value-without-field-type")
    if trigger.field_status_types and not trigger.field_status_values:
        pass  # type-only is the important target shape

    if trigger.field_status_type not in (None, FIELD_PRESERVATION_UP):
        reasons.append("field-status-type-not-proven")
    if not trigger.field_status_types and trigger.field_status_check_types:
        reasons.append("not-without-a-field-status-predicate")

    return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True, slots=True)
class StartPlayEvaluation:
    """Pure predicate result; ``None`` means the shape is unresolved."""

    trigger_id: str
    event_phase: str
    resolution: TriggerResolution
    fires: bool | None
    reasons: tuple[str, ...] = ()
    state_unchanged: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolution", TriggerResolution(self.resolution))
        object.__setattr__(self, "reasons", tuple(self.reasons))


def _state_predicate(trigger: ExamStartPlayTrigger, state: StartPlayState) -> bool:
    field_type = trigger.field_status_type
    if field_type is None:
        result = True
    else:
        result = state.stance == STANCE_PRESERVATION
        if trigger.value1 is not None:
            result = result and state.stance_level >= trigger.value1
    if trigger.field_status_check_types == (CHECK_NOT,):
        return not result
    return result


def evaluate(
    trigger: ExamStartPlayTrigger | object,
    state: StartPlayState | object,
    event: StartPlayEvent | None = None,
    *,
    event_phase: str | None = None,
) -> StartPlayEvaluation:
    """Evaluate one row without mutating the supplied event or state.

    A wrong event phase is a valid, resolved non-fire.  An unknown row shape,
    non-canonical phase token, or non-typed input returns ``fires=None``.
    """

    if event is not None and event_phase is not None and event.phase != event_phase:
        return StartPlayEvaluation(
            trigger_id=getattr(trigger, "id", "<unknown>"),
            event_phase=event.phase,
            resolution=TriggerResolution.UNRESOLVED,
            fires=None,
            reasons=("conflicting-event-phase-inputs",),
        )

    if event is None:
        event = StartPlayEvent(event_phase or PHASE_START_PLAY)
    if not isinstance(trigger, ExamStartPlayTrigger):
        return StartPlayEvaluation(
            trigger_id=getattr(trigger, "id", "<unknown>"),
            event_phase=event.phase,
            resolution=TriggerResolution.UNRESOLVED,
            fires=None,
            reasons=("unknown-trigger-shape",),
        )
    if not isinstance(state, StartPlayState):
        return StartPlayEvaluation(
            trigger_id=trigger.id,
            event_phase=event.phase,
            resolution=TriggerResolution.UNRESOLVED,
            fires=None,
            reasons=("unknown-state-shape",),
        )
    if trigger.resolution is TriggerResolution.UNRESOLVED:
        return StartPlayEvaluation(
            trigger_id=trigger.id,
            event_phase=event.phase,
            resolution=trigger.resolution,
            fires=None,
            reasons=trigger.unresolved_reasons,
        )
    if event.phase != PHASE_START_PLAY:
        return StartPlayEvaluation(
            trigger_id=trigger.id,
            event_phase=event.phase,
            resolution=trigger.resolution,
            fires=False,
            reasons=("event-phase-mismatch",),
        )

    return StartPlayEvaluation(
        trigger_id=trigger.id,
        event_phase=event.phase,
        resolution=trigger.resolution,
        fires=_state_predicate(trigger, state),
    )


def fires(
    trigger: ExamStartPlayTrigger | object,
    state: StartPlayState | object,
    event: StartPlayEvent | None = None,
    *,
    event_phase: str | None = None,
) -> bool | None:
    """Convenience wrapper returning only the tri-state fire result."""

    return evaluate(trigger, state, event, event_phase=event_phase).fires


def event_index(phase: str) -> int | None:
    """Return the native relative order for a known start-turn phase."""

    try:
        return START_TURN_TRIGGER_PHASE_ORDER.index(phase)
    except ValueError:
        return None


def is_before(first_phase: str, second_phase: str) -> bool:
    """Compare two known native trigger phases, fail-closed for unknowns."""

    first = event_index(first_phase)
    second = event_index(second_phase)
    return first is not None and second is not None and first < second


def resolve_trigger(trigger_id: str) -> ExamStartPlayTrigger | None:
    """Return the immutable catalog row for one known ID."""

    return START_PLAY_TRIGGER_BY_ID.get(trigger_id)


# ---------------------------------------------------------------------------
# Native listener budget and ordered effect helpers

UNLIMITED_LIMIT = -1


def normalize_native_limit(value: int) -> int:
    """Mirror ExamStatusEnchant's ``0 -> -1`` finite-limit normalization."""

    if type(value) is not int:
        raise TypeError("native limit must be a plain integer")
    return value if value > 0 else UNLIMITED_LIMIT


@dataclass(frozen=True, slots=True)
class ListenerBudget:
    """The total/per-turn limits held by one TriggerEffectStatusEffect."""

    total_remaining: int
    per_turn_limit: int = UNLIMITED_LIMIT
    per_turn_remaining: int = UNLIMITED_LIMIT

    def __post_init__(self) -> None:
        for name in (
            "total_remaining",
            "per_turn_limit",
            "per_turn_remaining",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < UNLIMITED_LIMIT:
                raise ValueError(f"{name} must be -1 or a non-negative integer")

    @classmethod
    def from_master(cls, *, effect_count: int, effect_value1: int) -> "ListenerBudget":
        per_turn = normalize_native_limit(effect_value1)
        return cls(
            total_remaining=normalize_native_limit(effect_count),
            per_turn_limit=per_turn,
            per_turn_remaining=per_turn,
        )

    @property
    def can_fire(self) -> bool:
        return self.total_remaining != 0 and self.per_turn_remaining != 0


@dataclass(frozen=True, slots=True)
class ListenerConsumption:
    before: ListenerBudget
    after: ListenerBudget
    fired: bool
    removed: bool


def consume_listener(
    budget: ListenerBudget,
    *,
    predicate_matched: bool,
) -> ListenerConsumption:
    """Spend one matched queued listener candidate, purely.

    Native ``SpendCount`` runs while the trigger command is being built.  A
    finite total reaching zero is then eligible for
    ``RemoveCountLimitTriggeredStatus``; a per-turn zero is retained until
    the next turn reset.
    """

    if not predicate_matched or not budget.can_fire:
        return ListenerConsumption(
            before=budget,
            after=budget,
            fired=False,
            removed=budget.total_remaining == 0,
        )

    total = (
        budget.total_remaining - 1
        if budget.total_remaining > 0
        else budget.total_remaining
    )
    per_turn_remaining = (
        budget.per_turn_remaining - 1
        if budget.per_turn_remaining > 0
        else budget.per_turn_remaining
    )
    after = ListenerBudget(
        total_remaining=total,
        per_turn_limit=budget.per_turn_limit,
        per_turn_remaining=per_turn_remaining,
    )
    return ListenerConsumption(
        before=budget,
        after=after,
        fired=True,
        removed=after.total_remaining == 0,
    )


def reset_listener_turn(budget: ListenerBudget) -> ListenerBudget:
    """Reset only the per-turn remaining counter at a native turn boundary."""

    return ListenerBudget(
        total_remaining=budget.total_remaining,
        per_turn_limit=budget.per_turn_limit,
        per_turn_remaining=budget.per_turn_limit,
    )


def ordered_effect_ids(effect_ids: Sequence[str]) -> tuple[str, ...]:
    """Retain the Master effect-list order; never sort or deduplicate it."""

    if isinstance(effect_ids, (str, bytes)):
        raise TypeError("effect_ids must be a sequence of IDs")
    result = tuple(effect_ids)
    if any(not isinstance(effect_id, str) for effect_id in result):
        raise TypeError("effect_ids must contain strings")
    return result


def effect_group_execution_order(effect_group_ids: Sequence[str]) -> tuple[str, ...]:
    """Return the native stable effect-group sequence as an immutable tuple."""

    return ordered_effect_ids(effect_group_ids)


# Target effect payload facts are kept here so trigger and listener tests do
# not confuse field-status thresholds with effect payload values.
EFFECT_EXAM_OVER_PRESERVATION = "e_effect-exam_over_preservation"
EFFECT_TYPE_EXAM_OVER_PRESERVATION = "ProduceExamEffectType_ExamOverPreservation"
TARGET_EFFECT_VALUE1 = 0
TARGET_EFFECT_VALUE2 = 0
TARGET_EFFECT_COUNT = 0
TARGET_EFFECT_TURN = 0
TARGET_STATUS_ENCHANT_EFFECT_COUNT = 0
TARGET_STATUS_ENCHANT_EFFECT_VALUE1 = 0
TARGET_STATUS_ENCHANT_EFFECT_VALUE2 = 0
TARGET_STATUS_ENCHANT_EFFECT_TURN = -1
TARGET_STATUS_ENCHANT_IDS: tuple[str, ...] = (
    "enchant-p_card-03-ido-1_054-enc01",
    "enchant-p_card-03-ido-2_084-enc01",
    "enchant-p_card-03-ido-3_077-enc01",
)
TARGET_STATUS_ENCHANT_EFFECT_IDS: tuple[str, ...] = (
    "e_effect-exam_status_enchant-inf-enchant-p_card-03-ido-1_054-enc01",
    "e_effect-exam_status_enchant-inf-enchant-p_card-03-ido-2_084-enc01",
    "e_effect-exam_status_enchant-inf-enchant-p_card-03-ido-3_077-enc01",
)
TARGET_EFFECT_GROUP_IDS: tuple[str, ...] = (
    "effect_group-visible-exam_status_enchant-000",
    "effect_group-visible-exam_preservation-000",
)


# ---------------------------------------------------------------------------
# Exact local StartPlay Master catalog (23 rows)


def _row(
    trigger_id: str,
    *,
    field_type: str | None = None,
    value: int | None = None,
    field_card_search_id: str | None = None,
    lesson_type: str = LESSON_UNKNOWN,
) -> ExamStartPlayTrigger:
    return ExamStartPlayTrigger(
        id=trigger_id,
        phase_types=(PHASE_START_PLAY,),
        phase_values=(),
        field_status_check_types=(),
        field_status_types=() if field_type is None else (field_type,),
        field_status_values=() if value is None else (value,),
        field_status_produce_card_search_ids=(
            () if field_card_search_id is None else (field_card_search_id,)
        ),
        produce_card_search_id="",
        upper_search_count=0,
        lower_search_count=0,
        card_move_position_type=PHASE_UNKNOWN_MOVE,
        effect_types=(),
        lesson_type=lesson_type,
    )


ALL_EXAM_START_PLAY_TRIGGERS: tuple[ExamStartPlayTrigger, ...] = (
    _row("e_trigger-start_play"),
    _row(
        "e_trigger-start_play-card_play_aggressive_up-5",
        field_type=FIELD_CARD_PLAY_AGGRESSIVE_UP,
        value=5,
    ),
    _row(
        "e_trigger-start_play-card_search_count_up-1-p_card_search-r-hand",
        field_type=FIELD_CARD_SEARCH_COUNT_UP,
        value=1,
        field_card_search_id="p_card_search-r-hand",
    ),
    _row(
        "e_trigger-start_play-card_search_count_up-12-p_card_search-deck_grave",
        field_type=FIELD_CARD_SEARCH_COUNT_UP,
        value=12,
        field_card_search_id="p_card_search-deck_grave",
    ),
    _row(
        "e_trigger-start_play-card_search_count_up-18-p_card_search-deck_grave",
        field_type=FIELD_CARD_SEARCH_COUNT_UP,
        value=18,
        field_card_search_id="p_card_search-deck_grave",
    ),
    _row(
        "e_trigger-start_play-concentration_up",
        field_type=FIELD_CONCENTRATION_UP,
    ),
    _row(
        "e_trigger-start_play-concentration_up-lesson_dance",
        field_type=FIELD_CONCENTRATION_UP,
        lesson_type="ProduceStepLessonType_LessonDance",
    ),
    _row("e_trigger-start_play-full_power_up", field_type=FIELD_FULL_POWER_UP),
    _row(
        "e_trigger-start_play-lesson_dance",
        lesson_type="ProduceStepLessonType_LessonDance",
    ),
    _row(
        "e_trigger-start_play-lesson_visual",
        lesson_type="ProduceStepLessonType_LessonVisual",
    ),
    _row(
        "e_trigger-start_play-lesson_vocal",
        lesson_type="ProduceStepLessonType_LessonVocal",
    ),
    _row("e_trigger-start_play-p_card-01-men-100_005"),
    _row("e_trigger-start_play-parameter_buff", field_type=FIELD_PARAMETER_BUFF),
    _row(
        "e_trigger-start_play-parameter_buff_up-10-lesson_visual",
        field_type=FIELD_PARAMETER_BUFF_UP,
        value=10,
        lesson_type="ProduceStepLessonType_LessonVisual",
    ),
    _row("e_trigger-start_play-play_card_lesson", field_type=FIELD_PLAY_CARD_LESSON),
    _row("e_trigger-start_play-preservation_up", field_type=FIELD_PRESERVATION_UP),
    _row(
        "e_trigger-start_play-preservation_up-lesson_visual",
        field_type=FIELD_PRESERVATION_UP,
        lesson_type="ProduceStepLessonType_LessonVisual",
    ),
    _row(
        "e_trigger-start_play-remaining_turn-2",
        field_type=FIELD_REMAINING_TURN,
        value=2,
    ),
    _row(
        "e_trigger-start_play-remaining_turn-3",
        field_type=FIELD_REMAINING_TURN,
        value=3,
    ),
    _row(
        "e_trigger-start_play-stamina_consumption_down",
        field_type=FIELD_STAMINA_CONSUMPTION_DOWN,
    ),
    _row(
        "e_trigger-start_play-stamina_less_multiple-250",
        field_type=FIELD_STAMINA_LESS_MULTIPLE,
        value=250,
    ),
    _row(
        "e_trigger-start_play-stamina_less_multiple-500",
        field_type=FIELD_STAMINA_LESS_MULTIPLE,
        value=500,
    ),
    _row(
        "e_trigger-start_play-stance_change_count_up-4",
        field_type=FIELD_STANCE_CHANGE_COUNT_UP,
        value=4,
    ),
)

if len(ALL_EXAM_START_PLAY_TRIGGERS) != 23:  # pragma: no cover - static guard
    raise RuntimeError("the local StartPlay row inventory must contain 23 rows")

START_PLAY_MASTER_ROW_COUNT = len(ALL_EXAM_START_PLAY_TRIGGERS)
START_PLAY_TRIGGER_BY_ID: Mapping[str, ExamStartPlayTrigger] = MappingProxyType(
    {trigger.id: trigger for trigger in ALL_EXAM_START_PLAY_TRIGGERS}
)

# Short aliases mirror the neighboring start-turn inventory naming.
START_PLAY_TRIGGERS = ALL_EXAM_START_PLAY_TRIGGERS
EXAM_START_PLAY_TRIGGER_BY_ID = START_PLAY_TRIGGER_BY_ID
StartPlayTrigger = ExamStartPlayTrigger


__all__ = [
    "ALL_EXAM_START_PLAY_TRIGGERS",
    "CHECK_NOT",
    "EFFECT_EXAM_OVER_PRESERVATION",
    "EFFECT_TYPE_EXAM_OVER_PRESERVATION",
    "EXAM_START_PLAY_TRIGGER_BY_ID",
    "ExamStartPlayTrigger",
    "FIELD_PRESERVATION_UP",
    "LESSON_UNKNOWN",
    "ListenerBudget",
    "ListenerConsumption",
    "PHASE_EXAM_START_PLAY",
    "PHASE_EXAM_START_PLAY_REQUESTED",
    "PHASE_EXAM_START_TURN",
    "PHASE_EXAM_TURN_INTERVAL",
    "PHASE_EXAM_TURN_TIMER",
    "PHASE_START_EXAM_PLAY",
    "PHASE_START_PLAY",
    "START_PLAY_MASTER_ROW_COUNT",
    "START_PLAY_TRIGGER_BY_ID",
    "START_TURN_TRIGGER_PHASE_ORDER",
    "STANCE_PRESERVATION",
    "StartPlayEvent",
    "StartPlayEvaluation",
    "StartPlayState",
    "TARGET_EFFECT_GROUP_IDS",
    "TARGET_EFFECT_VALUE1",
    "TARGET_EFFECT_VALUE2",
    "TARGET_STATUS_ENCHANT_EFFECT_COUNT",
    "TARGET_STATUS_ENCHANT_EFFECT_IDS",
    "TARGET_STATUS_ENCHANT_EFFECT_TURN",
    "TARGET_STATUS_ENCHANT_EFFECT_VALUE1",
    "TARGET_STATUS_ENCHANT_EFFECT_VALUE2",
    "TARGET_STATUS_ENCHANT_IDS",
    "TriggerResolution",
    "consume_listener",
    "effect_group_execution_order",
    "event_index",
    "evaluate",
    "fires",
    "is_before",
    "normalize_native_limit",
    "ordered_effect_ids",
    "reset_listener_turn",
    "resolve_trigger",
]
