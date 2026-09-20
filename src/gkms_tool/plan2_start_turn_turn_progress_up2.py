"""Bounded Plan2/Common card gate for ``TurnProgressUp(2)``.

This module owns one card-level predicate only:
``e_trigger-exam_start_turn-turn_progress_up-2`` on
``p_card-00-sup-2_028`` (``ストレッチ談議``), upgrades 0 through 3.

The Master row and the card/effect slice are read-only catalog evidence.  The
native scalar predicate is delegated to the exact StartTurn resolver in
``plan3_start_turn_trigger``; no central coverage/runtime is rebuilt here.
The direct gate is evaluated at the settled StartTurn snapshot while the
direct PlayEffect batch is built, before card payment or any effect command.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from .master_db import DEFAULT_DATABASE
from .plan2_aggressive_card_trigger import EvaluationBoundary
from .plan2_turn_progress_trigger import (
    TurnProgressTriggerRow,
    load_plan2_turn_progress_trigger_catalog,
)
from .plan3_start_turn_trigger import (
    EXAM_START_TURN_TRIGGER_BY_ID,
    FIELD_TURN_PROGRESS_UP,
    PHASE_EXAM_START_TURN,
    TRIGGER_TURN_PROGRESS_UP,
    TriggerResolution as NativeTriggerResolution,
    fires as fires_native_start_turn,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDIT_PATH = (
    PROJECT_ROOT
    / "var"
    / "coverage"
    / "plan2_start_turn_turn_progress_up2_native_audit.json"
)

TARGET_TRIGGER_ID: Final = TRIGGER_TURN_PROGRESS_UP
START_TURN_TRIGGER_ID: Final = TARGET_TRIGGER_ID
TARGET_CARD_ID: Final = "p_card-00-sup-2_028"
TARGET_CARD_NAME: Final = "ストレッチ談議"
TARGET_UPGRADES: Final = (0, 1, 2, 3)
TARGET_THRESHOLD: Final = 2
TRIGGER_THRESHOLD: Final = TARGET_THRESHOLD

COMMON_PLAN_TYPE: Final = "ProducePlanType_Common"
PLAN_COMMON: Final = COMMON_PLAN_TYPE
ACTIVE_SKILL_CATEGORY: Final = "ProduceCardCategory_ActiveSkill"
CARD_MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN: Final = "ProduceStepLessonType_Unknown"
CHECK_NOT: Final = "ProduceExamTriggerCheckType_Not"
DIRECT_DISPATCH_PHASE: Final = "PlayEffect"

INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1
_I32_MIN = INT32_MIN
_I32_MAX = INT32_MAX

_TARGET_NAMES_BY_UPGRADE: Final = {
    0: TARGET_CARD_NAME,
    1: f"{TARGET_CARD_NAME}+",
    2: f"{TARGET_CARD_NAME}++",
    3: f"{TARGET_CARD_NAME}+++",
}
_TARGET_STAMINA_BY_UPGRADE: Final = {0: 3, 1: 0, 2: 0, 3: 0}
_TARGET_LESSON_BY_UPGRADE: Final = {0: 3, 1: 3, 2: 6, 3: 9}
_TARGET_EFFECT_TIMER_ID: Final = (
    "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_upgrade-"
    "p_card_search-hand-all-0_0"
)
_TARGET_EFFECT_STAMINA_ID: Final = "e_effect-exam_stamina_consumption_down-02"
TARGET_EFFECT_IDS_BY_UPGRADE: Final = {
    upgrade: (
        f"e_effect-exam_lesson-{lesson:04d}-01",
        _TARGET_EFFECT_STAMINA_ID,
        _TARGET_EFFECT_TIMER_ID,
    )
    for upgrade, lesson in _TARGET_LESSON_BY_UPGRADE.items()
}
TARGET_EFFECT_IDS: Final = tuple(
    sorted(
        {
            effect_id
            for effect_ids in TARGET_EFFECT_IDS_BY_UPGRADE.values()
            for effect_id in effect_ids
        }
    )
)
TARGET_EFFECT_TYPES_BY_ID: Final = {
    effect_id: (
        "ProduceExamEffectType_ExamLesson"
        if effect_id.startswith("e_effect-exam_lesson-")
        else (
            "ProduceExamEffectType_ExamStaminaConsumptionDown"
            if effect_id == _TARGET_EFFECT_STAMINA_ID
            else "ProduceExamEffectType_ExamEffectTimer"
        )
    )
    for effect_id in TARGET_EFFECT_IDS
}
TARGET_EFFECT_CHAIN_ID: Final = "e_effect-exam_card_upgrade-p_card_search-hand-all-0_0"


class TurnProgressUp2CatalogError(ValueError):
    """The local Master slice is missing or outside the exact target shape."""


class EvaluationStage(str, Enum):
    """Transaction stage represented by a card-gate evaluation."""

    PRE_CARD_COST = "pre-card-cost"
    POST_CARD_COST = "post-card-cost"
    POST_DIRECT_EFFECTS = "post-direct-effects"
    POST_CARD_MOVE = "post-card-move"
    POST_TIMER_INSTALL = "post-timer-install"
    POST_PLAY_COUNT = "post-play-count"


class StartTurnSnapshot(str, Enum):
    """Native lifecycle boundary for the CurrentTurn read."""

    SETTLED_POST_DRAW = "settled-start-turn-post-draw"
    PRE_DRAW = "pre-draw"
    PRE_SETTLEMENT = "pre-start-turn-settlement"


class ExecutionMode(str, Enum):
    """Only the three native card command origins covered by the audit."""

    ORDINARY = "ordinary"
    FORCED = "forced"
    EXTRA = "extra"


# Neighbor modules use both vocabularies.  Keep one enum and expose the alias.
CardExecutionKind = ExecutionMode


@dataclass(frozen=True, slots=True)
class TurnProgressUp2Contract:
    """Native contract for the exact Master row."""

    row: TurnProgressTriggerRow
    threshold: int
    comparison: str = "current_turn > threshold"
    value_source: str = "ExamParameterModel.CurrentTurn"
    threshold_source: str = "produce_exam_trigger.fieldStatusValues[0]"
    value2_source: str = "native signed Int32 value argument"
    threshold_type: str = "signed Int32"
    threshold_bounds_inclusive: tuple[int, int] = (INT32_MIN, INT32_MAX)
    threshold_signed: bool = True
    equality_fires: bool = False
    initial_current_turn: str = (
        "0 before the first active turn; 1-based once StartTurn is active"
    )
    snapshot: str = StartTurnSnapshot.SETTLED_POST_DRAW.value
    phase_selector: str = "literal ProduceExamPhaseType_ExamStartTurn"
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    evaluation_boundary: str = EvaluationBoundary.PRE_PAYMENT_BUILD.value
    listener_kind: str = "card-owned direct play gate"

    @property
    def result_expression(self) -> str:
        return f"current_turn > {self.threshold}"

    @property
    def value2(self) -> int:
        """The native comparison's second signed operand (Master value [0])."""

        return self.threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggerId": self.row.id,
            "threshold": self.threshold,
            "value2": self.value2,
            "comparison": self.comparison,
            "resultExpression": self.result_expression,
            "valueSource": self.value_source,
            "thresholdSource": self.threshold_source,
            "value2Source": self.value2_source,
            "thresholdType": self.threshold_type,
            "thresholdBoundsInclusive": list(self.threshold_bounds_inclusive),
            "thresholdSigned": self.threshold_signed,
            "equalityFires": self.equality_fires,
            "initialCurrentTurn": self.initial_current_turn,
            "snapshot": self.snapshot,
            "phaseSelector": self.phase_selector,
            "dispatchPhase": self.dispatch_phase,
            "evaluationBoundary": self.evaluation_boundary,
            "listenerKind": self.listener_kind,
        }


