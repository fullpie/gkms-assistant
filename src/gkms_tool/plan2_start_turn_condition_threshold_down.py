"""Standalone Plan2/Common resolver for one card-owned StartTurn gate.

The only executable predicate in this module is the exact Master row
``e_trigger-exam_start_turn-condition_threshold_multiple_down-1000``.  It is
kept separate from the central runtime because the card's three direct effect
slots are already owned by Plan2 core; this file only decides whether the
card-level gate admits the card command.

The Android v3.2.3 field evaluator proves the following expression at the
settled StartTurn snapshot::

    float32(1000) / float32(1000) >=
        float32(ExamParameterModel.JudgeParameter) /
        float32(ExamParameterModel.ClearBorder)

The native operands are signed ``Int32`` values and the comparison is
inclusive.  ``ClearBorder <= 0`` is rejected before division so a degenerate
snapshot never turns into an invented truth value.  The card command path is
also explicit: both UseHand and UsePool converge on ``IsPlayable`` before the
direct PlayEffect batch; the latter's cost/no-cost variants are represented by
the forced/extra modes below, not inferred from card description text.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from .plan2_aggressive_card_trigger import EvaluationBoundary


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "var" / "master.sqlite3"
DEFAULT_AUDIT_PATH = (
    PROJECT_ROOT
    / "var"
    / "coverage"
    / "plan2_start_turn_condition_threshold_down_native_audit.json"
)

PHASE_EXAM_START_TURN = "ProduceExamPhaseType_ExamStartTurn"
DIRECT_DISPATCH_PHASE = "PlayEffect"
FIELD_CONDITION_THRESHOLD_MULTIPLE_DOWN = (
    "ProduceExamFieldStatusType_ConditionThresholdMultipleDown"
)
CHECK_NOT = "ProduceExamTriggerCheckType_Not"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
MOVE_LOST = "ProduceCardMovePositionType_Lost"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
PLAN_COMMON = "ProducePlanType_Common"
TARGET_TRIGGER_ID = (
    "e_trigger-exam_start_turn-condition_threshold_multiple_down-1000"
)
TARGET_CARD_ID = "p_card-00-sup-2_025"
TARGET_CARD_NAME = "お姉ちゃんだもの！"
TARGET_UPGRADES = (0, 1, 2, 3)
TARGET_THRESHOLD_PERMILLE = 1000
_TARGET_NAMES_BY_UPGRADE = {
    0: "お姉ちゃんだもの！",
    1: "お姉ちゃんだもの！+",
    2: "お姉ちゃんだもの！++",
    3: "お姉ちゃんだもの！+++",
}

_I32_MIN = -(2**31)
_I32_MAX = 2**31 - 1
_TARGET_EFFECTS_BY_UPGRADE: dict[int, tuple[str, ...]] = {
    0: (
        "e_effect-exam_lesson-0006-01",
        "e_effect-exam_block-0006",
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
    ),
    1: (
        "e_effect-exam_lesson-0010-01",
        "e_effect-exam_block-0009",
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
    ),
    2: (
        "e_effect-exam_lesson-0011-01",
        "e_effect-exam_block-0009",
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
    ),
    3: (
        "e_effect-exam_lesson-0011-01",
        "e_effect-exam_block-0009",
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
    ),
}
_TARGET_EFFECT_IDS = frozenset(
    {
        *(_TARGET_EFFECTS_BY_UPGRADE[upgrade][0] for upgrade in TARGET_UPGRADES),
        *(_TARGET_EFFECTS_BY_UPGRADE[upgrade][1] for upgrade in TARGET_UPGRADES),
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
        "e_effect-exam_card_draw-0001",
    }
)


class ConditionThresholdDownCatalogError(ValueError):
    """The local Master slice is missing or outside the exact target shape."""


class EvaluationStage(str, Enum):
    """Transaction stage at which the card gate is observed."""

    PRE_CARD_COST = "pre-card-cost"
    POST_CARD_COST = "post-card-cost"
    POST_DIRECT_EFFECTS = "post-direct-effects"
    POST_TIMER_INSTALL = "post-timer-install"


class StartTurnSnapshot(str, Enum):
    """Native lifecycle boundary supplied to the card-level predicate."""

    SETTLED_POST_DRAW = "settled-start-turn-post-draw"
    PRE_DRAW = "pre-draw"


class ExecutionMode(str, Enum):
    """Native command paths that share the card-level ``IsPlayable`` gate."""

    ORDINARY = "ordinary"
    FORCED = "forced"
    EXTRA = "extra"


@dataclass(frozen=True, slots=True)
class ConditionThresholdDownTriggerRow:
    """All columns of one ``produce_exam_trigger`` Master row."""

    id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...]
    field_status_check_types: tuple[str, ...]
    field_status_types: tuple[str, ...]
    field_status_values: tuple[int, ...]
    field_status_card_search_ids: tuple[str, ...]
    produce_card_search_id: str
    upper_search_count: int
    lower_search_count: int
    card_move_position_type: str
    effect_types: tuple[str, ...]
    lesson_type: str

    def __post_init__(self) -> None:
        if type(self.id) is not str or not self.id:
            raise TypeError("trigger id must be a non-empty string")
        for name in (
            "phase_types",
            "phase_values",
            "field_status_check_types",
            "field_status_types",
            "field_status_values",
            "field_status_card_search_ids",
            "effect_types",
        ):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                raise TypeError(f"{name} must be a tuple")
        for name in (
            "phase_types",
            "field_status_check_types",
            "field_status_types",
            "field_status_card_search_ids",
            "effect_types",
        ):
            if any(type(item) is not str for item in getattr(self, name)):
                raise TypeError(f"{name} must contain strings")
        for name in ("phase_values", "field_status_values"):
            if any(type(item) is not int for item in getattr(self, name)):
                raise TypeError(f"{name} must contain plain integers")
        if type(self.produce_card_search_id) is not str:
            raise TypeError("produce_card_search_id must be a string")
        if type(self.card_move_position_type) is not str:
            raise TypeError("card_move_position_type must be a string")
        if type(self.lesson_type) is not str:
            raise TypeError("lesson_type must be a string")
        for name in ("upper_search_count", "lower_search_count"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be a plain integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phaseTypes": list(self.phase_types),
            "phaseValues": list(self.phase_values),
            "fieldStatusCheckTypes": list(self.field_status_check_types),
            "fieldStatusTypes": list(self.field_status_types),
            "fieldStatusValues": list(self.field_status_values),
            "fieldStatusProduceCardSearchIds": list(
                self.field_status_card_search_ids
            ),
            "produceCardSearchId": self.produce_card_search_id,
            "upperSearchCount": self.upper_search_count,
            "lowerSearchCount": self.lower_search_count,
            "cardMovePositionType": self.card_move_position_type,
            "effectTypes": list(self.effect_types),
            "lessonType": self.lesson_type,
        }


@dataclass(frozen=True, slots=True)
class ConditionThresholdDownContract:
    """Native semantics proven for the exact target row."""

    row: ConditionThresholdDownTriggerRow
    threshold: int
    threshold_unit: str = "permille"
    comparison: str = "threshold_ratio >= current_ratio"
    equivalent_comparison: str = "current_ratio <= threshold_ratio"
    current_value_source: str = "ExamParameterModel.JudgeParameter"
    denominator_source: str = "ExamParameterModel.ClearBorder"
    value_type: str = "signed Int32"
    equality_fires: bool = True
    clear_border_policy: str = "clearBorder <= 0 => fail-closed"
    snapshot: str = StartTurnSnapshot.SETTLED_POST_DRAW.value
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    evaluation_boundary: str = EvaluationBoundary.PRE_PAYMENT_BUILD.value

    @property
    def result_expression(self) -> str:
        return (
            "float32(threshold) / float32(1000) >= "
            "float32(JudgeParameter) / float32(ClearBorder)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggerId": self.row.id,
            "threshold": self.threshold,
            "thresholdUnit": self.threshold_unit,
            "comparison": self.comparison,
            "equivalentComparison": self.equivalent_comparison,
            "resultExpression": self.result_expression,
            "currentValueSource": self.current_value_source,
            "denominatorSource": self.denominator_source,
            "valueType": self.value_type,
            "equalityFires": self.equality_fires,
            "clearBorderPolicy": self.clear_border_policy,
            "snapshot": self.snapshot,
            "dispatchPhase": self.dispatch_phase,
            "evaluationBoundary": self.evaluation_boundary,
            "checkNot": False,
        }


@dataclass(frozen=True, slots=True)
class ContractResolution:
    supported: bool
    contract: ConditionThresholdDownContract | None
    reasons: tuple[str, ...] = ()


def _shape_reasons(row: ConditionThresholdDownTriggerRow) -> tuple[str, ...]:
    reasons: list[str] = []
    if row.id != TARGET_TRIGGER_ID:
        reasons.append("trigger-id-is-not-target")
    if row.phase_types != (PHASE_EXAM_START_TURN,):
        reasons.append("phase-shape-is-not-exam-start-turn")
    if row.phase_values:
        reasons.append("phase-values-must-be-empty")
    if row.field_status_check_types:
        reasons.append("field-status-check-types-must-be-empty")
    if row.field_status_types != (FIELD_CONDITION_THRESHOLD_MULTIPLE_DOWN,):
        reasons.append("field-status-type-is-not-threshold-multiple-down")
    if row.field_status_values != (TARGET_THRESHOLD_PERMILLE,):
        reasons.append("field-status-value-is-not-exact-1000")
    elif not _I32_MIN <= row.field_status_values[0] <= _I32_MAX:
        reasons.append("field-status-value-is-outside-signed-int32")
    if row.field_status_card_search_ids:
        reasons.append("field-card-search-ids-must-be-empty")
    if row.produce_card_search_id:
        reasons.append("produce-card-search-id-must-be-empty")
    if row.upper_search_count != 0 or row.lower_search_count != 0:
        reasons.append("search-count-bounds-must-be-zero")
    if row.card_move_position_type != MOVE_UNKNOWN:
        reasons.append("card-move-condition-must-be-unknown")
    if row.effect_types:
        reasons.append("effect-types-must-be-empty")
    if row.lesson_type != LESSON_UNKNOWN:
        reasons.append("lesson-condition-must-be-unknown")
    return tuple(reasons)


def resolve_condition_threshold_down_trigger(
    row: ConditionThresholdDownTriggerRow,
) -> ContractResolution:
    """Resolve only the exact complete Master shape; everything else stops."""

    if not isinstance(row, ConditionThresholdDownTriggerRow):
        return ContractResolution(False, None, ("trigger-record-type-is-unknown",))
    reasons = _shape_reasons(row)
    if reasons:
        return ContractResolution(False, None, reasons)
    return ContractResolution(
        True,
        ConditionThresholdDownContract(
            row=row,
            threshold=TARGET_THRESHOLD_PERMILLE,
        ),
    )


@dataclass(frozen=True, slots=True)
class ConditionThresholdDownEvaluationInput:
    """Immutable snapshot passed to the card-level gate."""

    judge_parameter: int
    clear_border: int
    phase: str = PHASE_EXAM_START_TURN
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    boundary: EvaluationBoundary = EvaluationBoundary.PRE_PAYMENT_BUILD
    stage: EvaluationStage = EvaluationStage.PRE_CARD_COST
    snapshot: StartTurnSnapshot = StartTurnSnapshot.SETTLED_POST_DRAW
    execution_mode: ExecutionMode = ExecutionMode.ORDINARY
    repeat_index: int = 0
    card_cost_paid: bool = False
    direct_effects_applied: bool = False
    timer_installed: bool = False

    def __post_init__(self) -> None:
        for name in ("judge_parameter", "clear_border"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be a signed Int32")
            if not _I32_MIN <= value <= _I32_MAX:
                raise ValueError(f"{name} is outside signed Int32")
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
            "card_cost_paid",
            "direct_effects_applied",
            "timer_installed",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")


@dataclass(frozen=True, slots=True)
class ConditionThresholdDownEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    judge_parameter: int | None
    clear_border: int | None
    threshold: int | None
    threshold_ratio: float | None
    current_ratio: float | None
    comparison: str | None
    phase: str
    dispatch_phase: str
    boundary: EvaluationBoundary
    stage: EvaluationStage
    snapshot: StartTurnSnapshot
    execution_mode: ExecutionMode
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    @property
    def allows_card_invocation(self) -> bool:
        return self.supported and self.fires is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggerId": self.trigger_id,
            "supported": self.supported,
            "fires": self.fires,
            "allowsCardInvocation": self.allows_card_invocation,
            "judgeParameter": self.judge_parameter,
            "clearBorder": self.clear_border,
            "threshold": self.threshold,
            "thresholdRatio": self.threshold_ratio,
            "currentRatio": self.current_ratio,
            "comparison": self.comparison,
            "phase": self.phase,
            "dispatchPhase": self.dispatch_phase,
            "boundary": self.boundary.value,
            "stage": self.stage.value,
            "snapshot": self.snapshot.value,
            "executionMode": self.execution_mode.value,
            "reasons": list(self.reasons),
        }


def _unsupported_evaluation(
    row: ConditionThresholdDownTriggerRow | object,
    inputs: ConditionThresholdDownEvaluationInput,
    reasons: Sequence[str],
    *,
    threshold: int | None = None,
) -> ConditionThresholdDownEvaluation:
    trigger_id = getattr(row, "id", "<unknown>")
    if type(trigger_id) is not str:
        trigger_id = str(trigger_id)
    return ConditionThresholdDownEvaluation(
        trigger_id=trigger_id,
        supported=False,
        fires=None,
        judge_parameter=None,
        clear_border=None,
        threshold=threshold,
        threshold_ratio=None,
        current_ratio=None,
        comparison=None,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        stage=inputs.stage,
        snapshot=inputs.snapshot,
        execution_mode=inputs.execution_mode,
        reasons=tuple(reasons),
    )


def _float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def evaluate_condition_threshold_down(
    row: ConditionThresholdDownTriggerRow,
    inputs: ConditionThresholdDownEvaluationInput,
) -> ConditionThresholdDownEvaluation:
    """Evaluate the card gate without paying, applying, or installing effects."""

    resolution = resolve_condition_threshold_down_trigger(row)
    if not resolution.supported:
        return _unsupported_evaluation(row, inputs, resolution.reasons)
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
    if any(
        (
            inputs.card_cost_paid,
            inputs.direct_effects_applied,
            inputs.timer_installed,
        )
    ):
        reasons.append("snapshot-is-after-payment-effect-or-timer-step")
    if inputs.clear_border <= 0:
        reasons.append("clear-border-must-be-positive")
    if reasons:
        return _unsupported_evaluation(
            row,
            inputs,
            reasons,
            threshold=contract.threshold,
        )

    try:
        threshold_ratio = _float32(
            _float32(contract.threshold) / _float32(1000.0)
        )
        current_ratio = _float32(
            _float32(inputs.judge_parameter)
            / _float32(inputs.clear_border)
        )
    except (OverflowError, struct.error, ZeroDivisionError):
        return _unsupported_evaluation(
            row,
            inputs,
            ("condition-threshold-float32-conversion-unproven",),
            threshold=contract.threshold,
        )
    return ConditionThresholdDownEvaluation(
        trigger_id=row.id,
        supported=True,
        fires=threshold_ratio >= current_ratio,
        judge_parameter=inputs.judge_parameter,
        clear_border=inputs.clear_border,
        threshold=contract.threshold,
        threshold_ratio=threshold_ratio,
        current_ratio=current_ratio,
        comparison=contract.comparison,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        stage=inputs.stage,
        snapshot=inputs.snapshot,
        execution_mode=inputs.execution_mode,
    )


@dataclass(frozen=True, slots=True)
class ConditionThresholdDownSimulation:
    evaluations: tuple[ConditionThresholdDownEvaluation, ...]

    @property
    def repeatable(self) -> bool:
        return all(item.supported for item in self.evaluations)


def simulate_condition_threshold_down(
    row: ConditionThresholdDownTriggerRow,
    inputs: ConditionThresholdDownEvaluationInput,
    *,
    repetitions: int = 1,
) -> ConditionThresholdDownSimulation:
    """Re-evaluate independent card invocations over the same immutable snapshot."""

    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    evaluations = tuple(
        evaluate_condition_threshold_down(
            row,
            ConditionThresholdDownEvaluationInput(
                judge_parameter=inputs.judge_parameter,
                clear_border=inputs.clear_border,
                phase=inputs.phase,
                dispatch_phase=inputs.dispatch_phase,
                boundary=inputs.boundary,
                stage=inputs.stage,
                snapshot=inputs.snapshot,
                execution_mode=inputs.execution_mode,
                repeat_index=index,
                card_cost_paid=inputs.card_cost_paid,
                direct_effects_applied=inputs.direct_effects_applied,
                timer_installed=inputs.timer_installed,
            ),
        )
        for index in range(repetitions)
    )
    return ConditionThresholdDownSimulation(evaluations)


@dataclass(frozen=True, slots=True)
class CardEffectSlot:
    card_id: str
    upgrade_count: int
    slot_index: int
    effect_id: str
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade_count,
            "slot": self.slot_index,
            "effectId": self.effect_id,
            "triggerId": self.trigger_id,
            "hideIcon": self.hide_icon,
            "isOncePlayEffect": self.is_once_play_effect,
        }


@dataclass(frozen=True, slots=True)
class CardVersion:
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
    effect_slots: tuple[CardEffectSlot, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade_count,
            "name": self.name,
            "planType": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "costType": self.cost_type,
            "costValue": self.cost_value,
            "playTriggerId": self.play_trigger_id,
            "movePositionType": self.move_position_type,
            "effectSlots": [slot.to_dict() for slot in self.effect_slots],
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "effectType": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "effectCount": self.effect_count,
            "effectTurn": self.effect_turn,
            "statusEnchantId": self.status_enchant_id,
            "chainEffectId": self.chain_effect_id,
        }


@dataclass(frozen=True, slots=True)
class ConditionThresholdDownCatalog:
    trigger_rows: tuple[ConditionThresholdDownTriggerRow, ...]
    card_versions: tuple[CardVersion, ...]
    effect_rows: tuple[EffectRow, ...]

    @property
    def target(self) -> ConditionThresholdDownTriggerRow:
        for row in self.trigger_rows:
            if row.id == TARGET_TRIGGER_ID:
                return row
        raise KeyError(TARGET_TRIGGER_ID)

    @property
    def target_card_versions(self) -> tuple[CardVersion, ...]:
        return tuple(
            card
            for card in self.card_versions
            if card.card_id == TARGET_CARD_ID
        )

    @property
    def direct_card_play_reference_count(self) -> int:
        return sum(
            card.play_trigger_id == TARGET_TRIGGER_ID
            for card in self.target_card_versions
        )

    @property
    def target_slot_trigger_reference_count(self) -> int:
        return sum(
            slot.trigger_id == TARGET_TRIGGER_ID
            for card in self.target_card_versions
            for slot in card.effect_slots
        )

    @property
    def affected_card_version_count(self) -> int:
        return sum(
            card.play_trigger_id == TARGET_TRIGGER_ID
            or any(
                slot.trigger_id == TARGET_TRIGGER_ID for slot in card.effect_slots
            )
            for card in self.target_card_versions
        )

    @property
    def co_blocker_count(self) -> int:
        """Non-target trigger references in the target card's play slots."""

        return sum(
            bool(slot.trigger_id)
            and slot.trigger_id != TARGET_TRIGGER_ID
            for card in self.target_card_versions
            for slot in card.effect_slots
        )

    def trigger(self, trigger_id: str) -> ConditionThresholdDownTriggerRow:
        for row in self.trigger_rows:
            if row.id == trigger_id:
                return row
        raise KeyError(trigger_id)

    def summary(self) -> dict[str, Any]:
        return {
            "affected": self.affected_card_version_count,
            "direct": self.direct_card_play_reference_count,
            "coBlock": self.co_blocker_count,
            "targetSlotTriggerReferences": self.target_slot_trigger_reference_count,
            "targetCardId": TARGET_CARD_ID,
            "targetUpgrades": list(TARGET_UPGRADES),
            "targetTriggerId": TARGET_TRIGGER_ID,
            "effectRowCount": len(self.effect_rows),
            "effectExecutionDelegatedToPlan2Core": True,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "triggerRows": [row.to_dict() for row in self.trigger_rows],
            "cardVersions": [card.to_dict() for card in self.card_versions],
            "effectRows": [row.to_dict() for row in self.effect_rows],
        }


