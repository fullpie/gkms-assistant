"""Standalone native contract for the Plan2/Common StartPlay trigger.

The target row is deliberately small: ``e_trigger-start_play`` is an
unconditional ``ProduceExamPhaseType_StartPlay`` trigger.  The surrounding
catalog is not small, however.  A card installs a StatusEnchant wrapper,
which creates a ``TriggerEffectStatusEffect`` listener and later evaluates
the listener at the native StartPlay boundary.  This module models that
boundary and the immutable Master graph without importing ``plan2_state``,
the Plan3 engine, or the central coverage builder.

Listener count/lifecycle primitives are imported from the existing Plan3
standalone trigger module.  They are pure helpers shared by the two native
audits; the Plan2-specific listener below only adapts those helpers to the
StartPlay status-enchant payload.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from .plan3_start_play_trigger import (
    ListenerBudget,
    ListenerConsumption,
    consume_listener,
    effect_group_execution_order,
    normalize_native_limit,
    ordered_effect_ids,
    reset_listener_turn,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "var" / "master.sqlite3"

PLAN_COMMON = "ProducePlanType_Common"
PLAN2 = "ProducePlanType_Plan2"
SUPPORTED_PLAN_TYPES = (PLAN_COMMON, PLAN2)

TARGET_TRIGGER_ID = "e_trigger-start_play"
START_PLAY_TRIGGER_ID = TARGET_TRIGGER_ID
START_PLAY_FAMILY_PREFIX = "e_trigger-start_play-"

PHASE_EXAM_START_TURN = "ProduceExamPhaseType_ExamStartTurn"
PHASE_START_EXAM_PLAY = "ProduceExamPhaseType_StartExamPlay"
PHASE_START_PLAY = "ProduceExamPhaseType_StartPlay"
PHASE_EXAM_START_PLAY_REQUESTED = "ProduceExamPhaseType_ExamStartPlay"
PHASE_EXAM_TURN_TIMER = "ProduceExamPhaseType_ExamTurnTimer"
PHASE_EXAM_TURN_INTERVAL = "ProduceExamPhaseType_ExamTurnInterval"
START_PLAY_PHASE_ORDER = (
    PHASE_EXAM_START_TURN,
    PHASE_START_EXAM_PLAY,
    PHASE_START_PLAY,
    PHASE_EXAM_TURN_TIMER,
    PHASE_EXAM_TURN_INTERVAL,
)
# Keep the name used by the Plan3 audit available for callers comparing the
# two independent phase contracts.
START_TURN_TRIGGER_PHASE_ORDER = START_PLAY_PHASE_ORDER

CHECK_NOT = "ProduceExamTriggerCheckType_Not"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"

FIELD_CARD_PLAY_AGGRESSIVE_UP = (
    "ProduceExamFieldStatusType_CardPlayAggressiveUp"
)
FIELD_PRESERVATION_UP = "ProduceExamFieldStatusType_PreservationUp"

STATUS_ENCHANT_EFFECT_TYPE = "ProduceExamEffectType_ExamStatusEnchant"
CARD_DRAW_EFFECT_TYPE = "ProduceExamEffectType_ExamCardDraw"
FORCE_PLAY_CARD_SEARCH_EFFECT_TYPE = (
    "ProduceExamEffectType_ExamForcePlayCardSearch"
)
PLAYABLE_VALUE_ADD_EFFECT_TYPE = "ProduceExamEffectType_ExamPlayableValueAdd"
AGGRESSIVE_EFFECT_TYPE = "ProduceExamEffectType_ExamCardPlayAggressive"
REVIEW_EFFECT_TYPE = "ProduceExamEffectType_ExamReview"
REVIEW_MULTIPLE_EFFECT_TYPE = "ProduceExamEffectType_ExamReviewMultiple"
BLOCK_EFFECT_TYPE = "ProduceExamEffectType_ExamBlock"

STAMINA_EFFECT_TYPES = frozenset(
    {
        "ProduceExamEffectType_ExamCardStaminaConsumptionChange",
        "ProduceExamEffectType_ExamCardStaminaConsumptionDownSpecify",
        "ProduceExamEffectType_ExamCardStaminaConsumptionReduce",
        "ProduceExamEffectType_ExamSearchPlayCardStaminaConsumptionChange",
        "ProduceExamEffectType_ExamStaminaConsumptionAdd",
        "ProduceExamEffectType_ExamStaminaConsumptionDown",
        "ProduceExamEffectType_ExamStaminaDamage",
        "ProduceExamEffectType_ExamStaminaRecoverAdd",
        "ProduceExamEffectType_ExamStaminaRecoverFix",
        "ProduceExamEffectType_ExamStaminaReduce",
        "ProduceExamEffectType_ExamStaminaReduceChange",
        "ProduceExamEffectType_ExamStaminaReduceFix",
    }
)

TARGET_STATUS_ENCHANT_IDS = (
    "enchant-p_card-02-men-100_007-enc01",
    "enchant-p_card-02-men-100_007-enc02",
    "enchant-p_card-02-sup-3_156-enc01",
)
TARGET_CARD_IDS = (
    "p_card-02-men-100_007",
    "p_card-02-sup-3_156",
)


class StartPlayCatalogError(ValueError):
    """The local Master catalog is absent or outside the audited shape."""


class EvaluationBoundary(str, Enum):
    """Native point at which a StartPlay predicate or child is considered."""

    PHASE_CAPTURE = "phase-trigger-capture"
    PRE_PAYMENT_BUILD = "pre-payment-build"
    POST_PAYMENT_PRE_DIRECT = "post-payment-pre-direct"
    DIRECT_CARD_EFFECT = "direct-card-effect"
    PRE_TURN_CHECK = "pre-turn-check"


@dataclass(frozen=True, slots=True)
class Plan2StartPlayTriggerRow:
    """The typed Master columns needed by the exact trigger contract."""

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
            if not isinstance(getattr(self, name), tuple):
                raise TypeError(f"{name} must be a tuple")
        if type(self.produce_card_search_id) is not str:
            raise TypeError("produce_card_search_id must be a string")
        for name in ("upper_search_count", "lower_search_count"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an integer")
        if type(self.card_move_position_type) is not str:
            raise TypeError("card_move_position_type must be a string")
        if type(self.lesson_type) is not str:
            raise TypeError("lesson_type must be a string")

    @property
    def is_not(self) -> bool:
        return self.field_status_check_types == (CHECK_NOT,)

    def to_dict(self) -> dict[str, object]:
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


TARGET_TRIGGER_ROW = Plan2StartPlayTriggerRow(
    id=TARGET_TRIGGER_ID,
    phase_types=(PHASE_START_PLAY,),
    phase_values=(),
    field_status_check_types=(),
    field_status_types=(),
    field_status_values=(),
    field_status_card_search_ids=(),
    produce_card_search_id="",
    upper_search_count=0,
    lower_search_count=0,
    card_move_position_type=MOVE_UNKNOWN,
    effect_types=(),
    lesson_type=LESSON_UNKNOWN,
)


@dataclass(frozen=True, slots=True)
class Plan2StartPlayTriggerContract:
    row: Plan2StartPlayTriggerRow
    predicate: str = "unconditional at canonical StartPlay"
    evaluation_boundary: str = EvaluationBoundary.PHASE_CAPTURE.value
    requested_spelling: str = PHASE_EXAM_START_PLAY_REQUESTED

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.row.id,
            "phase": self.row.phase_types[0],
            "predicate": self.predicate,
            "evaluationBoundary": self.evaluation_boundary,
            "requestedSpelling": self.requested_spelling,
        }


@dataclass(frozen=True, slots=True)
class TriggerResolution:
    supported: bool
    contract: Plan2StartPlayTriggerContract | None
    reasons: tuple[str, ...] = ()


def _shape_reasons(row: Plan2StartPlayTriggerRow) -> tuple[str, ...]:
    reasons: list[str] = []
    if row.id != TARGET_TRIGGER_ID:
        reasons.append("unknown-trigger-id")
    if row.phase_types != (PHASE_START_PLAY,):
        reasons.append("phase-is-not-canonical-start-play")
    if row.phase_values:
        reasons.append("phase-values-must-be-empty")
    if row.field_status_check_types:
        reasons.append("target-field-check-must-be-empty")
    if row.field_status_types:
        reasons.append("target-field-status-must-be-empty")
    if row.field_status_values:
        reasons.append("target-field-values-must-be-empty")
    if row.field_status_card_search_ids:
        reasons.append("target-field-card-search-must-be-empty")
    if row.produce_card_search_id:
        reasons.append("card-search-must-be-empty")
    if row.upper_search_count != 0 or row.lower_search_count != 0:
        reasons.append("search-count-bounds-must-be-zero")
    if row.card_move_position_type != MOVE_UNKNOWN:
        reasons.append("card-move-condition-is-not-supported")
    if row.effect_types:
        reasons.append("effect-type-condition-must-be-empty")
    if row.lesson_type != LESSON_UNKNOWN:
        reasons.append("lesson-condition-is-not-supported")
    return tuple(reasons)


def resolve_plan2_start_play_trigger(
    row: Plan2StartPlayTriggerRow,
) -> TriggerResolution:
    """Resolve the exact target or a proven same-family field probe.

    The database catalog contains only the unconditional target row.  The
    small field branch is accepted here for typed synthetic probes so the
    evaluator can pin the native value/Not semantics without broadening the
    catalog or claiming support for every StartPlay trigger in Master.
    """

    reasons = _shape_reasons(row)
    if not reasons:
        return TriggerResolution(True, Plan2StartPlayTriggerContract(row))

    field_reasons: list[str] = []
    if not (row.id == TARGET_TRIGGER_ID or row.id.startswith(START_PLAY_FAMILY_PREFIX)):
        field_reasons.append("unknown-start-play-family-id")
    if row.phase_types != (PHASE_START_PLAY,):
        field_reasons.append("phase-is-not-canonical-start-play")
    if row.phase_values:
        field_reasons.append("phase-values-must-be-empty")
    if row.field_status_check_types not in ((), (CHECK_NOT,)):
        field_reasons.append("only-empty-or-single-not-check-is-supported")
    if row.field_status_types not in (
        (FIELD_CARD_PLAY_AGGRESSIVE_UP,),
        (FIELD_PRESERVATION_UP,),
    ):
        field_reasons.append("field-is-outside-the-audited-native-slice")
    if row.field_status_types == (FIELD_CARD_PLAY_AGGRESSIVE_UP,):
        if len(row.field_status_values) != 1:
            field_reasons.append("one-numeric-threshold-is-required")
        elif row.field_status_values[0] < 0:
            field_reasons.append("numeric-threshold-must-be-non-negative")
    elif row.field_status_types == (FIELD_PRESERVATION_UP,):
        if row.field_status_values:
            field_reasons.append("preservation-probe-has-no-numeric-threshold")
    if row.field_status_card_search_ids:
        field_reasons.append("field-card-search-is-not-supported")
    if row.produce_card_search_id:
        field_reasons.append("card-search-is-not-supported")
    if row.upper_search_count != 0 or row.lower_search_count != 0:
        field_reasons.append("search-count-bounds-must-be-zero")
    if row.card_move_position_type != MOVE_UNKNOWN:
        field_reasons.append("card-move-condition-is-not-supported")
    if row.effect_types:
        field_reasons.append("effect-type-condition-is-not-supported")
    if row.lesson_type != LESSON_UNKNOWN:
        field_reasons.append("lesson-condition-is-not-supported")
    if field_reasons:
        return TriggerResolution(False, None, tuple(dict.fromkeys(field_reasons)))
    field_type = row.field_status_types[0]
    return TriggerResolution(
        True,
        Plan2StartPlayTriggerContract(
            row,
            predicate=f"current {field_type} field predicate",
        ),
    )


@dataclass(frozen=True, slots=True)
class StartPlayEvaluationInput:
    """Read-only event inputs.  Extra field values do not alter an empty row."""

    phase: str = PHASE_START_PLAY
    boundary: EvaluationBoundary = EvaluationBoundary.PHASE_CAPTURE
    field_values: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.phase) is not str or not self.phase:
            raise TypeError("phase must be a non-empty string")
        if not isinstance(self.boundary, EvaluationBoundary):
            object.__setattr__(self, "boundary", EvaluationBoundary(self.boundary))
        if not isinstance(self.field_values, Mapping):
            raise TypeError("field_values must be a mapping")


@dataclass(frozen=True, slots=True)
class Plan2StartPlayEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    phase: str
    boundary: EvaluationBoundary
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.trigger_id,
            "supported": self.supported,
            "fires": self.fires,
            "phase": self.phase,
            "boundary": self.boundary.value,
            "reasons": list(self.reasons),
        }


def evaluate_plan2_start_play_trigger(
    row: Plan2StartPlayTriggerRow,
    inputs: StartPlayEvaluationInput = StartPlayEvaluationInput(),
) -> Plan2StartPlayEvaluation:
    """Evaluate the exact unconditional row at one native phase boundary."""

    resolution = resolve_plan2_start_play_trigger(row)
    if not resolution.supported:
        return Plan2StartPlayEvaluation(
            trigger_id=row.id,
            supported=False,
            fires=None,
            phase=inputs.phase,
            boundary=inputs.boundary,
            reasons=resolution.reasons,
        )
    if inputs.boundary is not EvaluationBoundary.PHASE_CAPTURE:
        return Plan2StartPlayEvaluation(
            trigger_id=row.id,
            supported=False,
            fires=None,
            phase=inputs.phase,
            boundary=inputs.boundary,
            reasons=("start-play-trigger-is-not-rechecked-at-this-boundary",),
        )
    if inputs.phase != PHASE_START_PLAY:
        return Plan2StartPlayEvaluation(
            trigger_id=row.id,
            supported=True,
            fires=False,
            phase=inputs.phase,
            boundary=inputs.boundary,
            reasons=("event-phase-mismatch",),
        )
    if resolution.contract is not None and resolution.contract.predicate != (
        "unconditional at canonical StartPlay"
    ):
        field_type = row.field_status_types[0]
        current_value = inputs.field_values.get(field_type)
        if field_type == FIELD_CARD_PLAY_AGGRESSIVE_UP:
            threshold = row.field_status_values[0]
        else:
            threshold = None
        field_result = evaluate_plan2_field_status_predicate(
            field_type,
            current_value,
            threshold=threshold,
            check_not=row.is_not,
        )
        if not field_result.supported:
            return Plan2StartPlayEvaluation(
                trigger_id=row.id,
                supported=False,
                fires=None,
                phase=inputs.phase,
                boundary=inputs.boundary,
                reasons=field_result.reasons,
            )
        return Plan2StartPlayEvaluation(
            trigger_id=row.id,
            supported=True,
            fires=field_result.fires,
            phase=inputs.phase,
            boundary=inputs.boundary,
        )
    return Plan2StartPlayEvaluation(
        trigger_id=row.id,
        supported=True,
        fires=True,
        phase=inputs.phase,
        boundary=inputs.boundary,
    )


@dataclass(frozen=True, slots=True)
class FieldPredicateEvaluation:
    """Small typed probe for the native field/value/Not branch."""

    field_type: str
    current_value: object
    threshold: int | None
    check_not: bool
    comparison: str | None
    supported: bool
    fires: bool | None
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None


def evaluate_plan2_field_status_predicate(
    field_type: str,
    current_value: object,
    *,
    threshold: int | None = None,
    check_not: bool = False,
) -> FieldPredicateEvaluation:
    """Evaluate only native field branches used by the adjacent Plan2 audit.

    The target StartPlay row has no field predicate.  This helper exists to
    pin the shared native rule without pretending that every one of the
    dozens of field types is implemented here: the proven numeric branch is
    signed inclusive ``>=`` and ``Not`` inverts the completed predicate.
    Unknown fields and malformed value shapes return ``fires=None``.
    """

    if type(field_type) is not str or not field_type:
        return FieldPredicateEvaluation(
            str(field_type),
            current_value,
            threshold,
            check_not,
            None,
            False,
            None,
            ("field-type-is-unknown",),
        )
    if type(check_not) is not bool:
        raise TypeError("check_not must be a boolean")
    if field_type == FIELD_CARD_PLAY_AGGRESSIVE_UP:
        if type(current_value) is not int:
            reason = "current-field-value-must-be-a-plain-integer"
        elif type(threshold) is not int or threshold < 0:
            reason = "numeric-field-threshold-is-required"
        else:
            base = current_value >= threshold
            return FieldPredicateEvaluation(
                field_type,
                current_value,
                threshold,
                check_not,
                ">=",
                True,
                not base if check_not else base,
            )
        return FieldPredicateEvaluation(
            field_type,
            current_value,
            threshold,
            check_not,
            ">=",
            False,
            None,
            (reason,),
        )
    if field_type == FIELD_PRESERVATION_UP:
        if threshold is not None:
            return FieldPredicateEvaluation(
                field_type,
                current_value,
                threshold,
                check_not,
                ">=",
                False,
                None,
                ("preservation-target-has-no-numeric-threshold",),
            )
        if type(current_value) is not bool:
            return FieldPredicateEvaluation(
                field_type,
                current_value,
                threshold,
                check_not,
                "presence",
                False,
                None,
                ("preservation-field-value-must-be-a-boolean",),
            )
        return FieldPredicateEvaluation(
            field_type,
            current_value,
            threshold,
            check_not,
            "presence",
            True,
            not current_value if check_not else current_value,
        )
    return FieldPredicateEvaluation(
        field_type,
        current_value,
        threshold,
        check_not,
        None,
        False,
        None,
        ("field-type-is-not-in-the-audited-native-slice",),
    )


def event_index(phase: str) -> int | None:
    try:
        return START_PLAY_PHASE_ORDER.index(phase)
    except ValueError:
        return None


def is_before(first: str, second: str) -> bool:
    first_index = event_index(first)
    second_index = event_index(second)
    return (
        first_index is not None
        and second_index is not None
        and first_index < second_index
    )


@dataclass(frozen=True, slots=True)
class Plan2StartPlayEffectRow:
    id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    effect_group_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "effectType": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "effectCount": self.effect_count,
            "effectTurn": self.effect_turn,
            "statusEnchantId": self.status_enchant_id,
            "chainEffectId": self.chain_effect_id,
            "effectGroupIds": list(self.effect_group_ids),
        }


@dataclass(frozen=True, slots=True)
class Plan2StartPlayStatusEnchantRow:
    id: str
    asset_id: str
    trigger_id: str
    child_effect_ids: tuple[str, ...]
    wrapper_effect_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "assetId": self.asset_id,
            "triggerId": self.trigger_id,
            "childEffectIds": list(self.child_effect_ids),
            "wrapperEffectId": self.wrapper_effect_id,
        }


@dataclass(frozen=True, slots=True)
class Plan2StartPlayStatusProgram:
    status: Plan2StartPlayStatusEnchantRow
    wrapper: Plan2StartPlayEffectRow
    children: tuple[Plan2StartPlayEffectRow, ...]

    @property
    def listener_budget(self) -> ListenerBudget:
        return ListenerBudget.from_master(
            effect_count=self.wrapper.effect_count,
            effect_value1=self.wrapper.value1,
        )

    @property
    def lifetime_turns(self) -> int:
        return self.wrapper.effect_turn

    @property
    def child_effect_ids(self) -> tuple[str, ...]:
        return ordered_effect_ids(tuple(effect.id for effect in self.children))

    @property
    def wrapper_effect_group_execution_order(self) -> tuple[str, ...]:
        return effect_group_execution_order(self.wrapper.effect_group_ids)

    @property
    def child_effect_group_execution_order(self) -> tuple[str, ...]:
        return effect_group_execution_order(
            tuple(
                group_id
                for effect in self.children
                for group_id in effect.effect_group_ids
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.to_dict(),
            "wrapper": self.wrapper.to_dict(),
            "children": [effect.to_dict() for effect in self.children],
            "listenerBudget": {
                "totalRemaining": self.listener_budget.total_remaining,
                "perTurnLimit": self.listener_budget.per_turn_limit,
                "perTurnRemaining": self.listener_budget.per_turn_remaining,
            },
            "lifetimeTurns": self.lifetime_turns,
            "wrapperEffectGroupExecutionOrder": list(
                self.wrapper_effect_group_execution_order
            ),
            "childEffectGroupExecutionOrder": list(
                self.child_effect_group_execution_order
            ),
        }


@dataclass(frozen=True, slots=True)
class Plan2StartPlayCardEffectSlot:
    card_id: str
    upgrade_count: int
    slot_index: int
    effect_id: str
    trigger_id: str
    is_once_play_effect: bool
    effect_group_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade_count,
            "slot": self.slot_index,
            "effectId": self.effect_id,
            "triggerId": self.trigger_id,
            "isOncePlayEffect": self.is_once_play_effect,
            "effectGroupIds": list(self.effect_group_ids),
        }


@dataclass(frozen=True, slots=True)
class Plan2StartPlayCardVersion:
    card_id: str
    upgrade_count: int
    name: str
    plan_type: str
    category: str
    play_trigger_id: str
    effect_slots: tuple[Plan2StartPlayCardEffectSlot, ...]
    direct_from_start_play_fix: bool
    direct_blockers: tuple[str, ...]

    @property
    def target_wrapper_slots(self) -> tuple[Plan2StartPlayCardEffectSlot, ...]:
        return tuple(
            slot
            for slot in self.effect_slots
            if slot.effect_id.startswith("e_effect-exam_status_enchant-")
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade_count,
            "name": self.name,
            "planType": self.plan_type,
            "category": self.category,
            "playTriggerId": self.play_trigger_id,
            "effectSlots": [slot.to_dict() for slot in self.effect_slots],
            "directFromStartPlayFix": self.direct_from_start_play_fix,
            "directBlockers": list(self.direct_blockers),
        }


@dataclass(frozen=True, slots=True)
class Plan2StartPlayCatalog:
    """Immutable exact target graph from the local Master database."""

    trigger_rows: tuple[Plan2StartPlayTriggerRow, ...]
    status_enchants: tuple[Plan2StartPlayStatusEnchantRow, ...]
    programs: tuple[Plan2StartPlayStatusProgram, ...]
    effect_rows: tuple[Plan2StartPlayEffectRow, ...]
    card_versions: tuple[Plan2StartPlayCardVersion, ...]
    universe_plan2_common_versions: int
    universe_plan2_versions: int
    universe_common_versions: int

    @property
    def target(self) -> Plan2StartPlayTriggerRow:
        return self.trigger_rows[0]

    @property
    def standalone_executable_rows(self) -> tuple[Plan2StartPlayTriggerRow, ...]:
        return tuple(
            row for row in self.trigger_rows if resolve_plan2_start_play_trigger(row).supported
        )

    @property
    def affected_versions(self) -> tuple[Plan2StartPlayCardVersion, ...]:
        return self.card_versions

    @property
    def direct_versions(self) -> tuple[Plan2StartPlayCardVersion, ...]:
        return tuple(card for card in self.card_versions if card.direct_from_start_play_fix)

    def trigger(self, trigger_id: str) -> Plan2StartPlayTriggerRow:
        for row in self.trigger_rows:
            if row.id == trigger_id:
                return row
        raise KeyError(trigger_id)

    def program(self, status_id: str) -> Plan2StartPlayStatusProgram:
        for program in self.programs:
            if program.status.id == status_id:
                return program
        raise KeyError(status_id)

    def direct_versions_for_trigger(
        self, trigger_id: str
    ) -> tuple[Plan2StartPlayCardVersion, ...]:
        if trigger_id != TARGET_TRIGGER_ID:
            return ()
        return self.direct_versions

    def affected_versions_for_trigger(
        self, trigger_id: str
    ) -> tuple[Plan2StartPlayCardVersion, ...]:
        if trigger_id != TARGET_TRIGGER_ID:
            return ()
        return self.affected_versions

    def summary(self) -> dict[str, object]:
        return {
            "targetTriggerId": TARGET_TRIGGER_ID,
            "triggerRowCount": len(self.trigger_rows),
            "standaloneExecutableTriggerRowCount": len(
                self.standalone_executable_rows
            ),
            "statusEnchantRowCount": len(self.status_enchants),
            "statusProgramCount": len(self.programs),
            "effectRowCount": len(self.effect_rows),
            "affectedCardVersionCount": len(self.affected_versions),
            "affectedUniqueCardCount": len(
                {card.card_id for card in self.affected_versions}
            ),
            "directCardVersionCount": len(self.direct_versions),
            "directUniqueCardCount": len({card.card_id for card in self.direct_versions}),
            "directCardIds": [card.card_id for card in self.direct_versions],
            "affectedCardIds": [card.card_id for card in self.affected_versions],
            "universePlan2CommonVersionCount": self.universe_plan2_common_versions,
            "universePlan2VersionCount": self.universe_plan2_versions,
            "universeCommonVersionCount": self.universe_common_versions,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary(),
            "triggerRows": [row.to_dict() for row in self.trigger_rows],
            "statusEnchants": [row.to_dict() for row in self.status_enchants],
            "programs": [program.to_dict() for program in self.programs],
            "effectRows": [row.to_dict() for row in self.effect_rows],
            "cardVersions": [card.to_dict() for card in self.card_versions],
        }


def _json_list(value: object, label: str) -> list[object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise StartPlayCatalogError(f"{label}: invalid JSON") from error
    if not isinstance(parsed, list):
        raise StartPlayCatalogError(f"{label}: expected JSON list")
    return parsed


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_list(value, label)
    if any(type(item) is not str for item in parsed):
        raise StartPlayCatalogError(f"{label}: expected string entries")
    return tuple(parsed)


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_list(value, label)
    if any(type(item) is not int for item in parsed):
        raise StartPlayCatalogError(f"{label}: expected integer entries")
    return tuple(parsed)


def _trigger_from_row(row: sqlite3.Row) -> Plan2StartPlayTriggerRow:
    trigger_id = str(row["id"])
    return Plan2StartPlayTriggerRow(
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
        produce_card_search_id=str(row["produce_card_search_id"]),
        upper_search_count=int(row["upper_search_count"]),
        lower_search_count=int(row["lower_search_count"]),
        card_move_position_type=str(row["card_move_position_type"]),
        effect_types=_string_tuple(row["effect_types_json"], f"{trigger_id}.effectTypes"),
        lesson_type=str(row["lesson_type"]),
    )


def _effect_groups(raw_json: object, effect_id: str) -> tuple[str, ...]:
    try:
        raw = json.loads(str(raw_json))
    except json.JSONDecodeError as error:
        raise StartPlayCatalogError(f"{effect_id}: invalid raw_json") from error
    if not isinstance(raw, dict):
        raise StartPlayCatalogError(f"{effect_id}: raw_json must be an object")
    groups = raw.get("effectGroupIds", [])
    if not isinstance(groups, list) or any(
        type(group) is not str or not group for group in groups
    ):
        raise StartPlayCatalogError(f"{effect_id}: invalid effectGroupIds")
    return tuple(groups)


def _effect_from_row(row: sqlite3.Row) -> Plan2StartPlayEffectRow:
    effect_id = str(row["id"])
    return Plan2StartPlayEffectRow(
        id=effect_id,
        effect_type=str(row["effect_type"]),
        value1=int(row["value1"]),
        value2=int(row["value2"]),
        effect_count=int(row["effect_count"]),
        effect_turn=int(row["effect_turn"]),
        status_enchant_id=str(row["status_enchant_id"]),
        chain_effect_id=str(row["chain_effect_id"]),
        effect_group_ids=_effect_groups(row["raw_json"], effect_id),
    )


def _status_from_row(
    row: sqlite3.Row,
    wrapper_effect_id: str,
) -> Plan2StartPlayStatusEnchantRow:
    status_id = str(row["id"])
    return Plan2StartPlayStatusEnchantRow(
        id=status_id,
        asset_id=str(row["asset_id"]),
        trigger_id=str(row["produce_exam_trigger_id"]),
        child_effect_ids=_string_tuple(
            row["produce_exam_effect_ids_json"],
            f"{status_id}.produceExamEffectIds",
        ),
        wrapper_effect_id=wrapper_effect_id,
    )


def _card_slots(
    card_id: str,
    upgrade_count: int,
    raw_effects: object,
    effect_groups: Mapping[str, tuple[str, ...]],
) -> tuple[Plan2StartPlayCardEffectSlot, ...]:
    if not isinstance(raw_effects, list):
        raise StartPlayCatalogError(f"{card_id}+{upgrade_count}: effects must be list")
    slots: list[Plan2StartPlayCardEffectSlot] = []
    for index, raw in enumerate(raw_effects):
        if not isinstance(raw, dict):
            raise StartPlayCatalogError(f"{card_id}+{upgrade_count}: invalid slot")
        effect_id = raw.get("produceExamEffectId", "")
        trigger_id = raw.get("produceExamTriggerId", "")
        once = raw.get("isOncePlayEffect")
        if type(effect_id) is not str or type(trigger_id) is not str:
            raise StartPlayCatalogError(f"{card_id}+{upgrade_count}: invalid slot IDs")
        if type(once) is not bool:
            raise StartPlayCatalogError(
                f"{card_id}+{upgrade_count}: isOncePlayEffect must be bool"
            )
        if effect_id and effect_id not in effect_groups:
            raise StartPlayCatalogError(
                f"{card_id}+{upgrade_count}: missing effect row {effect_id}"
            )
        slots.append(
            Plan2StartPlayCardEffectSlot(
                card_id=card_id,
                upgrade_count=upgrade_count,
                slot_index=index,
                effect_id=effect_id,
                trigger_id=trigger_id,
                is_once_play_effect=once,
                effect_group_ids=effect_groups.get(effect_id, ()),
            )
        )
    return tuple(slots)


def _open_master_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise StartPlayCatalogError(f"Master database not found: {path}")
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise StartPlayCatalogError(f"cannot open Master read-only: {path}") from error
    connection.row_factory = sqlite3.Row
    return connection


def load_plan2_start_play_status_program(
    wrapper_effect_id: str,
    database: Path | str = DEFAULT_DATABASE,
) -> Plan2StartPlayStatusProgram:
    """Load one exact unconditional StartPlay StatusEnchant program.

    Unlike :func:`load_plan2_start_play_trigger_catalog`, this narrow loader
    is source-agnostic: cards, items, and drinks may all install the same
    native TriggerEffectStatusEffect.  It still admits only the already
    proven unconditional ``e_trigger-start_play`` row and direct CardDraw
    children, so callers reuse one phase/lifecycle implementation without
    broadening the audited trigger family.
    """

    if not isinstance(wrapper_effect_id, str) or not wrapper_effect_id:
        raise StartPlayCatalogError("wrapper_effect_id must be non-empty")
    connection = _open_master_read_only(Path(database))
    with closing(connection):
        trigger_raw = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id=?",
            (TARGET_TRIGGER_ID,),
        ).fetchone()
        if trigger_raw is None:
            raise StartPlayCatalogError(
                f"missing trigger row: {TARGET_TRIGGER_ID}"
            )
        trigger = _trigger_from_row(trigger_raw)
        resolution = resolve_plan2_start_play_trigger(trigger)
        if not resolution.supported:
            raise StartPlayCatalogError(
                f"target trigger shape is not exact: {resolution.reasons}"
            )

        wrapper_raw = connection.execute(
            "SELECT * FROM effect WHERE id=?",
            (wrapper_effect_id,),
        ).fetchone()
        if wrapper_raw is None:
            raise StartPlayCatalogError(
                f"missing wrapper effect row: {wrapper_effect_id}"
            )
        wrapper = _effect_from_row(wrapper_raw)
        if (
            wrapper.effect_type != STATUS_ENCHANT_EFFECT_TYPE
            or not wrapper.status_enchant_id
            or wrapper.value1 < 0
            or wrapper.value2 != 0
            or wrapper.effect_count < 0
            or wrapper.effect_turn == 0
            or wrapper.effect_turn < -1
            or wrapper.chain_effect_id
        ):
            raise StartPlayCatalogError(
                f"unsupported StartPlay wrapper shape: {wrapper_effect_id}"
            )

        status_raw = connection.execute(
            """
            SELECT id, asset_id, produce_exam_trigger_id,
                   produce_exam_effect_ids_json
              FROM produce_exam_status_enchant
             WHERE id=?
            """,
            (wrapper.status_enchant_id,),
        ).fetchone()
        if status_raw is None:
            raise StartPlayCatalogError(
                f"missing status row: {wrapper.status_enchant_id}"
            )
        status = _status_from_row(status_raw, wrapper_effect_id)
        if status.trigger_id != TARGET_TRIGGER_ID:
            raise StartPlayCatalogError(
                f"{status.id}: trigger mismatch: {status.trigger_id}"
            )
        if not status.child_effect_ids:
            raise StartPlayCatalogError(f"{status.id}: child list is empty")
        placeholders = ",".join("?" for _ in status.child_effect_ids)
        child_rows = connection.execute(
            f"SELECT * FROM effect WHERE id IN ({placeholders})",
            tuple(status.child_effect_ids),
        ).fetchall()
        child_by_id = {str(row["id"]): row for row in child_rows}
        missing = tuple(
            child_id
            for child_id in status.child_effect_ids
            if child_id not in child_by_id
        )
        if missing:
            raise StartPlayCatalogError(
                f"missing status child effects: {list(missing)}"
            )
        children = tuple(
            _effect_from_row(child_by_id[child_id])
            for child_id in status.child_effect_ids
        )
        for child in children:
            if (
                child.effect_type != CARD_DRAW_EFFECT_TYPE
                or child.value1 < 1
                or child.value2 != 0
                or child.effect_count != 0
                or child.effect_turn != 0
                or child.status_enchant_id
                or child.chain_effect_id
            ):
                raise StartPlayCatalogError(
                    f"unsupported StartPlay child shape: {child.id}"
                )
        return Plan2StartPlayStatusProgram(status, wrapper, children)


def load_plan2_start_play_trigger_catalog(
    database: Path | str = DEFAULT_DATABASE,
) -> Plan2StartPlayCatalog:
    """Load the exact Plan2/Common card/status graph from local Master.

    The loader intentionally rejects a changed target universe.  It does not
    read or rebuild any coverage snapshot; ``direct_from_start_play_fix`` is
    derived only from the adjacent child-effect contract (CardDraw is
    already available, ForcePlayCardSearch remains a blocker).
    """

    path = Path(database)
    connection = _open_master_read_only(path)
    with closing(connection):
        trigger_raw = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id=?", (TARGET_TRIGGER_ID,)
        ).fetchone()
        if trigger_raw is None:
            raise StartPlayCatalogError(f"missing trigger row: {TARGET_TRIGGER_ID}")
        trigger = _trigger_from_row(trigger_raw)
        trigger_resolution = resolve_plan2_start_play_trigger(trigger)
        if not trigger_resolution.supported:
            raise StartPlayCatalogError(
                f"target trigger shape is not exact: {trigger_resolution.reasons}"
            )

        status_raw_rows = connection.execute(
            """
            SELECT id, asset_id, produce_exam_trigger_id,
                   produce_exam_effect_ids_json
              FROM produce_exam_status_enchant
             WHERE produce_exam_trigger_id=?
             ORDER BY id
            """,
            (TARGET_TRIGGER_ID,),
        ).fetchall()
        if not status_raw_rows:
            raise StartPlayCatalogError("no status-enchant rows for target trigger")
        status_ids = tuple(str(row["id"]) for row in status_raw_rows)

        status_placeholders = ",".join("?" for _ in status_ids)
        wrapper_rows = connection.execute(
            f"""
            SELECT * FROM effect
             WHERE status_enchant_id IN ({status_placeholders})
               AND effect_type=?
             ORDER BY id
            """,
            (*status_ids, STATUS_ENCHANT_EFFECT_TYPE),
        ).fetchall()
        wrappers_by_status: dict[str, list[sqlite3.Row]] = {}
        for row in wrapper_rows:
            wrappers_by_status.setdefault(str(row["status_enchant_id"]), []).append(row)

        card_raw_rows = connection.execute(
            """
            SELECT id, upgrade_count, name, plan_type, category,
                   play_trigger_id, play_effects_json
              FROM card
             WHERE plan_type IN (?, ?)
             ORDER BY id, upgrade_count
            """,
            SUPPORTED_PLAN_TYPES,
        ).fetchall()
        parsed_cards: list[tuple[sqlite3.Row, list[dict[str, object]]]] = []
        wrapper_effect_ids = {
            str(row["id"])
            for rows in wrappers_by_status.values()
            for row in rows
        }
        for card in card_raw_rows:
            card_id = str(card["id"])
            effects = _json_list(card["play_effects_json"], f"{card_id}.playEffects")
            normalized: list[dict[str, object]] = []
            has_target_ref = str(card["play_trigger_id"]) == TARGET_TRIGGER_ID
            for raw in effects:
                if not isinstance(raw, dict):
                    raise StartPlayCatalogError(f"{card_id}: invalid play effect")
                effect_id = raw.get("produceExamEffectId", "")
                slot_trigger = raw.get("produceExamTriggerId", "")
                if type(effect_id) is not str or type(slot_trigger) is not str:
                    raise StartPlayCatalogError(f"{card_id}: invalid play effect IDs")
                normalized.append(raw)
                has_target_ref |= effect_id in wrapper_effect_ids
                has_target_ref |= slot_trigger == TARGET_TRIGGER_ID
            if has_target_ref:
                parsed_cards.append((card, normalized))

        if not parsed_cards:
            raise StartPlayCatalogError("no Plan2/Common card references target trigger")

        relevant_card_effect_ids = {
            str(raw["produceExamEffectId"])
            for _, effects in parsed_cards
            for raw in effects
            if str(raw["produceExamEffectId"])
        }
        effect_ids = relevant_card_effect_ids | wrapper_effect_ids
        effect_placeholders = ",".join("?" for _ in effect_ids)
        effect_raw_rows = connection.execute(
            f"SELECT * FROM effect WHERE id IN ({effect_placeholders})",
            tuple(sorted(effect_ids)),
        ).fetchall()
        effect_by_id_raw = {str(row["id"]): row for row in effect_raw_rows}
        missing = sorted(effect_ids - set(effect_by_id_raw))
        if missing:
            raise StartPlayCatalogError(f"missing card/wrapper effect rows: {missing}")

        relevant_status_ids: set[str] = set()
        for effect_id in relevant_card_effect_ids:
            status_id = str(effect_by_id_raw[effect_id]["status_enchant_id"])
            if status_id in status_ids:
                relevant_status_ids.add(status_id)
        expected_status_ids = set(TARGET_STATUS_ENCHANT_IDS)
        if relevant_status_ids != expected_status_ids:
            raise StartPlayCatalogError(
                "target status rows changed: "
                f"expected={sorted(expected_status_ids)} "
                f"actual={sorted(relevant_status_ids)}"
            )

        child_effect_ids = {
            child_id
            for row in status_raw_rows
            if str(row["id"]) in relevant_status_ids
            for child_id in _string_tuple(
                row["produce_exam_effect_ids_json"],
                f"{row['id']}.produceExamEffectIds",
            )
        }
        child_placeholders = ",".join("?" for _ in child_effect_ids)
        child_rows = connection.execute(
            f"SELECT * FROM effect WHERE id IN ({child_placeholders})",
            tuple(sorted(child_effect_ids)),
        ).fetchall()
        effect_by_id_raw.update({str(row["id"]): row for row in child_rows})
        missing_children = sorted(child_effect_ids - set(effect_by_id_raw))
        if missing_children:
            raise StartPlayCatalogError(f"missing status child effects: {missing_children}")

        effect_by_id = {
            effect_id: _effect_from_row(row)
            for effect_id, row in effect_by_id_raw.items()
            if effect_id in relevant_card_effect_ids or effect_id in child_effect_ids
        }
        effect_groups = {
            effect_id: row.effect_group_ids for effect_id, row in effect_by_id.items()
        }

        programs: list[Plan2StartPlayStatusProgram] = []
        status_enchants: list[Plan2StartPlayStatusEnchantRow] = []
        for status_id in TARGET_STATUS_ENCHANT_IDS:
            status_raw = next(
                (row for row in status_raw_rows if str(row["id"]) == status_id),
                None,
            )
            if status_raw is None:
                raise StartPlayCatalogError(f"missing target status row: {status_id}")
            wrapper_rows_for_status = wrappers_by_status.get(status_id, [])
            relevant_wrappers = [
                row for row in wrapper_rows_for_status if str(row["id"]) in relevant_card_effect_ids
            ]
            if len(relevant_wrappers) != 1:
                raise StartPlayCatalogError(
                    f"{status_id}: expected one exact wrapper, got {len(relevant_wrappers)}"
                )
            wrapper = _effect_from_row(relevant_wrappers[0])
            if wrapper.value1 < 0 or wrapper.value2 != 0 or wrapper.effect_count < 0:
                raise StartPlayCatalogError(f"{status_id}: invalid listener limit payload")
            if wrapper.effect_turn == 0 or wrapper.effect_turn < -1:
                raise StartPlayCatalogError(f"{status_id}: invalid lifetime payload")
            status = _status_from_row(status_raw, wrapper.id)
            if status.trigger_id != TARGET_TRIGGER_ID:
                raise StartPlayCatalogError(f"{status_id}: trigger mismatch")
            children = tuple(
                effect_by_id[child_id] for child_id in status.child_effect_ids
            )
            if any(child.chain_effect_id for child in children):
                raise StartPlayCatalogError(
                    f"{status_id}: chained child shape is outside the audit"
                )
            status_enchants.append(status)
            programs.append(Plan2StartPlayStatusProgram(status, wrapper, children))

        card_versions: list[Plan2StartPlayCardVersion] = []
        program_by_status = {program.status.id: program for program in programs}
        for card, effects in parsed_cards:
            card_id = str(card["id"])
            upgrade_count = int(card["upgrade_count"])
            slots = _card_slots(card_id, upgrade_count, effects, effect_groups)
            target_status_ids_for_card = tuple(
                dict.fromkeys(
                    effect_by_id[slot.effect_id].status_enchant_id
                    for slot in slots
                    if slot.effect_id in effect_by_id
                    and effect_by_id[slot.effect_id].status_enchant_id
                    in program_by_status
                )
            )
            blockers: list[str] = []
            for status_id in target_status_ids_for_card:
                program = program_by_status[status_id]
                for child in program.children:
                    if child.effect_type != CARD_DRAW_EFFECT_TYPE:
                        blockers.append(f"C:effect:{child.effect_type}")
            direct_blockers = tuple(dict.fromkeys(blockers))
            card_versions.append(
                Plan2StartPlayCardVersion(
                    card_id=card_id,
                    upgrade_count=upgrade_count,
                    name=str(card["name"]),
                    plan_type=str(card["plan_type"]),
                    category=str(card["category"]),
                    play_trigger_id=str(card["play_trigger_id"]),
                    effect_slots=slots,
                    direct_from_start_play_fix=not direct_blockers,
                    direct_blockers=direct_blockers,
                )
            )

        expected_card_keys = {
            ("p_card-02-men-100_007", 0),
            *(('p_card-02-sup-3_156', upgrade) for upgrade in range(4)),
        }
        actual_card_keys = {(card.card_id, card.upgrade_count) for card in card_versions}
        if actual_card_keys != expected_card_keys:
            raise StartPlayCatalogError(
                "affected card universe changed: "
                f"expected={sorted(expected_card_keys)} actual={sorted(actual_card_keys)}"
            )

        universe_counts = Counter(
            {
                str(row["plan_type"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT plan_type, COUNT(*) AS count
                      FROM card
                     WHERE plan_type IN (?, ?)
                     GROUP BY plan_type
                    """,
                    SUPPORTED_PLAN_TYPES,
                )
            }
        )

    return Plan2StartPlayCatalog(
        trigger_rows=(trigger,),
        status_enchants=tuple(status_enchants),
        programs=tuple(programs),
        effect_rows=tuple(effect_by_id[key] for key in sorted(effect_by_id)),
        card_versions=tuple(card_versions),
        universe_plan2_common_versions=sum(universe_counts.values()),
        universe_plan2_versions=universe_counts[PLAN2],
        universe_common_versions=universe_counts[PLAN_COMMON],
    )