@dataclass(frozen=True, slots=True)
class ContractResolution:
    supported: bool
    contract: TurnProgressUp2Contract | None
    reasons: tuple[str, ...] = ()


def _tuple_is(value: object, item_type: type) -> bool:
    return isinstance(value, tuple) and all(type(item) is item_type for item in value)


def _shape_reasons(row: object) -> tuple[str, ...]:
    if not isinstance(row, TurnProgressTriggerRow):
        return ("trigger-record-type-is-unknown",)
    reasons: list[str] = []
    if row.id != TARGET_TRIGGER_ID:
        reasons.append("trigger-id-is-not-target")
    if not _tuple_is(row.phase_types, str) or row.phase_types != (
        PHASE_EXAM_START_TURN,
    ):
        reasons.append("phase-shape-is-not-exam-start-turn")
    if not _tuple_is(row.phase_values, int) or row.phase_values:
        reasons.append("phase-values-must-be-empty")
    if not _tuple_is(row.field_status_check_types, str) or row.field_status_check_types:
        reasons.append("field-status-check-types-must-be-empty")
    if not _tuple_is(row.field_status_types, str) or row.field_status_types != (
        FIELD_TURN_PROGRESS_UP,
    ):
        reasons.append("field-status-type-is-not-turn-progress-up")
    if not _tuple_is(row.field_status_values, int) or len(row.field_status_values) != 1:
        reasons.append("one-signed-value2-is-required")
    else:
        value2 = row.field_status_values[0]
        if not _I32_MIN <= value2 <= _I32_MAX:
            reasons.append("value2-is-outside-signed-int32")
        if value2 != TARGET_THRESHOLD:
            reasons.append("value2-does-not-match-master-target")
    if not _tuple_is(row.field_status_card_search_ids, str) or row.field_status_card_search_ids:
        reasons.append("field-card-search-ids-must-be-empty")
    if type(row.produce_card_search_id) is not str or row.produce_card_search_id:
        reasons.append("produce-card-search-id-must-be-empty")
    if type(row.upper_search_count) is not int or row.upper_search_count != 0:
        reasons.append("upper-search-count-must-be-zero")
    if type(row.lower_search_count) is not int or row.lower_search_count != 0:
        reasons.append("lower-search-count-must-be-zero")
    if type(row.card_move_position_type) is not str or row.card_move_position_type != MOVE_UNKNOWN:
        reasons.append("card-move-condition-must-be-unknown")
    if not _tuple_is(row.effect_types, str) or row.effect_types:
        reasons.append("effect-types-must-be-empty")
    if type(row.lesson_type) is not str or row.lesson_type != LESSON_UNKNOWN:
        reasons.append("lesson-condition-must-be-unknown")
    return tuple(reasons)


def resolve_turn_progress_up2_trigger(row: object) -> ContractResolution:
    """Resolve only the complete StartTurn row; all deviations stop."""

    reasons = _shape_reasons(row)
    if reasons:
        return ContractResolution(False, None, reasons)
    assert isinstance(row, TurnProgressTriggerRow)
    return ContractResolution(
        True,
        TurnProgressUp2Contract(row=row, threshold=TARGET_THRESHOLD),
    )