def _json_value(value: object, label: str) -> object:
    if not isinstance(value, str):
        raise ConditionThresholdDownCatalogError(f"{label}: JSON text required")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise ConditionThresholdDownCatalogError(f"{label}: invalid JSON") from error


def _json_list(value: object, label: str) -> list[object]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, list):
        raise ConditionThresholdDownCatalogError(f"{label}: JSON list required")
    return parsed


def _json_object(value: object, label: str) -> dict[str, object]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, dict):
        raise ConditionThresholdDownCatalogError(f"{label}: JSON object required")
    return parsed


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_list(value, label)
    if any(type(item) is not str for item in parsed):
        raise ConditionThresholdDownCatalogError(f"{label}: string entries required")
    return tuple(parsed)


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_list(value, label)
    if any(type(item) is not int for item in parsed):
        raise ConditionThresholdDownCatalogError(f"{label}: integer entries required")
    return tuple(parsed)


def _plain_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise ConditionThresholdDownCatalogError(f"{label}: integer required")
    return value


def _trigger_from_row(row: sqlite3.Row) -> ConditionThresholdDownTriggerRow:
    trigger_id = str(row["id"])
    return ConditionThresholdDownTriggerRow(
        id=trigger_id,
        phase_types=_string_tuple(
            row["phase_types_json"], f"{trigger_id}.phaseTypes"
        ),
        phase_values=_int_tuple(
            row["phase_values_json"], f"{trigger_id}.phaseValues"
        ),
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
        produce_card_search_id=str(row["produce_card_search_id"]),
        upper_search_count=_plain_int(
            row["upper_search_count"], f"{trigger_id}.upperSearchCount"
        ),
        lower_search_count=_plain_int(
            row["lower_search_count"], f"{trigger_id}.lowerSearchCount"
        ),
        card_move_position_type=str(row["card_move_position_type"]),
        effect_types=_string_tuple(
            row["effect_types_json"], f"{trigger_id}.effectTypes"
        ),
        lesson_type=str(row["lesson_type"]),
    )