load_plan2_start_play_catalog = load_plan2_start_play_trigger_catalog


@dataclass(frozen=True, slots=True)
class Plan2StartPlayListener:
    """One installed TriggerEffectStatusEffect, independent of Plan2State."""

    status_uid: int
    status_enchant_id: str
    trigger_id: str
    child_effects: tuple[Plan2StartPlayEffectRow, ...]
    wrapper_effect_group_ids: tuple[str, ...]
    turn: int
    budget: ListenerBudget
    turn_count: int = 0
    is_passing_turn_start: bool = False

    def __post_init__(self) -> None:
        if type(self.status_uid) is not int or self.status_uid < 1:
            raise ValueError("status_uid must be a positive integer")
        if type(self.status_enchant_id) is not str or not self.status_enchant_id:
            raise ValueError("status_enchant_id must be non-empty")
        if self.trigger_id != TARGET_TRIGGER_ID:
            raise ValueError("listener trigger must be the exact StartPlay trigger")
        if type(self.turn) is not int or self.turn == 0 or self.turn < -1:
            raise ValueError("turn must be -1 or a positive integer")
        if type(self.turn_count) is not int or self.turn_count < 0:
            raise ValueError("turn_count must be non-negative")
        if type(self.is_passing_turn_start) is not bool:
            raise TypeError("is_passing_turn_start must be a boolean")
        if not isinstance(self.budget, ListenerBudget):
            raise TypeError("budget must be the shared ListenerBudget")
        effects = tuple(self.child_effects)
        if any(not isinstance(effect, Plan2StartPlayEffectRow) for effect in effects):
            raise TypeError("child_effects must contain typed effect rows")
        object.__setattr__(self, "child_effects", effects)
        groups = tuple(self.wrapper_effect_group_ids)
        if any(type(group) is not str or not group for group in groups):
            raise ValueError("wrapper_effect_group_ids must contain non-empty strings")
        object.__setattr__(self, "wrapper_effect_group_ids", groups)

    @classmethod
    def from_program(
        cls,
        status_uid: int,
        program: Plan2StartPlayStatusProgram,
    ) -> "Plan2StartPlayListener":
        return cls(
            status_uid=status_uid,
            status_enchant_id=program.status.id,
            trigger_id=program.status.trigger_id,
            child_effects=program.children,
            wrapper_effect_group_ids=program.wrapper.effect_group_ids,
            turn=program.lifetime_turns,
            budget=program.listener_budget,
        )

    @property
    def can_trigger(self) -> bool:
        return self.budget.can_fire

    @property
    def child_effect_ids(self) -> tuple[str, ...]:
        return ordered_effect_ids(tuple(effect.id for effect in self.child_effects))

    @property
    def wrapper_effect_group_execution_order(self) -> tuple[str, ...]:
        return effect_group_execution_order(self.wrapper_effect_group_ids)

    @property
    def child_effect_group_execution_order(self) -> tuple[str, ...]:
        return effect_group_execution_order(
            tuple(
                group_id
                for effect in self.child_effects
                for group_id in effect.effect_group_ids
            )
        )