@dataclass(frozen=True, slots=True)
class TurnProgressUp2EvaluationInput:
    """Immutable state observed at the direct card gate.

    ``current_turn`` is the only predicate input.  Remaining-turn, extra-turn,
    and accumulated-progress values are retained solely as provenance fields;
    native TurnProgressUp does not read them.
    """

    current_turn: int
    remaining_turn: int | None = None
    remain_turn: int | None = None
    turns_remaining: int | None = None
    extra_turn: int | None = None
    accumulated_progress: int | None = None
    turn_progress: int | None = None
    phase: str = PHASE_EXAM_START_TURN
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    boundary: EvaluationBoundary = EvaluationBoundary.PRE_PAYMENT_BUILD
    stage: EvaluationStage = EvaluationStage.PRE_CARD_COST
    snapshot: StartTurnSnapshot = StartTurnSnapshot.SETTLED_POST_DRAW
    execution_mode: ExecutionMode = ExecutionMode.ORDINARY
    repeat_index: int = 0
    start_turn_settled: bool = True
    full_power_settled: bool = True
    gimmick_settled: bool = True
    draw_settled: bool = True
    card_cost_paid: bool = False
    direct_effects_applied: bool = False
    timer_installed: bool = False
    move_applied: bool = False
    card_move_applied: bool = False
    play_count_incremented: bool = False

    def __post_init__(self) -> None:
        if type(self.current_turn) is not int or not _I32_MIN <= self.current_turn <= _I32_MAX:
            raise TypeError("current_turn must be a signed Int32")
        for name in (
            "remaining_turn",
            "remain_turn",
            "turns_remaining",
            "extra_turn",
            "accumulated_progress",
            "turn_progress",
        ):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not int or not _I32_MIN <= value <= _I32_MAX
            ):
                raise TypeError(f"{name} must be a signed Int32 or None")
        if type(self.repeat_index) is not int or self.repeat_index < 0:
            raise ValueError("repeat_index must be a non-negative integer")
        if type(self.phase) is not str or not self.phase:
            raise TypeError("phase must be a non-empty string")
        if type(self.dispatch_phase) is not str or not self.dispatch_phase:
            raise TypeError("dispatch_phase must be a non-empty string")
        for name, enum_type in (
            ("boundary", EvaluationBoundary),
            ("stage", EvaluationStage),
            ("snapshot", StartTurnSnapshot),
            ("execution_mode", ExecutionMode),
        ):
            value = getattr(self, name)
            if not isinstance(value, enum_type):
                object.__setattr__(self, name, enum_type(value))
        for name in (
            "start_turn_settled",
            "full_power_settled",
            "gimmick_settled",
            "draw_settled",
            "card_cost_paid",
            "direct_effects_applied",
            "timer_installed",
            "move_applied",
            "card_move_applied",
            "play_count_incremented",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")


@dataclass(frozen=True, slots=True)
class TurnProgressUp2Evaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    current_turn: int | None
    threshold: int | None
    comparison: str | None
    phase: str
    dispatch_phase: str
    boundary: EvaluationBoundary
    stage: EvaluationStage
    snapshot: StartTurnSnapshot
    execution_mode: ExecutionMode
    remaining_turn: int | None = None
    extra_turn: int | None = None
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    @property
    def resolved(self) -> bool:
        return self.fires is not None

    @property
    def allows_card_invocation(self) -> bool:
        return self.supported and self.fires is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggerId": self.trigger_id,
            "supported": self.supported,
            "fires": self.fires,
            "allowsCardInvocation": self.allows_card_invocation,
            "currentTurn": self.current_turn,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "phase": self.phase,
            "dispatchPhase": self.dispatch_phase,
            "boundary": self.boundary.value,
            "stage": self.stage.value,
            "snapshot": self.snapshot.value,
            "executionMode": self.execution_mode.value,
            "remainingTurn": self.remaining_turn,
            "extraTurn": self.extra_turn,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class _CurrentTurnState:
    """Small Plan3ScalarState projection used by the precise resolver."""

    round_number: int
    block: int = 0
    score: int = 0
    clear_border: int = 1
    stance: str = "neutral"


def _unsupported(
    row: object,
    inputs: TurnProgressUp2EvaluationInput,
    reasons: Sequence[str],
    *,
    threshold: int | None = None,
) -> TurnProgressUp2Evaluation:
    trigger_id = getattr(row, "id", "<unknown>")
    if type(trigger_id) is not str:
        trigger_id = str(trigger_id)
    remaining = inputs.remaining_turn
    if remaining is None:
        remaining = inputs.remain_turn
    if remaining is None:
        remaining = inputs.turns_remaining
    return TurnProgressUp2Evaluation(
        trigger_id=trigger_id,
        supported=False,
        fires=None,
        current_turn=None,
        threshold=threshold,
        comparison=None,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        stage=inputs.stage,
        snapshot=inputs.snapshot,
        execution_mode=inputs.execution_mode,
        remaining_turn=remaining,
        extra_turn=inputs.extra_turn,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def evaluate_turn_progress_up2(
    row: object,
    inputs: TurnProgressUp2EvaluationInput,
) -> TurnProgressUp2Evaluation:
    """Evaluate the card-level gate without paying or executing effects."""

    resolution = resolve_turn_progress_up2_trigger(row)
    if not resolution.supported:
        return _unsupported(row, inputs, resolution.reasons)
    contract = resolution.contract
    assert contract is not None
    reasons: list[str] = []
    if inputs.phase != PHASE_EXAM_START_TURN:
        reasons.append("runtime-phase-is-not-exam-start-turn")
    if inputs.dispatch_phase != DIRECT_DISPATCH_PHASE:
        reasons.append("dispatch-phase-is-not-direct-play-effect")
    if inputs.boundary is not EvaluationBoundary.PRE_PAYMENT_BUILD:
        reasons.append("card-gate-is-not-rechecked-at-this-boundary")
    if inputs.stage is not EvaluationStage.PRE_CARD_COST:
        reasons.append("card-gate-is-not-rechecked-after-card-stage")
    if inputs.snapshot is not StartTurnSnapshot.SETTLED_POST_DRAW:
        reasons.append("start-turn-snapshot-is-not-settled-post-draw")
    if not inputs.start_turn_settled:
        reasons.append("start-turn-settlement-not-proven")
    if not inputs.full_power_settled:
        reasons.append("full-power-settlement-not-proven")
    if not inputs.gimmick_settled:
        reasons.append("gimmick-settlement-not-proven")
    if not inputs.draw_settled:
        reasons.append("draw-settlement-not-proven")
    if any(
        (
            inputs.card_cost_paid,
            inputs.direct_effects_applied,
            inputs.timer_installed,
            inputs.move_applied,
            inputs.card_move_applied,
            inputs.play_count_incremented,
        )
    ):
        reasons.append("snapshot-is-after-payment-effect-move-or-count-step")
    if inputs.current_turn < 1:
        reasons.append("current-turn-is-not-active-1-based-value")
    if reasons:
        return _unsupported(row, inputs, reasons, threshold=contract.threshold)

    native_trigger = EXAM_START_TURN_TRIGGER_BY_ID.get(TARGET_TRIGGER_ID)
    if native_trigger is None or native_trigger.resolution is not NativeTriggerResolution.INDEPENDENT_RESOLVED:
        return _unsupported(
            row,
            inputs,
            ("precise-plan3-start-turn-trigger-is-unavailable",),
            threshold=contract.threshold,
        )
    native_result = fires_native_start_turn(
        native_trigger,
        _CurrentTurnState(round_number=inputs.current_turn),
        event_phase=PHASE_EXAM_START_TURN,
    )
    if native_result.fires is None:
        return _unsupported(
            row,
            inputs,
            native_result.reasons or ("native-current-turn-predicate-unresolved",),
            threshold=contract.threshold,
        )
    remaining = inputs.remaining_turn
    if remaining is None:
        remaining = inputs.remain_turn
    if remaining is None:
        remaining = inputs.turns_remaining
    return TurnProgressUp2Evaluation(
        trigger_id=TARGET_TRIGGER_ID,
        supported=True,
        fires=native_result.fires,
        current_turn=inputs.current_turn,
        threshold=contract.threshold,
        comparison=contract.comparison,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        stage=inputs.stage,
        snapshot=inputs.snapshot,
        execution_mode=inputs.execution_mode,
        remaining_turn=remaining,
        extra_turn=inputs.extra_turn,
    )


# Natural aliases for neighboring Plan2 adapters and callers.
evaluate_plan2_start_turn_turn_progress_up2 = evaluate_turn_progress_up2
evaluate_plan2_start_turn_card_trigger = evaluate_turn_progress_up2
evaluate_plan2_start_turn_trigger = evaluate_turn_progress_up2
evaluate = evaluate_turn_progress_up2


@dataclass(frozen=True, slots=True)
class TurnProgressUp2Simulation:
    evaluations: tuple[TurnProgressUp2Evaluation, ...]

    @property
    def repeatable(self) -> bool:
        return all(item.supported for item in self.evaluations)


def simulate_turn_progress_up2(
    row: object,
    inputs: TurnProgressUp2EvaluationInput,
    *,
    repetitions: int = 1,
) -> TurnProgressUp2Simulation:
    """Evaluate independent card invocations over one immutable snapshot."""

    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    evaluations = tuple(
        evaluate_turn_progress_up2(
            row,
            TurnProgressUp2EvaluationInput(
                current_turn=inputs.current_turn,
                remaining_turn=inputs.remaining_turn,
                remain_turn=inputs.remain_turn,
                turns_remaining=inputs.turns_remaining,
                extra_turn=inputs.extra_turn,
                accumulated_progress=inputs.accumulated_progress,
                turn_progress=inputs.turn_progress,
                phase=inputs.phase,
                dispatch_phase=inputs.dispatch_phase,
                boundary=inputs.boundary,
                stage=inputs.stage,
                snapshot=inputs.snapshot,
                execution_mode=inputs.execution_mode,
                repeat_index=index,
                start_turn_settled=inputs.start_turn_settled,
                full_power_settled=inputs.full_power_settled,
                gimmick_settled=inputs.gimmick_settled,
                draw_settled=inputs.draw_settled,
                card_cost_paid=inputs.card_cost_paid,
                direct_effects_applied=inputs.direct_effects_applied,
                timer_installed=inputs.timer_installed,
                move_applied=inputs.move_applied,
                card_move_applied=inputs.card_move_applied,
                play_count_incremented=inputs.play_count_incremented,
            ),
        )
        for index in range(repetitions)
    )
    return TurnProgressUp2Simulation(evaluations)


@dataclass(frozen=True, slots=True)
class Plan2StartTurnCardEffectSlot:
    slot_index: int
    effect_id: str
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool

    @property
    def is_direct(self) -> bool:
        return self.trigger_id == ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_index": self.slot_index,
            "effect_id": self.effect_id,
            "trigger_id": self.trigger_id,
            "hide_icon": self.hide_icon,
            "is_once_play_effect": self.is_once_play_effect,
        }


@dataclass(frozen=True, slots=True)
class Plan2StartTurnCardVersion:
    card_id: str
    upgrade_count: int
    name: str
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    move_effect_trigger_type: str
    move_effect_ids: tuple[str, ...]
    effect_slots: tuple[Plan2StartTurnCardEffectSlot, ...]

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    @property
    def is_common_direct(self) -> bool:
        return (
            self.card_id == TARGET_CARD_ID
            and self.plan_type == COMMON_PLAN_TYPE
            and self.play_trigger_id == TARGET_TRIGGER_ID
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "name": self.name,
            "plan_type": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "cost_type": self.cost_type,
            "cost_value": self.cost_value,
            "play_trigger_id": self.play_trigger_id,
            "move_position_type": self.move_position_type,
            "move_effect_trigger_type": self.move_effect_trigger_type,
            "move_effect_ids": list(self.move_effect_ids),
            "effect_slots": [slot.to_dict() for slot in self.effect_slots],
            "ordered_effect_ids": list(self.ordered_effect_ids),
            "direct": self.is_common_direct,
        }


@dataclass(frozen=True, slots=True)
class EffectRow:
    id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    effect_group_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "effect_type": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "effect_count": self.effect_count,
            "effect_turn": self.effect_turn,
            "status_enchant_id": self.status_enchant_id,
            "chain_effect_id": self.chain_effect_id,
            "effect_group_ids": list(self.effect_group_ids),
        }