def _card_from_row(row: sqlite3.Row) -> CardVersion:
    card_id = str(row["id"])
    upgrade = _plain_int(row["upgrade_count"], f"{card_id}.upgradeCount")
    if card_id != TARGET_CARD_ID or upgrade not in TARGET_UPGRADES:
        raise ConditionThresholdDownCatalogError("card is outside fixed target slice")
    if str(row["name"]) != _TARGET_NAMES_BY_UPGRADE[upgrade]:
        raise ConditionThresholdDownCatalogError(f"{card_id}+{upgrade}: name drift")
    if str(row["plan_type"]) != PLAN_COMMON:
        raise ConditionThresholdDownCatalogError(f"{card_id}+{upgrade}: plan drift")
    if str(row["play_trigger_id"]) != TARGET_TRIGGER_ID:
        raise ConditionThresholdDownCatalogError(f"{card_id}+{upgrade}: trigger drift")
    raw = _json_object(row["raw_json"], f"{card_id}+{upgrade}.rawJson")
    if raw.get("playProduceExamTriggerId") != TARGET_TRIGGER_ID:
        raise ConditionThresholdDownCatalogError(
            f"{card_id}+{upgrade}: raw play trigger drift"
        )
    if raw.get("moveEffectTriggerType") != "ProduceCardMoveEffectTriggerType_Unknown":
        raise ConditionThresholdDownCatalogError(
            f"{card_id}+{upgrade}: move trigger shape drift"
        )
    for name in ("moveProduceExamEffectIds", "moveProduceExamTriggerIds"):
        if raw.get(name) != []:
            raise ConditionThresholdDownCatalogError(
                f"{card_id}+{upgrade}: {name} must be empty"
            )
    raw_effects = _json_list(
        row["play_effects_json"], f"{card_id}+{upgrade}.playEffects"
    )
    expected_effects = _TARGET_EFFECTS_BY_UPGRADE[upgrade]
    if len(raw_effects) != len(expected_effects):
        raise ConditionThresholdDownCatalogError(
            f"{card_id}+{upgrade}: expected three direct effects"
        )
    slots: list[CardEffectSlot] = []
    for index, raw_slot in enumerate(raw_effects):
        if not isinstance(raw_slot, dict):
            raise ConditionThresholdDownCatalogError(
                f"{card_id}+{upgrade}: effect slot must be object"
            )
        effect_id = raw_slot.get("produceExamEffectId")
        trigger_id = raw_slot.get("produceExamTriggerId")
        hide_icon = raw_slot.get("hideIcon")
        once = raw_slot.get("isOncePlayEffect")
        if effect_id != expected_effects[index] or type(effect_id) is not str:
            raise ConditionThresholdDownCatalogError(
                f"{card_id}+{upgrade}: direct effect shape drift"
            )
        if type(trigger_id) is not str or type(hide_icon) is not bool:
            raise ConditionThresholdDownCatalogError(
                f"{card_id}+{upgrade}: effect trigger/visibility shape drift"
            )
        if type(once) is not bool:
            raise ConditionThresholdDownCatalogError(
                f"{card_id}+{upgrade}: once flag must be bool"
            )
        slots.append(
            CardEffectSlot(
                card_id=card_id,
                upgrade_count=upgrade,
                slot_index=index,
                effect_id=effect_id,
                trigger_id=trigger_id,
                hide_icon=hide_icon,
                is_once_play_effect=once,
            )
        )
    return CardVersion(
        card_id=card_id,
        upgrade_count=upgrade,
        name=str(row["name"]),
        plan_type=str(row["plan_type"]),
        category=str(row["category"]),
        stamina=_plain_int(row["stamina"], f"{card_id}+{upgrade}.stamina"),
        cost_type=str(row["cost_type"]),
        cost_value=_plain_int(row["cost_value"], f"{card_id}+{upgrade}.costValue"),
        play_trigger_id=str(row["play_trigger_id"]),
        move_position_type=str(row["move_position_type"]),
        effect_slots=tuple(slots),
    )