def simulate_install_plan2_start_play_listener(
    status_uid: int,
    program: Plan2StartPlayStatusProgram,
) -> Plan2StartPlayListener:
    """Install a future listener; the child is not executed on install."""

    return Plan2StartPlayListener.from_program(status_uid, program)


@dataclass(frozen=True, slots=True)
class StartPlayListenerConsumption:
    before: Plan2StartPlayListener
    after: Plan2StartPlayListener | None
    native: ListenerConsumption

    @property
    def fired(self) -> bool:
        return self.native.fired

    @property
    def removed(self) -> bool:
        return self.native.removed


def consume_plan2_start_play_listener(
    listener: Plan2StartPlayListener,
    *,
    predicate_matched: bool,
) -> StartPlayListenerConsumption:
    """Adapt the shared native count helper to one StartPlay listener."""

    native = consume_listener(listener.budget, predicate_matched=predicate_matched)
    after = (
        None
        if native.removed
        else replace(listener, budget=native.after)
    )
    return StartPlayListenerConsumption(listener, after, native)


@dataclass(frozen=True, slots=True)
class StartPlayActivation:
    listener_before: Plan2StartPlayListener
    listener_after: Plan2StartPlayListener | None
    consumption: ListenerConsumption
    child_effects: tuple[Plan2StartPlayEffectRow, ...]

    @property
    def child_effect_ids(self) -> tuple[str, ...]:
        return ordered_effect_ids(tuple(effect.id for effect in self.child_effects))