@dataclass(frozen=True, slots=True)
class TurnProgressUp2Catalog:
    database: str
    trigger: TurnProgressTriggerRow
    card_versions: tuple[Plan2StartTurnCardVersion, ...]
    effect_rows: tuple[EffectRow, ...]
    target_status_enchant_ids: tuple[str, ...] = ()
    out_of_scope_card_versions: tuple[Plan2StartTurnCardVersion, ...] = ()
    shape_issues: tuple[str, ...] = ()

    @property
    def target(self) -> TurnProgressTriggerRow:
        return self.trigger

    @property
    def trigger_rows(self) -> tuple[TurnProgressTriggerRow, ...]:
        return (self.trigger,)

    @property
    def affected_card_versions(self) -> tuple[Plan2StartTurnCardVersion, ...]:
        return tuple(
            card
            for card in self.card_versions
            if card.is_common_direct
            or any(slot.trigger_id == TARGET_TRIGGER_ID for slot in card.effect_slots)
        )

    @property
    def direct_card_versions(self) -> tuple[Plan2StartTurnCardVersion, ...]:
        return tuple(card for card in self.card_versions if card.is_common_direct)

    @property
    def co_blocked_card_versions(self) -> tuple[Plan2StartTurnCardVersion, ...]:
        return tuple(
            card
            for card in self.affected_card_versions
            if any(slot.trigger_id not in ("", TARGET_TRIGGER_ID) for slot in card.effect_slots)
        )

    @property
    def affected_card_version_count(self) -> int:
        return len(self.affected_card_versions)

    @property
    def direct_card_version_count(self) -> int:
        return len(self.direct_card_versions)

    @property
    def co_blocked_card_version_count(self) -> int:
        return len(self.co_blocked_card_versions)

    @property
    def co_blocker_count(self) -> int:
        return sum(
            bool(slot.trigger_id) and slot.trigger_id != TARGET_TRIGGER_ID
            for card in self.affected_card_versions
            for slot in card.effect_slots
        )

    @property
    def exact_shape_supported(self) -> bool:
        return not self.shape_issues and (
            self.affected_card_version_count == 4
            and self.direct_card_version_count == 4
            and self.co_blocker_count == 0
        )

    def summary(self) -> dict[str, Any]:
        return {
            "affected": self.affected_card_version_count,
            "direct": self.direct_card_version_count,
            "coBlock": self.co_blocker_count,
            "affected_card_versions": self.affected_card_version_count,
            "direct_card_versions": self.direct_card_version_count,
            "co_blocked_card_versions": self.co_blocked_card_version_count,
            "targetCardId": TARGET_CARD_ID,
            "targetCardName": TARGET_CARD_NAME,
            "targetTriggerId": TARGET_TRIGGER_ID,
            "targetUpgrades": list(TARGET_UPGRADES),
            "effectRowCount": len(self.effect_rows),
            "targetStatusEnchantCount": len(self.target_status_enchant_ids),
            "effectExecutionDelegatedToPlan2Core": True,
            "exact_shape_supported": self.exact_shape_supported,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "trigger": self.trigger.to_dict(),
            "card_versions": [card.to_dict() for card in self.card_versions],
            "effect_rows": [effect.to_dict() for effect in self.effect_rows],
            "target_status_enchant_ids": list(self.target_status_enchant_ids),
            "out_of_scope_card_versions": [
                card.to_dict() for card in self.out_of_scope_card_versions
            ],
            "shape_issues": list(self.shape_issues),
            "summary": self.summary(),
        }