def _effect_from_row(row: sqlite3.Row) -> EffectRow:
    effect_id = str(row["id"])
    return EffectRow(
        id=effect_id,
        effect_type=str(row["effect_type"]),
        value1=_plain_int(row["value1"], f"{effect_id}.value1"),
        value2=_plain_int(row["value2"], f"{effect_id}.value2"),
        effect_count=_plain_int(row["effect_count"], f"{effect_id}.effectCount"),
        effect_turn=_plain_int(row["effect_turn"], f"{effect_id}.effectTurn"),
        status_enchant_id=str(row["status_enchant_id"]),
        chain_effect_id=str(row["chain_effect_id"]),
    )


def load_plan2_start_turn_condition_threshold_down_catalog(
    database: Path | str = DEFAULT_DATABASE,
) -> ConditionThresholdDownCatalog:
    """Read exactly the target trigger/card/effect slice from Master read-only."""

    path = Path(database)
    if not path.is_file():
        raise ConditionThresholdDownCatalogError(f"Master database not found: {path}")
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise ConditionThresholdDownCatalogError(
            f"cannot open Master read-only: {path}"
        ) from error
    connection.row_factory = sqlite3.Row
    with closing(connection):
        try:
            trigger_rows_raw = connection.execute(
                "SELECT id, phase_types_json, phase_values_json, "
                "field_status_check_types_json, field_status_types_json, "
                "field_status_values_json, field_status_produce_card_search_ids_json, "
                "produce_card_search_id, upper_search_count, lower_search_count, "
                "card_move_position_type, effect_types_json, lesson_type "
                "FROM produce_exam_trigger WHERE id = ?",
                (TARGET_TRIGGER_ID,),
            ).fetchall()
            if len(trigger_rows_raw) != 1:
                raise ConditionThresholdDownCatalogError(
                    "target trigger row must exist exactly once"
                )
            trigger_rows = (_trigger_from_row(trigger_rows_raw[0]),)

            card_rows_raw = connection.execute(
                "SELECT id, upgrade_count, name, plan_type, category, stamina, "
                "cost_type, cost_value, play_trigger_id, move_position_type, "
                "play_effects_json, raw_json FROM card WHERE id = ? "
                "ORDER BY upgrade_count",
                (TARGET_CARD_ID,),
            ).fetchall()
            if tuple(row["upgrade_count"] for row in card_rows_raw) != TARGET_UPGRADES:
                raise ConditionThresholdDownCatalogError(
                    "target card must contain upgrades 0..3 exactly"
                )
            card_versions = tuple(_card_from_row(row) for row in card_rows_raw)

            placeholders = ",".join("?" for _ in _TARGET_EFFECT_IDS)
            effect_rows_raw = connection.execute(
                "SELECT id, effect_type, value1, value2, effect_count, effect_turn, "
                "status_enchant_id, chain_effect_id FROM effect "
                f"WHERE id IN ({placeholders}) ORDER BY id",
                tuple(sorted(_TARGET_EFFECT_IDS)),
            ).fetchall()
            if {str(row["id"]) for row in effect_rows_raw} != set(_TARGET_EFFECT_IDS):
                raise ConditionThresholdDownCatalogError(
                    "target direct/timer effect rows are incomplete"
                )
            effect_rows = tuple(_effect_from_row(row) for row in effect_rows_raw)
        except sqlite3.Error as error:
            raise ConditionThresholdDownCatalogError(
                f"cannot read target Master slice: {path}"
            ) from error
    return ConditionThresholdDownCatalog(
        trigger_rows=trigger_rows,
        card_versions=card_versions,
        effect_rows=effect_rows,
    )