@dataclass(frozen=True, slots=True)
class StartPlayCapture:
    before_listeners: tuple[Plan2StartPlayListener, ...]
    evaluations: tuple[Plan2StartPlayEvaluation, ...]
    activations: tuple[StartPlayActivation, ...]
    after_count_spend: tuple[Plan2StartPlayListener, ...]
    event_trace: tuple[str, ...]

    @property
    def child_effects_in_order(self) -> tuple[Plan2StartPlayEffectRow, ...]:
        return tuple(
            effect
            for activation in self.activations
            for effect in activation.child_effects
        )

    @property
    def child_effect_ids_in_order(self) -> tuple[str, ...]:
        return ordered_effect_ids(
            tuple(effect.id for effect in self.child_effects_in_order)
        )

    def execute_children(self) -> tuple[Plan2StartPlayEffectRow, ...]:
        """Return native child order; applying effect formulas is out of scope."""

        return self.child_effects_in_order


def capture_plan2_start_play(
    listeners: Sequence[Plan2StartPlayListener],
    *,
    row: Plan2StartPlayTriggerRow = TARGET_TRIGGER_ROW,
    inputs: StartPlayEvaluationInput = StartPlayEvaluationInput(),
) -> StartPlayCapture:
    """Capture and spend eligible listeners from one stable active snapshot."""

    before = tuple(listeners)
    remaining: list[Plan2StartPlayListener] = []
    evaluations: list[Plan2StartPlayEvaluation] = []
    activations: list[StartPlayActivation] = []
    trace = [f"capture:{inputs.phase}"]
    for listener in before:
        evaluation = evaluate_plan2_start_play_trigger(row, inputs)
        evaluations.append(evaluation)
        result = consume_plan2_start_play_listener(
            listener,
            predicate_matched=evaluation.fires is True,
        )
        if result.fired:
            trace.append(f"spend-count:{listener.status_uid}")
            activations.append(
                StartPlayActivation(
                    listener_before=listener,
                    listener_after=result.after,
                    consumption=result.native,
                    child_effects=listener.child_effects,
                )
            )
        if result.after is not None:
            remaining.append(result.after)
    return StartPlayCapture(
        before_listeners=before,
        evaluations=tuple(evaluations),
        activations=tuple(activations),
        after_count_spend=tuple(remaining),
        event_trace=tuple(trace),
    )