def _json_value(value: object, label: str) -> object:
    if not isinstance(value, str):
        raise TurnProgressUp2CatalogError(f"{label}: JSON text required")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise TurnProgressUp2CatalogError(f"{label}: invalid JSON") from error


def _json_list(value: object, label: str) -> list[object]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, list):
        raise TurnProgressUp2CatalogError(f"{label}: JSON list required")
    return parsed


def _json_object(value: object, label: str) -> dict[str, object]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, dict):
        raise TurnProgressUp2CatalogError(f"{label}: JSON object required")
    return parsed


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_list(value, label)
    if any(type(item) is not str for item in parsed):
        raise TurnProgressUp2CatalogError(f"{label}: string entries required")
    return tuple(parsed)


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_list(value, label)
    if any(type(item) is not int for item in parsed):
        raise TurnProgressUp2CatalogError(f"{label}: integer entries required")
    return tuple(parsed)


def _plain_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise TurnProgressUp2CatalogError(f"{label}: integer required")
    return value


def _trigger_from_sql_row(row: sqlite3.Row) -> TurnProgressTriggerRow:
    trigger_id = row["id"]
    if type(trigger_id) is not str:
        raise TurnProgressUp2CatalogError("trigger.id: string required")
    return TurnProgressTriggerRow(
        id=trigger_id,
        phase_types=_string_tuple(row["phase_types_json"], f"{trigger_id}.phaseTypes"),
        phase_values=_int_tuple(row["phase_values_json"], f"{trigger_id}.phaseValues"),
        field_status_check_types=_string_tuple(
            row["field_status_check_types_json"],
            f"{trigger_id}.fieldStatusCheckTypes",
        ),
        field_status_types=_string_tuple(
            row["field_status_types_json"], f"{trigger_id}.fieldStatusTypes"
        ),
        field_status_values=_int_tuple(
            row["field_status_values_json"], f"{trigger_id}.fieldStatusValues"
        ),
        field_status_card_search_ids=_string_tuple(
            row["field_status_produce_card_search_ids_json"],
            f"{trigger_id}.fieldStatusProduceCardSearchIds",
        ),
        produce_card_search_id=row["produce_card_search_id"],
        upper_search_count=_plain_int(
            row["upper_search_count"], f"{trigger_id}.upperSearchCount"
        ),
        lower_search_count=_plain_int(
            row["lower_search_count"], f"{trigger_id}.lowerSearchCount"
        ),
        card_move_position_type=row["card_move_position_type"],
        effect_types=_string_tuple(row["effect_types_json"], f"{trigger_id}.effectTypes"),
        lesson_type=row["lesson_type"],
    )