NATIVE_EVIDENCE: tuple[dict[str, str], ...] = (
    {
        "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt",
        "locator": "IsFieldStatusTriggerStatusEffect 0x68082D4..0x6808E5C; ISIL get_JudgeParameter/0x447A0000/get_ClearBorder",
        "claim": "The target field evaluator reads signed JudgeParameter and ClearBorder and uses float32 arithmetic with 1000.0f; the threshold-down comparison is inclusive.",
    },
    {
        "source": "_research/il2cpp/B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/targeted-metadata-index.json",
        "locator": "ExamParameterModel get_JudgeParameter/get_ClearBorder; ProduceExamFieldStatusType.ConditionThresholdMultipleDown",
        "claim": "PC metadata independently names both current-value/denominator properties and the exact field enum; no description-text translation is used.",
    },
    {
        "source": "_research/android/game-v3.2.3/native-analysis/target-metadata.json",
        "locator": "IL2CPP metadata v31 targeted Android type/method records",
        "claim": "Android metadata matches the native v3.2.3 type/method shape used by the evaluator.",
    },
    {
        "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/Exam/ExamSequence.txt",
        "locator": "ExecuteCardCommandImpl 0x7ECE628..0x7ECF228; ISIL IsPlayable before PlayProduceExamEffectList and CreatePlayEffectCommand",
        "claim": "The card-level IsPlayable gate precedes direct PlayEffect batch construction; slot effects are not a second card-level gate.",
    },
    {
        "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/Exam/ExamSequence_NestedType__ExecuteCommandImplAsync_d__111.txt",
        "locator": "ISIL UseHand ExecuteCardCommandImpl 3884/ConsumeCardCost 3896; UsePool IsPlayable 5617/ConsumeCardCost 5712/ExecuteCardCommandImpl 5766",
        "claim": "Ordinary UseHand and UsePool cost/no-cost paths share the native IsPlayable gate before payment/effect/timer command installation; forced/extra are accepted only as these proven UsePool variants.",
    },
    {
        "source": "docs/android-v323-plan3-static-trigger-order.md and src/gkms_tool/plan3_engine.py",
        "locator": "Full Power -> gimmick -> draw; settled ExamStartTurn runtime after draw",
        "claim": "The predicate observes the settled post-draw StartTurn snapshot, not a draw delta or pre-draw state.",
    },
)