@dataclass(frozen=True, slots=True)
class StartPlayTurnTransition:
    before: Plan2StartPlayListener
    after: Plan2StartPlayListener | None
    spent: bool
    expired: bool
    fresh: bool
    permanent: bool


def advance_plan2_start_play_listener_turn(
    listener: Plan2StartPlayListener,
) -> StartPlayTurnTransition:
    """Apply one native TurnStart lifetime boundary to one listener."""

    next_listener = replace(
        listener,
        turn_count=listener.turn_count + 1,
        budget=reset_listener_turn(listener.budget),
        is_passing_turn_start=True,
    )
    if listener.turn == -1:
        return StartPlayTurnTransition(
            listener, next_listener, False, False, False, True
        )
    if not listener.is_passing_turn_start:
        return StartPlayTurnTransition(
            listener, next_listener, False, False, True, False
        )
    next_turn = listener.turn - 1
    if next_turn <= 0:
        return StartPlayTurnTransition(listener, None, True, True, False, False)
    return StartPlayTurnTransition(
        listener,
        replace(next_listener, turn=next_turn),
        True,
        False,
        False,
        False,
    )


@dataclass(frozen=True, slots=True)
class Plan2ChildEvaluationTiming:
    """Where a named Plan2 effect predicate/child is observed."""

    effect_type: str
    supported: bool
    predicate_boundary: str | None
    execution_boundary: str | None
    rechecked_after_capture: bool | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "effectType": self.effect_type,
            "supported": self.supported,
            "predicateBoundary": self.predicate_boundary,
            "executionBoundary": self.execution_boundary,
            "recheckedAfterCapture": self.rechecked_after_capture,
            "reason": self.reason,
        }