def _effect_slots(
    row: sqlite3.Row,
    upgrade: int,
) -> tuple[Plan2StartTurnCardEffectSlot, ...]:
    card_id = row["id"]
    raw_slots = _json_list(row["play_effects_json"], f"{card_id}#{upgrade}.playEffects")
    expected = TARGET_EFFECT_IDS_BY_UPGRADE.get(upgrade)
    if expected is None or len(raw_slots) != 3:
        raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: expected three direct slots")
    slots: list[Plan2StartTurnCardEffectSlot] = []
    for index, raw in enumerate(raw_slots):
        if not isinstance(raw, dict):
            raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: slot is not an object")
        if set(raw) != {"produceExamEffectId", "produceExamTriggerId", "hideIcon", "isOncePlayEffect"}:
            raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: slot shape drift")
        effect_id = raw.get("produceExamEffectId")
        trigger_id = raw.get("produceExamTriggerId")
        hide_icon = raw.get("hideIcon")
        once = raw.get("isOncePlayEffect")
        if effect_id != expected[index] or type(effect_id) is not str:
            raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: effect slot order drift")
        if trigger_id != "" or type(trigger_id) is not str:
            raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: slot trigger drift")
        if hide_icon is not False or type(hide_icon) is not bool:
            raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: hideIcon drift")
        if once is not False or type(once) is not bool:
            raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: isOncePlayEffect drift")
        slots.append(
            Plan2StartTurnCardEffectSlot(index, effect_id, trigger_id, hide_icon, once)
        )
    return tuple(slots)


def _card_from_sql_row(row: sqlite3.Row) -> Plan2StartTurnCardVersion:
    card_id = row["id"]
    upgrade = _plain_int(row["upgrade_count"], f"{card_id}.upgradeCount")
    if type(card_id) is not str or card_id != TARGET_CARD_ID:
        raise TurnProgressUp2CatalogError("card is outside fixed target scope")
    if upgrade not in TARGET_UPGRADES:
        raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: upgrade outside scope")
    if row["name"] != _TARGET_NAMES_BY_UPGRADE[upgrade]:
        raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: name drift")
    if row["plan_type"] != COMMON_PLAN_TYPE:
        raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: plan drift")
    if row["category"] != ACTIVE_SKILL_CATEGORY:
        raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: category drift")
    if row["stamina"] != _TARGET_STAMINA_BY_UPGRADE[upgrade]:
        raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: stamina drift")
    if row["cost_type"] != "ExamCostType_Unknown" or row["cost_value"] != 0:
        raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: cost shape drift")
    if row["play_trigger_id"] != TARGET_TRIGGER_ID:
        raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: play trigger drift")
    if row["move_position_type"] != CARD_MOVE_LOST:
        raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: card move drift")
    raw = _json_object(row["raw_json"], f"{card_id}#{upgrade}.rawJson")
    if raw.get("playProduceExamTriggerId") != TARGET_TRIGGER_ID:
        raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: raw play trigger drift")
    if raw.get("moveEffectTriggerType") != "ProduceCardMoveEffectTriggerType_Unknown":
        raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: move trigger drift")
    if raw.get("moveProduceExamEffectIds") != [] or raw.get("moveProduceExamTriggerIds") != []:
        raise TurnProgressUp2CatalogError(f"{card_id}#{upgrade}: move effect shape drift")
    return Plan2StartTurnCardVersion(
        card_id=card_id,
        upgrade_count=upgrade,
        name=row["name"],
        plan_type=row["plan_type"],
        category=row["category"],
        stamina=_plain_int(row["stamina"], f"{card_id}#{upgrade}.stamina"),
        cost_type=row["cost_type"],
        cost_value=_plain_int(row["cost_value"], f"{card_id}#{upgrade}.costValue"),
        play_trigger_id=row["play_trigger_id"],
        move_position_type=row["move_position_type"],
        move_effect_trigger_type=raw["moveEffectTriggerType"],
        move_effect_ids=tuple(raw["moveProduceExamEffectIds"]),
        effect_slots=_effect_slots(row, upgrade),
    )


def _effect_from_sql_row(row: sqlite3.Row) -> EffectRow:
    effect_id = row["id"]
    raw = _json_object(row["raw_json"], f"{effect_id}.rawJson")
    groups = raw.get("effectGroupIds")
    if type(effect_id) is not str or not isinstance(groups, list) or any(
        type(group) is not str or not group for group in groups
    ):
        raise TurnProgressUp2CatalogError(f"{effect_id}: effectGroupIds drift")
    if row["effect_type"] != TARGET_EFFECT_TYPES_BY_ID.get(effect_id):
        raise TurnProgressUp2CatalogError(f"{effect_id}: effect type drift")
    if effect_id.startswith("e_effect-exam_lesson-"):
        expected_value = int(effect_id.split("-")[-2])
        expected = (expected_value, 0, 1, 0, "", "")
    elif effect_id == _TARGET_EFFECT_STAMINA_ID:
        expected = (0, 0, 0, 2, "", "")
    else:
        expected = (1, 0, 1, 0, "", TARGET_EFFECT_CHAIN_ID)
    actual = (
        row["value1"],
        row["value2"],
        row["effect_count"],
        row["effect_turn"],
        row["status_enchant_id"],
        row["chain_effect_id"],
    )
    if actual != expected:
        raise TurnProgressUp2CatalogError(f"{effect_id}: effect scalar shape drift")
    return EffectRow(
        id=effect_id,
        effect_type=row["effect_type"],
        value1=_plain_int(row["value1"], f"{effect_id}.value1"),
        value2=_plain_int(row["value2"], f"{effect_id}.value2"),
        effect_count=_plain_int(row["effect_count"], f"{effect_id}.effectCount"),
        effect_turn=_plain_int(row["effect_turn"], f"{effect_id}.effectTurn"),
        status_enchant_id=row["status_enchant_id"],
        chain_effect_id=row["chain_effect_id"],
        effect_group_ids=tuple(groups),
    )