def build_plan2_start_turn_condition_threshold_down_native_audit(
    catalog: ConditionThresholdDownCatalog | None = None,
) -> dict[str, Any]:
    """Build the standalone audit object without touching central coverage."""

    catalog = catalog or load_plan2_start_turn_condition_threshold_down_catalog()
    resolution = resolve_condition_threshold_down_trigger(catalog.target)
    contract = resolution.contract
    return {
        "artifact": "plan2_start_turn_condition_threshold_down_native_audit",
        "scope": {
            "cardId": TARGET_CARD_ID,
            "cardName": TARGET_CARD_NAME,
            "planType": PLAN_COMMON,
            "upgrades": list(TARGET_UPGRADES),
            "triggerId": TARGET_TRIGGER_ID,
        },
        "coverageSummary": catalog.summary(),
        "masterTriggerShape": catalog.target.to_dict(),
        "contract": contract.to_dict() if contract is not None else None,
        "nativeConclusion": {
            "currentValue": "ExamParameterModel.JudgeParameter",
            "denominator": "ExamParameterModel.ClearBorder",
            "threshold": TARGET_THRESHOLD_PERMILLE,
            "unit": "permille",
            "signed": True,
            "inclusiveAtExact1000": True,
            "clearBorderZeroAndNegative": "fail-closed",
            "settledSnapshot": StartTurnSnapshot.SETTLED_POST_DRAW.value,
            "gateOrder": [
                "settled post-FullPower/gimmick/post-draw snapshot",
                "card-level ExamExtensions.IsPlayable field gate",
                "payment/cost command",
                "direct Lesson/Block/Timer PlayEffect command build",
                "timer installation/effect execution",
            ],
            "sharedExecutionModes": [
                ExecutionMode.ORDINARY.value,
                ExecutionMode.FORCED.value,
                ExecutionMode.EXTRA.value,
            ],
        },
        "failClosed": {
            "masterShapeDeviation": True,
            "unexpectedNotCheck": True,
            "unexpectedSearchOrEffectCondition": True,
            "wrongPhaseOrDispatch": True,
            "postPaymentOrEffectSnapshot": True,
            "clearBorderNonPositive": True,
        },
        "nativeEvidence": list(NATIVE_EVIDENCE),
        "centralCoverageRebuilt": False,
        "centralCoreModified": False,
        "plan3Modified": False,
        "guiModified": False,
        "effectExecution": "delegated-to-existing-plan2-core",
        "resolution": {
            "supported": resolution.supported,
            "reasons": list(resolution.reasons),
        },
    }