def resolve_plan2_child_evaluation_timing(
    effect_type: str,
    *,
    listener_phase: str | None = None,
    direct_card_trigger: bool = False,
    direct_effect_slot: bool = False,
) -> Plan2ChildEvaluationTiming:
    """Return only the transaction boundary proved by the adjacent runtimes."""

    known = {
        AGGRESSIVE_EFFECT_TYPE,
        REVIEW_EFFECT_TYPE,
        REVIEW_MULTIPLE_EFFECT_TYPE,
        BLOCK_EFFECT_TYPE,
        *STAMINA_EFFECT_TYPES,
    }
    if effect_type not in known:
        return Plan2ChildEvaluationTiming(
            effect_type,
            False,
            None,
            None,
            None,
            "unknown-effect-shape",
        )
    if listener_phase == "ProduceExamPhaseType_ExamCardPlay":
        return Plan2ChildEvaluationTiming(
            effect_type,
            True,
            EvaluationBoundary.PRE_PAYMENT_BUILD.value,
            EvaluationBoundary.POST_PAYMENT_PRE_DIRECT.value,
            False,
            "ExamCardPlay listener candidates are captured/spent before payment; children execute later.",
        )
    if listener_phase == "ProduceExamPhaseType_ExamEndTurn":
        return Plan2ChildEvaluationTiming(
            effect_type,
            True,
            "ExamEndTurn phase command build",
            EvaluationBoundary.PRE_TURN_CHECK.value,
            False,
            "ExamEndTurn listeners execute before the TurnCheck review score.",
        )
    if listener_phase == PHASE_START_PLAY:
        return Plan2ChildEvaluationTiming(
            effect_type,
            True,
            EvaluationBoundary.PHASE_CAPTURE.value,
            EvaluationBoundary.PHASE_CAPTURE.value,
            False,
            "StartPlay status children execute in the captured Master order.",
        )
    if direct_card_trigger:
        return Plan2ChildEvaluationTiming(
            effect_type,
            True,
            EvaluationBoundary.PRE_PAYMENT_BUILD.value,
            EvaluationBoundary.DIRECT_CARD_EFFECT.value,
            False,
            "Direct card trigger is selected while the card command is built; it is not re-run later.",
        )
    if direct_effect_slot:
        return Plan2ChildEvaluationTiming(
            effect_type,
            True,
            EvaluationBoundary.DIRECT_CARD_EFFECT.value,
            EvaluationBoundary.DIRECT_CARD_EFFECT.value,
            False,
            "Direct card effect slots retain Master order after payment.",
        )
    return Plan2ChildEvaluationTiming(
        effect_type,
        False,
        None,
        None,
        None,
        "effect context is not specified; fail closed",
    )