def load_plan2_start_turn_turn_progress_up2_catalog(
    database: Path | str = DEFAULT_DATABASE,
) -> TurnProgressUp2Catalog:
    """Read the exact trigger/card/effect slice from Master read-only."""

    path = Path(database)
    if not path.is_file():
        raise TurnProgressUp2CatalogError(f"Master database not found: {path}")
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise TurnProgressUp2CatalogError(f"cannot open Master read-only: {path}") from error
    connection.row_factory = sqlite3.Row
    try:
        # The sibling catalog already parses the complete TurnProgressUp
        # family, including this ExamStartTurn row.  Reuse that exact row
        # rather than creating a second trigger translation here.
        sibling_catalog = load_plan2_turn_progress_trigger_catalog(path)
        trigger = sibling_catalog.trigger(TARGET_TRIGGER_ID)
    except ValueError as error:
        connection.close()
        raise TurnProgressUp2CatalogError(
            f"cannot load sibling TurnProgressUp trigger catalog: {path}"
        ) from error
    with closing(connection):
        status_rows = connection.execute(
            "SELECT id FROM produce_exam_status_enchant "
            "WHERE produce_exam_trigger_id = ? ORDER BY id",
            (TARGET_TRIGGER_ID,),
        ).fetchall()
        status_ids = tuple(row["id"] for row in status_rows)
        if any(type(item) is not str for item in status_ids):
            raise TurnProgressUp2CatalogError("target status IDs must be strings")

        card_rows = connection.execute(
            "SELECT id, upgrade_count, name, plan_type, category, stamina, "
            "cost_type, cost_value, play_trigger_id, move_position_type, "
            "play_effects_json, raw_json FROM card "
            "WHERE play_trigger_id = ? ORDER BY id, upgrade_count",
            (TARGET_TRIGGER_ID,),
        ).fetchall()
        cards = tuple(_card_from_sql_row(row) for row in card_rows)
        if tuple(card.upgrade_count for card in cards) != TARGET_UPGRADES:
            raise TurnProgressUp2CatalogError("target card must contain upgrades 0..3 exactly")

        placeholders = ",".join("?" for _ in TARGET_EFFECT_IDS)
        effect_rows_raw = connection.execute(
            "SELECT id, effect_type, value1, value2, effect_count, effect_turn, "
            "status_enchant_id, chain_effect_id, raw_json FROM effect "
            f"WHERE id IN ({placeholders}) ORDER BY id",
            TARGET_EFFECT_IDS,
        ).fetchall()
        if {row["id"] for row in effect_rows_raw} != set(TARGET_EFFECT_IDS):
            raise TurnProgressUp2CatalogError("target effect rows are incomplete")
        effects = tuple(_effect_from_sql_row(row) for row in effect_rows_raw)
    return TurnProgressUp2Catalog(
        database=str(path),
        trigger=trigger,
        card_versions=cards,
        effect_rows=effects,
        target_status_enchant_ids=status_ids,
    )


# Short aliases used by adjacent Plan2 modules.
load_catalog = load_plan2_start_turn_turn_progress_up2_catalog
TurnProgressTriggerRowForTarget = TurnProgressTriggerRow


NATIVE_EVIDENCE: Final = (
    {
        "source": "Android v3.2.3 native ISIL",
        "locator": (
            "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
            "Assembly-CSharp/Campus/InGame/ExamExtensions.txt::"
            "IsFieldStatusTriggerStatusEffect, 0x6808D24 CMP W0,W21; 0x6808D28 B.LE"
        ),
        "claim": "TurnProgressUp reads ExamParameterModel.CurrentTurn and uses signed current_turn > value2; equality does not fire.",
    },
    {
        "source": "Android v3.2.3 metadata",
        "locator": (
            "_research/android/game-v3.2.3/native-analysis/target-metadata.json"
            "::Campus.InGame.Exam.ExamParameterModel"
        ),
        "claim": "CurrentTurn, RemainTurn, and ExtraTurn are separate signed-int properties; the field evaluator's authority is CurrentTurn.",
    },
    {
        "source": "PC metadata",
        "locator": (
            "_research/il2cpp/B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/"
            "targeted-metadata-index.json::ExamParameterModel/ExamSequence"
        ),
        "claim": "Metadata names get_CurrentTurn, get_RemainTurn, get_ExtraTurn, ExecuteCardCommandImpl, and ConsumeCardCost.",
    },
    {
        "source": "Android v3.2.3 native ISIL",
        "locator": (
            "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
            "Assembly-CSharp/Campus/InGame/Exam/ExamSequence_NestedType__"
            "ExecuteCommandImplAsync_d__111.txt:3884-3896,5617-5766"
        ),
        "claim": "The native card command path builds/validates the direct card command before ConsumeCardCost; PlayEffect execution follows the gate.",
    },
    {
        "source": "Android v3.2.3 native ISIL",
        "locator": (
            "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
            "Assembly-CSharp/Campus/InGame/Exam/ExamSequence_NestedType__"
            "ExamLoopTaskAsync_d__94.txt:675-686,3003-3007"
        ),
        "claim": "EndTurn decrements RemainTurn and the next active turn increments CurrentTurn; pre-first active CurrentTurn is 0 and active turns are 1-based.",
    },
    {
        "source": "Android v3.2.3 native/metadata plus precise local modules",
        "locator": (
            "ExtraTurnEffectExecutor RVA 0x7E794E8; ExamParameterModel.AddExtraTurn; "
            "src/gkms_tool/plan3_engine.py:6146-6154"
        ),
        "claim": "ExtraTurn changes remaining/extra-turn accounting, not CurrentTurn; StartTurn settlement observes the already committed CurrentTurn after full-power/gimmick/draw ordering.",
    },
    {
        "source": "precise local modules",
        "locator": (
            "src/gkms_tool/plan2_turn_progress_trigger.py; "
            "src/gkms_tool/plan3_start_turn_trigger.py"
        ),
        "claim": "The sibling trigger row is reused from the existing Plan2 catalog and the exact signed strict-GT CurrentTurn resolver is reused from the Plan3 StartTurn inventory.",
    },
)