build_native_audit = build_plan2_start_turn_condition_threshold_down_native_audit


__all__ = [
    "CHECK_NOT",
    "ConditionThresholdDownCatalog",
    "ConditionThresholdDownCatalogError",
    "ConditionThresholdDownContract",
    "ConditionThresholdDownEvaluation",
    "ConditionThresholdDownEvaluationInput",
    "ConditionThresholdDownSimulation",
    "ConditionThresholdDownTriggerRow",
    "DIRECT_DISPATCH_PHASE",
    "DEFAULT_AUDIT_PATH",
    "DEFAULT_DATABASE",
    "EvaluationBoundary",
    "EvaluationStage",
    "ExecutionMode",
    "FIELD_CONDITION_THRESHOLD_MULTIPLE_DOWN",
    "LESSON_UNKNOWN",
    "MOVE_UNKNOWN",
    "NATIVE_EVIDENCE",
    "PHASE_EXAM_START_TURN",
    "PLAN_COMMON",
    "StartTurnSnapshot",
    "TARGET_CARD_ID",
    "TARGET_CARD_NAME",
    "TARGET_THRESHOLD_PERMILLE",
    "TARGET_TRIGGER_ID",
    "TARGET_UPGRADES",
    "build_native_audit",
    "build_plan2_start_turn_condition_threshold_down_native_audit",
    "evaluate_condition_threshold_down",
    "load_plan2_start_turn_condition_threshold_down_catalog",
    "resolve_condition_threshold_down_trigger",
    "simulate_condition_threshold_down",
]