def ordered_start_play_child_effects(
    capture: StartPlayCapture,
) -> tuple[Plan2StartPlayEffectRow, ...]:
    return capture.execute_children()


ANDROID_PLAN2_START_PLAY_TRIGGER_EVIDENCE = {
    "phase_name": {
        "canonical_native_token": PHASE_START_PLAY,
        "requested_spelling": PHASE_EXAM_START_PLAY_REQUESTED,
        "requested_spelling_is_enum_token": False,
        "separate_token": PHASE_START_EXAM_PLAY,
    },
    "native_event_order": {
        "trigger_phase_order": list(START_PLAY_PHASE_ORDER),
        "boundary_claim": "ExamStartTurn precedes StartPlay; StartExamPlay is separate; TurnTimer and TurnInterval follow StartPlay.",
        "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/Exam/ExamSequence_NestedType__ExamLoopTaskAsync_d__94.txt",
    },
    "trigger_row": {
        "id": TARGET_TRIGGER_ID,
        "phase_types": [PHASE_START_PLAY],
        "phase_values": [],
        "field_status_check_types": [],
        "field_status_types": [],
        "field_status_values": [],
        "field_status_produce_card_search_ids": [],
        "produce_card_search_id": "",
        "upper_search_count": 0,
        "lower_search_count": 0,
        "card_move_position_type": MOVE_UNKNOWN,
        "effect_types": [],
        "lesson_type": LESSON_UNKNOWN,
        "predicate": "unconditional at canonical StartPlay",
    },
    "predicate_contract": {
        "field_status_type": "Selects the native field branch; the target has no field branch.",
        "field_status_values": "The first value is the signed branch threshold; absent values mean no numeric threshold.",
        "comparison": "signed >=",
        "not": "ProduceExamTriggerCheckType_Not inverts the completed field predicate.",
        "target_value2": "There is no trigger value2 column; effectValue2=0 is payload, not a trigger threshold.",
        "unknown_shape": "unsupported field/search/lesson/phase shapes return fires=None.",
    },
    "listener_contract": {
        "listener_type": "TriggerEffectStatusEffect",
        "phase_capture_symbol": "ExamStatusEffectCollection.GetPhaseEffectList",
        "phase_capture_callsites": "ExamLoopTaskAsync ISIL original instructions 810/853 for StartExamPlay/StartPlay",
        "active_listener_filter_symbol": "ExamStatusEffectCollection.GetTriggerEffectList",
        "active_listener_filter_rva": "0x7EA1D74",
        "count_symbol": "TriggerEffectStatusEffect.SpendCount",
        "count_rva": "0x7EAE70C",
        "installation_symbol": "StatusEnchantEffectExecutor.ExecuteEffect",
        "installation_rva": "0x7E8FA08",
        "count_rule": "effectCount/value1 normalize 0 to unlimited; finite total is removed at zero; finite per-turn exhaustion is retained until TurnStart reset.",
        "lifetime_rule": "effectTurn is relative to installation; a fresh finite listener survives its first TurnStart boundary, then spends one lifetime turn per later boundary.",
        "child_rule": "capture/spend is queue-time; child effects execute afterward in stable source order.",
    },
    "transaction_order": {
        "start_play": [
            "ordinary draw boundary",
            PHASE_EXAM_START_TURN,
            PHASE_START_EXAM_PLAY,
            PHASE_START_PLAY,
            PHASE_EXAM_TURN_TIMER,
            PHASE_EXAM_TURN_INTERVAL,
        ],
        "card_play": [
            "build direct and ExamCardPlay listener candidates",
            "payment/cost reactives",
            "captured listener children",
            "direct card effects in Master order",
            "physical move later",
        ],
        "fact": "a captured listener is not re-evaluated after payment or after direct effects.",
    },
    "child_timing": [
        resolve_plan2_child_evaluation_timing(
            AGGRESSIVE_EFFECT_TYPE,
            listener_phase="ProduceExamPhaseType_ExamCardPlay",
        ).to_dict(),
        resolve_plan2_child_evaluation_timing(
            REVIEW_EFFECT_TYPE,
            listener_phase="ProduceExamPhaseType_ExamCardPlay",
        ).to_dict(),
        resolve_plan2_child_evaluation_timing(
            REVIEW_MULTIPLE_EFFECT_TYPE,
            direct_effect_slot=True,
        ).to_dict(),
        resolve_plan2_child_evaluation_timing(
            BLOCK_EFFECT_TYPE,
            direct_effect_slot=True,
        ).to_dict(),
        resolve_plan2_child_evaluation_timing(
            sorted(STAMINA_EFFECT_TYPES)[0],
            listener_phase="ProduceExamPhaseType_ExamCardPlay",
        ).to_dict(),
    ],
}