def build_plan2_start_turn_turn_progress_up2_native_audit(
    catalog: TurnProgressUp2Catalog | None = None,
) -> dict[str, Any]:
    """Build a standalone target audit without central coverage output."""

    catalog = catalog or load_plan2_start_turn_turn_progress_up2_catalog()
    resolution = resolve_turn_progress_up2_trigger(catalog.trigger)
    contract = resolution.contract
    return {
        "artifact": "plan2_start_turn_turn_progress_up2_native_audit",
        "schemaVersion": 1,
        "scope": {
            "cardId": TARGET_CARD_ID,
            "cardName": TARGET_CARD_NAME,
            "planType": COMMON_PLAN_TYPE,
            "upgrades": list(TARGET_UPGRADES),
            "triggerId": TARGET_TRIGGER_ID,
        },
        "coverageSummary": catalog.summary(),
        "masterTriggerShape": catalog.trigger.to_dict(),
        "contract": contract.to_dict() if contract is not None else None,
        "nativeConclusion": {
            "currentValueSource": "ExamParameterModel.CurrentTurn",
            "thresholdValue2": TARGET_THRESHOLD,
            "thresholdSignedInt32Inclusive": [INT32_MIN, INT32_MAX],
            "comparison": "CurrentTurn > value2",
            "equalityFires": False,
            "initialCurrentTurn": {
                "preFirstActiveTurn": 0,
                "firstActiveTurn": 1,
                "activeIndexing": "1-based",
                "zeroPolicy": "fail-closed",
            },
            "remainTurnAndExtraTurn": (
                "RemainTurn/ExtraTurn changes do not change the CurrentTurn operand; "
                "ExtraTurn supplies additional remaining-turn accounting."
            ),
            "startTurnSettlement": (
                "Read the already settled CurrentTurn at ExamStartTurn after the "
                "native full-power/gimmick/draw ordering; settlement does not add a turn."
            ),
            "cardGateOrder": [
                "settled ExamStartTurn post-draw snapshot",
                "card-level TurnProgressUp direct gate",
                "card cost/payment",
                "direct PlayEffect command/effect execution",
                "card move and play-count update",
            ],
            "sharedExecutionModes": [item.value for item in ExecutionMode],
        },
        "failClosed": {
            "masterShapeDeviation": True,
            "unexpectedNotCheck": True,
            "unexpectedSearchOrEffectCondition": True,
            "wrongPhaseOrDispatch": True,
            "preSettlementSnapshot": True,
            "preFirstActiveCurrentTurnZero": True,
            "postPaymentOrEffectSnapshot": True,
        },
        "nativeEvidence": list(NATIVE_EVIDENCE),
        "reusedPreciseModules": [
            "src/gkms_tool/plan2_turn_progress_trigger.py",
            "src/gkms_tool/plan3_start_turn_trigger.py",
            "src/gkms_tool/plan3_engine.py",
        ],
        "centralCoverageRebuilt": False,
        "centralCoreModified": False,
        "plan3Modified": False,
        "guiModified": False,
        "effectExecution": "delegated-to-existing-plan2-runtime",
        "resolution": {
            "supported": resolution.supported,
            "reasons": list(resolution.reasons),
        },
    }


build_native_audit = build_plan2_start_turn_turn_progress_up2_native_audit


__all__ = [
    "ACTIVE_SKILL_CATEGORY",
    "CARD_MOVE_LOST",
    "CardExecutionKind",
    "CHECK_NOT",
    "COMMON_PLAN_TYPE",
    "DEFAULT_AUDIT_PATH",
    "DIRECT_DISPATCH_PHASE",
    "EffectRow",
    "EvaluationBoundary",
    "EvaluationStage",
    "ExecutionMode",
    "FIELD_TURN_PROGRESS_UP",
    "INT32_MAX",
    "INT32_MIN",
    "LESSON_UNKNOWN",
    "MOVE_UNKNOWN",
    "NATIVE_EVIDENCE",
    "PHASE_EXAM_START_TURN",
    "Plan2StartTurnCardEffectSlot",
    "Plan2StartTurnCardVersion",
    "START_TURN_TRIGGER_ID",
    "StartTurnSnapshot",
    "TARGET_CARD_ID",
    "TARGET_CARD_NAME",
    "TARGET_EFFECT_IDS",
    "TARGET_EFFECT_IDS_BY_UPGRADE",
    "TARGET_EFFECT_TYPES_BY_ID",
    "TARGET_THRESHOLD",
    "TARGET_TRIGGER_ID",
    "TARGET_UPGRADES",
    "TRIGGER_THRESHOLD",
    "TurnProgressTriggerRow",
    "TurnProgressTriggerRowForTarget",
    "TurnProgressUp2Catalog",
    "TurnProgressUp2CatalogError",
    "TurnProgressUp2Contract",
    "TurnProgressUp2Evaluation",
    "TurnProgressUp2EvaluationInput",
    "TurnProgressUp2Simulation",
    "build_native_audit",
    "build_plan2_start_turn_turn_progress_up2_native_audit",
    "evaluate",
    "evaluate_plan2_start_turn_card_trigger",
    "evaluate_plan2_start_turn_trigger",
    "evaluate_plan2_start_turn_turn_progress_up2",
    "evaluate_turn_progress_up2",
    "load_catalog",
    "load_plan2_start_turn_turn_progress_up2_catalog",
    "resolve_turn_progress_up2_trigger",
    "simulate_turn_progress_up2",
]