__all__ = [
    "AGGRESSIVE_EFFECT_TYPE",
    "ANDROID_PLAN2_START_PLAY_TRIGGER_EVIDENCE",
    "BLOCK_EFFECT_TYPE",
    "CARD_DRAW_EFFECT_TYPE",
    "CHECK_NOT",
    "DEFAULT_DATABASE",
    "EvaluationBoundary",
    "FIELD_CARD_PLAY_AGGRESSIVE_UP",
    "FIELD_PRESERVATION_UP",
    "FORCE_PLAY_CARD_SEARCH_EFFECT_TYPE",
    "LESSON_UNKNOWN",
    "ListenerBudget",
    "ListenerConsumption",
    "MOVE_UNKNOWN",
    "PHASE_EXAM_START_PLAY_REQUESTED",
    "PHASE_EXAM_START_TURN",
    "PHASE_EXAM_TURN_INTERVAL",
    "PHASE_EXAM_TURN_TIMER",
    "PHASE_START_EXAM_PLAY",
    "PHASE_START_PLAY",
    "PLAN2",
    "PLAN_COMMON",
    "PLAYABLE_VALUE_ADD_EFFECT_TYPE",
    "Plan2ChildEvaluationTiming",
    "Plan2StartPlayCardEffectSlot",
    "Plan2StartPlayCardVersion",
    "Plan2StartPlayCatalog",
    "Plan2StartPlayEffectRow",
    "Plan2StartPlayEvaluation",
    "Plan2StartPlayListener",
    "Plan2StartPlayStatusEnchantRow",
    "Plan2StartPlayStatusProgram",
    "Plan2StartPlayTriggerContract",
    "Plan2StartPlayTriggerRow",
    "REVIEW_EFFECT_TYPE",
    "REVIEW_MULTIPLE_EFFECT_TYPE",
    "START_PLAY_PHASE_ORDER",
    "START_PLAY_FAMILY_PREFIX",
    "START_PLAY_TRIGGER_ID",
    "START_TURN_TRIGGER_PHASE_ORDER",
    "STAMINA_EFFECT_TYPES",
    "STATUS_ENCHANT_EFFECT_TYPE",
    "StartPlayActivation",
    "StartPlayCatalogError",
    "StartPlayCapture",
    "StartPlayEvaluationInput",
    "StartPlayListenerConsumption",
    "StartPlayTurnTransition",
    "TARGET_CARD_IDS",
    "TARGET_STATUS_ENCHANT_IDS",
    "TARGET_TRIGGER_ID",
    "TARGET_TRIGGER_ROW",
    "TriggerResolution",
    "advance_plan2_start_play_listener_turn",
    "capture_plan2_start_play",
    "consume_listener",
    "consume_plan2_start_play_listener",
    "effect_group_execution_order",
    "evaluate_plan2_field_status_predicate",
    "evaluate_plan2_start_play_trigger",
    "event_index",
    "is_before",
    "load_plan2_start_play_catalog",
    "load_plan2_start_play_status_program",
    "load_plan2_start_play_trigger_catalog",
    "normalize_native_limit",
    "ordered_effect_ids",
    "ordered_start_play_child_effects",
    "reset_listener_turn",
    "resolve_plan2_child_evaluation_timing",
    "resolve_plan2_start_play_trigger",
    "simulate_install_plan2_start_play_listener",
]
