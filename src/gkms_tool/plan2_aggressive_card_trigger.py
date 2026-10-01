"""Standalone native contract for the Plan2/Common aggressive card trigger.

This module is intentionally independent of :mod:`plan2_state`, the Plan3
engine, and the central coverage builder.  It reads the local Master database
as an immutable catalog and evaluates only the trigger predicate proved by the
Android v3.2.3 native call chain.

The important distinction is that ``ProduceExamPhaseType_None`` is a literal
phase value in the Master trigger.  For a card-owned ``playTriggerId`` it is
evaluated while ``ExamSequence.ExecuteCardCommandImpl`` builds the card's
direct ``PlayEffect`` commands, before payment.  It is not a wildcard phase,
not a status-listener subscription, and not an event-delta test.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "var" / "master.sqlite3"

PHASE_NONE = "ProduceExamPhaseType_None"
PHASE_EXAM_CARD_PLAY = "ProduceExamPhaseType_ExamCardPlay"
FIELD_CARD_PLAY_AGGRESSIVE_UP = "ProduceExamFieldStatusType_CardPlayAggressiveUp"
CHECK_NOT = "ProduceExamTriggerCheckType_Not"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
DIRECT_DISPATCH_PHASE = "PlayEffect"
CARD_PLAY_AGGRESSIVE_EFFECT_TYPE = (
    "ProduceExamEffectType_ExamCardPlayAggressive"
)
PLAN_COMMON = "ProducePlanType_Common"
PLAN2 = "ProducePlanType_Plan2"

TARGET_TRIGGER_ID = "e_trigger-none-card_play_aggressive_up-3"
NONE_TRIGGER_IDS = (
    TARGET_TRIGGER_ID,
    "e_trigger-none-card_play_aggressive_up-6",
    "e_trigger-none-card_play_aggressive_up-9",
    "e_trigger-none-not-card_play_aggressive_up-6",
    "e_trigger-none-not-card_play_aggressive_up-9",
)
EXAM_CARD_PLAY_TRIGGER_IDS = (
    "e_trigger-exam_card_play-card_play_aggressive_up-3",
    "e_trigger-exam_card_play-card_play_aggressive_up-5-lesson_visual",
    "e_trigger-exam_card_play-card_play_aggressive_up-5-"
    "p_card_search-active_skill-playing-0_1",
    "e_trigger-exam_card_play-card_play_aggressive_up-6",
    "e_trigger-exam_card_play-card_play_aggressive_up-8-"
    "p_card_search-active_skill-playing-0_1",
    "e_trigger-exam_card_play-card_play_aggressive_up-12-"
    "p_card_search-playing-idol-unique-0_1",
)
FAMILY_TRIGGER_IDS = NONE_TRIGGER_IDS + EXAM_CARD_PLAY_TRIGGER_IDS
SUPPORTED_NONE_TRIGGER_IDS = frozenset(NONE_TRIGGER_IDS)
SUPPORTED_PLAN_TYPES = (PLAN_COMMON, PLAN2)


class EvaluationBoundary(str, Enum):
    """Native boundary at which a direct card predicate may be evaluated."""

    PRE_PAYMENT_BUILD = "pre-payment-build"
    POST_PAYMENT_EXECUTION = "post-payment-execution"
    POST_DIRECT_EFFECT = "post-direct-effect"


class AggressiveCatalogError(ValueError):
    """The local Master catalog is missing or has an unproved shape."""


@dataclass(frozen=True, slots=True)
class AggressiveTriggerRow:
    """The exact trigger columns needed by the native field predicate."""

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
        if not isinstance(self.id, str) or not self.id:
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
        if not isinstance(self.produce_card_search_id, str):
            raise TypeError("produce_card_search_id must be a string")
        for name in ("upper_search_count", "lower_search_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if not isinstance(self.card_move_position_type, str):
            raise TypeError("card_move_position_type must be a string")
        if not isinstance(self.lesson_type, str):
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


@dataclass(frozen=True, slots=True)
class AggressiveTriggerContract:
    """Proved semantics for one None/CardPlayAggressiveUp trigger row."""

    row: AggressiveTriggerRow
    threshold: int
    check_not: bool
    comparison: str = ">="
    value_source: str = "current Aggressive status value"
    counter_scope: str = "none"
    counter_source: str = "global/per-turn card-play counts are not read"
    trigger_count_scope: str = "none"
    trigger_count_source: str = "not a TriggerEffectStatusEffect listener"
    evaluation_boundary: str = EvaluationBoundary.PRE_PAYMENT_BUILD.value
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    listener_kind: str = "card-owned direct trigger"
    repeat_policy: str = "re-evaluate once per card play; isOncePlayEffect=false"

    @property
    def result_expression(self) -> str:
        expression = f"aggressive_value >= {self.threshold}"
        return f"not ({expression})" if self.check_not else expression

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.row.id,
            "phase": self.row.phase_types[0],
            "threshold": self.threshold,
            "checkNot": self.check_not,
            "comparison": self.comparison,
            "resultExpression": self.result_expression,
            "valueSource": self.value_source,
            "counterScope": self.counter_scope,
            "counterSource": self.counter_source,
            "triggerCountScope": self.trigger_count_scope,
            "triggerCountSource": self.trigger_count_source,
            "evaluationBoundary": self.evaluation_boundary,
            "dispatchPhase": self.dispatch_phase,
            "listenerKind": self.listener_kind,
            "repeatPolicy": self.repeat_policy,
        }


@dataclass(frozen=True, slots=True)
class ContractResolution:
    supported: bool
    contract: AggressiveTriggerContract | None
    reasons: tuple[str, ...] = ()


def _shape_reasons(row: AggressiveTriggerRow) -> tuple[str, ...]:
    reasons: list[str] = []
    if row.id not in SUPPORTED_NONE_TRIGGER_IDS:
        reasons.append("unknown-none-trigger-id")
    if row.phase_types != (PHASE_NONE,):
        reasons.append("phase-is-not-none")
    if row.phase_values:
        reasons.append("phase-values-must-be-empty")
    if row.field_status_types != (FIELD_CARD_PLAY_AGGRESSIVE_UP,):
        reasons.append("field-is-not-card-play-aggressive-up")
    if len(row.field_status_values) != 1:
        reasons.append("one-threshold-is-required")
    elif row.field_status_values[0] < 0:
        reasons.append("threshold-must-be-non-negative")
    if row.field_status_check_types not in ((), (CHECK_NOT,)):
        reasons.append("only-empty-or-single-not-check-is-supported")
    if row.field_status_card_search_ids:
        reasons.append("field-card-search-is-not-supported")
    if row.produce_card_search_id:
        reasons.append("card-search-is-not-supported")
    if row.upper_search_count != 0 or row.lower_search_count != 0:
        reasons.append("search-count-bounds-must-be-zero")
    if row.card_move_position_type != MOVE_UNKNOWN:
        reasons.append("card-move-condition-is-not-supported")
    if row.effect_types:
        reasons.append("effect-type-condition-is-not-supported")
    if row.lesson_type != LESSON_UNKNOWN:
        reasons.append("lesson-condition-is-not-supported")
    return tuple(reasons)


def resolve_aggressive_trigger(row: AggressiveTriggerRow) -> ContractResolution:
    """Resolve only the five exact None rows; unknown shape fails closed."""

    reasons = _shape_reasons(row)
    if reasons:
        return ContractResolution(False, None, reasons)
    threshold = row.field_status_values[0]
    return ContractResolution(
        True,
        AggressiveTriggerContract(
            row=row,
            threshold=threshold,
            check_not=row.is_not,
        ),
    )


@dataclass(frozen=True, slots=True)
class AggressiveEvaluationInput:
    """Explicit read-only native inputs; counters are intentionally inert."""

    aggressive_status_value: int
    global_card_play_count: int = 0
    turn_card_play_count: int = 0
    phase: str = PHASE_NONE
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    boundary: EvaluationBoundary = EvaluationBoundary.PRE_PAYMENT_BUILD

    def __post_init__(self) -> None:
        for name in (
            "aggressive_status_value",
            "global_card_play_count",
            "turn_card_play_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if not isinstance(self.phase, str) or not self.phase:
            raise TypeError("phase must be a non-empty string")
        if not isinstance(self.dispatch_phase, str) or not self.dispatch_phase:
            raise TypeError("dispatch_phase must be a non-empty string")
        if not isinstance(self.boundary, EvaluationBoundary):
            object.__setattr__(self, "boundary", EvaluationBoundary(self.boundary))


@dataclass(frozen=True, slots=True)
class AggressiveEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    aggressive_status_value: int | None
    threshold: int | None
    comparison: str | None
    check_not: bool | None
    phase: str
    dispatch_phase: str
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
            "aggressiveStatusValue": self.aggressive_status_value,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "checkNot": self.check_not,
            "phase": self.phase,
            "dispatchPhase": self.dispatch_phase,
            "boundary": self.boundary.value,
            "reasons": list(self.reasons),
        }


def evaluate_aggressive_trigger(
    row: AggressiveTriggerRow,
    inputs: AggressiveEvaluationInput,
) -> AggressiveEvaluation:
    """Evaluate a proved row without mutating or consulting game state."""

    resolution = resolve_aggressive_trigger(row)
    if not resolution.supported:
        return AggressiveEvaluation(
            trigger_id=row.id,
            supported=False,
            fires=None,
            aggressive_status_value=None,
            threshold=None,
            comparison=None,
            check_not=None,
            phase=inputs.phase,
            dispatch_phase=inputs.dispatch_phase,
            boundary=inputs.boundary,
            reasons=resolution.reasons,
        )
    contract = resolution.contract
    assert contract is not None
    reasons: list[str] = []
    if inputs.phase != PHASE_NONE:
        reasons.append("runtime-phase-is-not-none")
    if inputs.dispatch_phase != contract.dispatch_phase:
        reasons.append("dispatch-phase-is-not-direct-play-effect")
    if inputs.boundary is not EvaluationBoundary.PRE_PAYMENT_BUILD:
        reasons.append("direct-trigger-is-not-rechecked-at-this-boundary")
    if reasons:
        return AggressiveEvaluation(
            trigger_id=row.id,
            supported=False,
            fires=None,
            aggressive_status_value=None,
            threshold=contract.threshold,
            comparison=contract.comparison,
            check_not=contract.check_not,
            phase=inputs.phase,
            dispatch_phase=inputs.dispatch_phase,
            boundary=inputs.boundary,
            reasons=tuple(reasons),
        )
    base_match = inputs.aggressive_status_value >= contract.threshold
    fires = not base_match if contract.check_not else base_match
    return AggressiveEvaluation(
        trigger_id=row.id,
        supported=True,
        fires=fires,
        aggressive_status_value=inputs.aggressive_status_value,
        threshold=contract.threshold,
        comparison=contract.comparison,
        check_not=contract.check_not,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
    )


@dataclass(frozen=True, slots=True)
class AggressiveCardPlaySimulation:
    """A pure repeated-play probe; it never changes the supplied snapshot."""

    evaluations: tuple[AggressiveEvaluation, ...]

    @property
    def repeatable(self) -> bool:
        return all(item.supported for item in self.evaluations)


def simulate_aggressive_card_play(
    row: AggressiveTriggerRow,
    inputs: AggressiveEvaluationInput,
    *,
    repetitions: int = 1,
) -> AggressiveCardPlaySimulation:
    """Evaluate the same card-owned predicate once per play invocation."""

    if isinstance(repetitions, bool) or not isinstance(repetitions, int):
        raise TypeError("repetitions must be an integer")
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    return AggressiveCardPlaySimulation(
        tuple(evaluate_aggressive_trigger(row, inputs) for _ in range(repetitions))
    )


@dataclass(frozen=True, slots=True)
class CardEffectSlot:
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
class AggressiveCardVersion:
    card_id: str
    upgrade_count: int
    plan_type: str
    category: str
    play_trigger_id: str
    effect_slots: tuple[CardEffectSlot, ...]

    @property
    def family_slot_trigger_ids(self) -> tuple[str, ...]:
        return tuple(
            slot.trigger_id
            for slot in self.effect_slots
            if slot.trigger_id in FAMILY_TRIGGER_IDS
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade_count,
            "planType": self.plan_type,
            "category": self.category,
            "playTriggerId": self.play_trigger_id,
            "effectSlots": [slot.to_dict() for slot in self.effect_slots],
        }


@dataclass(frozen=True, slots=True)
class AggressiveStatusEnchantRow:
    id: str
    trigger_id: str
    child_effect_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "triggerId": self.trigger_id,
            "childEffectIds": list(self.child_effect_ids),
        }


@dataclass(frozen=True, slots=True)
class AggressiveEffectRow:
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
class AggressiveTriggerCatalog:
    """Immutable Master catalog for the exact 11-row family."""

    trigger_rows: tuple[AggressiveTriggerRow, ...]
    card_versions: tuple[AggressiveCardVersion, ...]
    status_enchants: tuple[AggressiveStatusEnchantRow, ...]
    effect_rows: tuple[AggressiveEffectRow, ...]
    universe_plan2_common_versions: int
    universe_plan2_versions: int
    universe_common_versions: int

    def trigger(self, trigger_id: str) -> AggressiveTriggerRow:
        for row in self.trigger_rows:
            if row.id == trigger_id:
                return row
        raise KeyError(trigger_id)

    @property
    def target(self) -> AggressiveTriggerRow:
        return self.trigger(TARGET_TRIGGER_ID)

    @property
    def none_rows(self) -> tuple[AggressiveTriggerRow, ...]:
        return tuple(row for row in self.trigger_rows if row.id in NONE_TRIGGER_IDS)

    @property
    def card_play_rows(self) -> tuple[AggressiveTriggerRow, ...]:
        return tuple(
            row for row in self.trigger_rows if row.id in EXAM_CARD_PLAY_TRIGGER_IDS
        )

    @property
    def standalone_executable_rows(self) -> tuple[AggressiveTriggerRow, ...]:
        return tuple(
            row
            for row in self.trigger_rows
            if resolve_aggressive_trigger(row).supported
        )

    def direct_versions_for_trigger(
        self, trigger_id: str
    ) -> tuple[AggressiveCardVersion, ...]:
        return tuple(
            card for card in self.card_versions if card.play_trigger_id == trigger_id
        )

    def affected_versions_for_trigger(
        self, trigger_id: str
    ) -> tuple[AggressiveCardVersion, ...]:
        return tuple(
            card
            for card in self.card_versions
            if card.play_trigger_id == trigger_id
            or any(slot.trigger_id == trigger_id for slot in card.effect_slots)
        )

    def slot_references_for_trigger(self, trigger_id: str) -> tuple[CardEffectSlot, ...]:
        return tuple(
            slot
            for card in self.card_versions
            for slot in card.effect_slots
            if slot.trigger_id == trigger_id
        )

    @property
    def direct_card_play_reference_count(self) -> int:
        return sum(
            card.play_trigger_id in FAMILY_TRIGGER_IDS
            for card in self.card_versions
        )

    @property
    def family_reference_count(self) -> int:
        return self.direct_card_play_reference_count + sum(
            slot.trigger_id in FAMILY_TRIGGER_IDS
            for card in self.card_versions
            for slot in card.effect_slots
        )

    def summary(self) -> dict[str, object]:
        direct = {
            trigger_id: len(self.direct_versions_for_trigger(trigger_id))
            for trigger_id in FAMILY_TRIGGER_IDS
        }
        affected = {
            trigger_id: len(self.affected_versions_for_trigger(trigger_id))
            for trigger_id in FAMILY_TRIGGER_IDS
        }
        return {
            "triggerRowCount": len(self.trigger_rows),
            "noneTriggerRowCount": len(self.none_rows),
            "examCardPlayTriggerRowCount": len(self.card_play_rows),
            "notTriggerRowCount": sum(row.is_not for row in self.none_rows),
            "standaloneExecutableTriggerRowCount": len(
                self.standalone_executable_rows
            ),
            "statusEnchantRowCount": len(self.status_enchants),
            "effectRowCount": len(self.effect_rows),
            "relevantCardVersionCount": len(self.card_versions),
            "relevantUniqueCardCount": len({card.card_id for card in self.card_versions}),
            "universePlan2CommonVersionCount": self.universe_plan2_common_versions,
            "universePlan2VersionCount": self.universe_plan2_versions,
            "universeCommonVersionCount": self.universe_common_versions,
            "directCardPlayReferenceCount": self.direct_card_play_reference_count,
            "familyReferenceCount": self.family_reference_count,
            "directVersionsByTrigger": direct,
            "affectedVersionsByTrigger": affected,
            "targetDirectVersionCount": len(
                self.direct_versions_for_trigger(TARGET_TRIGGER_ID)
            ),
            "targetAffectedVersionCount": len(
                self.affected_versions_for_trigger(TARGET_TRIGGER_ID)
            ),
            "targetDirectUniqueCardCount": len(
                {
                    card.card_id
                    for card in self.direct_versions_for_trigger(TARGET_TRIGGER_ID)
                }
            ),
            "targetAffectedUniqueCardCount": len(
                {
                    card.card_id
                    for card in self.affected_versions_for_trigger(TARGET_TRIGGER_ID)
                }
            ),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary(),
            "triggerRows": [row.to_dict() for row in self.trigger_rows],
            "cardVersions": [card.to_dict() for card in self.card_versions],
            "statusEnchants": [row.to_dict() for row in self.status_enchants],
            "effectRows": [row.to_dict() for row in self.effect_rows],
        }


def _json_list(value: object, label: str) -> list[object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise AggressiveCatalogError(f"{label}: invalid JSON") from error
    if not isinstance(parsed, list):
        raise AggressiveCatalogError(f"{label}: expected JSON list")
    return parsed


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_list(value, label)
    if any(not isinstance(item, str) for item in parsed):
        raise AggressiveCatalogError(f"{label}: expected string entries")
    return tuple(parsed)


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_list(value, label)
    if any(isinstance(item, bool) or not isinstance(item, int) for item in parsed):
        raise AggressiveCatalogError(f"{label}: expected integer entries")
    return tuple(parsed)


def _trigger_from_row(row: sqlite3.Row) -> AggressiveTriggerRow:
    trigger_id = str(row["id"])
    return AggressiveTriggerRow(
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
        raise AggressiveCatalogError(f"{effect_id}: invalid raw_json") from error
    if not isinstance(raw, dict):
        raise AggressiveCatalogError(f"{effect_id}: raw_json must be an object")
    groups = raw.get("effectGroupIds", [])
    if not isinstance(groups, list) or any(
        not isinstance(group, str) or not group for group in groups
    ):
        raise AggressiveCatalogError(f"{effect_id}: invalid effectGroupIds")
    return tuple(groups)


def _effect_from_row(row: sqlite3.Row) -> AggressiveEffectRow:
    effect_id = str(row["id"])
    return AggressiveEffectRow(
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


def _status_from_row(row: sqlite3.Row) -> AggressiveStatusEnchantRow:
    status_id = str(row["id"])
    children = _string_tuple(
        row["produce_exam_effect_ids_json"], f"{status_id}.produceExamEffectIds"
    )
    return AggressiveStatusEnchantRow(
        id=status_id,
        trigger_id=str(row["produce_exam_trigger_id"]),
        child_effect_ids=children,
    )


def _card_slots(
    card_id: str,
    upgrade_count: int,
    raw_effects: object,
    effect_groups: Mapping[str, tuple[str, ...]],
) -> tuple[CardEffectSlot, ...]:
    if not isinstance(raw_effects, list):
        raise AggressiveCatalogError(f"{card_id}+{upgrade_count}: effects must be list")
    slots: list[CardEffectSlot] = []
    for index, raw in enumerate(raw_effects):
        if not isinstance(raw, dict):
            raise AggressiveCatalogError(f"{card_id}+{upgrade_count}: invalid slot")
        effect_id = raw.get("produceExamEffectId", "")
        trigger_id = raw.get("produceExamTriggerId", "")
        once = raw.get("isOncePlayEffect")
        if not isinstance(effect_id, str) or not isinstance(trigger_id, str):
            raise AggressiveCatalogError(f"{card_id}+{upgrade_count}: invalid slot IDs")
        if not isinstance(once, bool):
            raise AggressiveCatalogError(
                f"{card_id}+{upgrade_count}: isOncePlayEffect must be bool"
            )
        groups = effect_groups.get(effect_id, ())
        slots.append(
            CardEffectSlot(
                card_id=card_id,
                upgrade_count=upgrade_count,
                slot_index=index,
                effect_id=effect_id,
                trigger_id=trigger_id,
                is_once_play_effect=once,
                effect_group_ids=groups,
            )
        )
    return tuple(slots)


def load_aggressive_card_trigger_catalog(
    database: Path | str = DEFAULT_DATABASE,
) -> AggressiveTriggerCatalog:
    """Load the exact family from local Master without modifying the database."""

    path = Path(database)
    if not path.is_file():
        raise AggressiveCatalogError(f"Master database not found: {path}")
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise AggressiveCatalogError(f"cannot open Master read-only: {path}") from error
    connection.row_factory = sqlite3.Row
    with closing(connection):
        placeholders = ",".join("?" for _ in FAMILY_TRIGGER_IDS)
        trigger_rows_raw = connection.execute(
            f"SELECT * FROM produce_exam_trigger WHERE id IN ({placeholders})",
            FAMILY_TRIGGER_IDS,
        ).fetchall()
        trigger_by_id = {str(row["id"]): _trigger_from_row(row) for row in trigger_rows_raw}
        missing = [trigger_id for trigger_id in FAMILY_TRIGGER_IDS if trigger_id not in trigger_by_id]
        if missing:
            raise AggressiveCatalogError(f"missing trigger rows: {missing}")
        trigger_rows = tuple(trigger_by_id[trigger_id] for trigger_id in FAMILY_TRIGGER_IDS)

        status_rows_raw = connection.execute(
            f"""
            SELECT id, produce_exam_trigger_id, produce_exam_effect_ids_json
              FROM produce_exam_status_enchant
             WHERE produce_exam_trigger_id IN ({placeholders})
             ORDER BY produce_exam_trigger_id, id
            """,
            FAMILY_TRIGGER_IDS,
        ).fetchall()
        status_enchants = tuple(_status_from_row(row) for row in status_rows_raw)

        card_rows = connection.execute(
            """
            SELECT id, upgrade_count, plan_type, category,
                   play_trigger_id, play_effects_json
              FROM card
             WHERE plan_type IN (?, ?)
             ORDER BY id, upgrade_count
            """,
            SUPPORTED_PLAN_TYPES,
        ).fetchall()
        parsed_cards: list[tuple[sqlite3.Row, list[dict[str, object]]]] = []
        relevant_effect_ids: set[str] = set()
        for card in card_rows:
            card_id = str(card["id"])
            effects = _json_list(card["play_effects_json"], f"{card_id}.playEffects")
            normalized: list[dict[str, object]] = []
            has_family_ref = str(card["play_trigger_id"]) in FAMILY_TRIGGER_IDS
            for raw in effects:
                if not isinstance(raw, dict):
                    raise AggressiveCatalogError(f"{card_id}: invalid play effect")
                normalized.append(raw)
                trigger_id = raw.get("produceExamTriggerId", "")
                effect_id = raw.get("produceExamEffectId", "")
                if not isinstance(trigger_id, str) or not isinstance(effect_id, str):
                    raise AggressiveCatalogError(f"{card_id}: invalid play effect IDs")
                has_family_ref |= trigger_id in FAMILY_TRIGGER_IDS
            if has_family_ref:
                parsed_cards.append((card, normalized))
                relevant_effect_ids.update(
                    str(raw["produceExamEffectId"])
                    for raw in normalized
                    if str(raw["produceExamEffectId"])
                )

        if relevant_effect_ids:
            effect_placeholders = ",".join("?" for _ in relevant_effect_ids)
            effect_rows_raw = connection.execute(
                f"SELECT * FROM effect WHERE id IN ({effect_placeholders})",
                tuple(sorted(relevant_effect_ids)),
            ).fetchall()
        else:
            effect_rows_raw = []
        effect_by_id = {str(row["id"]): row for row in effect_rows_raw}
        missing_effects = sorted(relevant_effect_ids - set(effect_by_id))
        if missing_effects:
            raise AggressiveCatalogError(f"missing card effect rows: {missing_effects}")

        status_effect_ids = {
            effect_id
            for status in status_enchants
            for effect_id in status.child_effect_ids
        }
        missing_status_effect_ids = status_effect_ids - set(effect_by_id)
        if missing_status_effect_ids:
            status_placeholders = ",".join("?" for _ in missing_status_effect_ids)
            more_rows = connection.execute(
                f"SELECT * FROM effect WHERE id IN ({status_placeholders})",
                tuple(sorted(missing_status_effect_ids)),
            ).fetchall()
            effect_rows_raw = list(effect_rows_raw) + list(more_rows)
            effect_by_id.update({str(row["id"]): row for row in more_rows})
        still_missing = sorted(status_effect_ids - set(effect_by_id))
        if still_missing:
            raise AggressiveCatalogError(f"missing status effect rows: {still_missing}")

        effect_catalog_by_id = {
            effect_id: _effect_from_row(row) for effect_id, row in effect_by_id.items()
        }
        effect_groups = {
            effect_id: row.effect_group_ids
            for effect_id, row in effect_catalog_by_id.items()
        }
        card_versions = tuple(
            AggressiveCardVersion(
                card_id=str(card["id"]),
                upgrade_count=int(card["upgrade_count"]),
                plan_type=str(card["plan_type"]),
                category=str(card["category"]),
                play_trigger_id=str(card["play_trigger_id"]),
                effect_slots=_card_slots(
                    str(card["id"]),
                    int(card["upgrade_count"]),
                    effects,
                    effect_groups,
                ),
            )
            for card, effects in parsed_cards
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
    return AggressiveTriggerCatalog(
        trigger_rows=trigger_rows,
        card_versions=card_versions,
        status_enchants=status_enchants,
        effect_rows=tuple(effect_catalog_by_id[key] for key in sorted(effect_catalog_by_id)),
        universe_plan2_common_versions=sum(universe_counts.values()),
        universe_plan2_versions=universe_counts[PLAN2],
        universe_common_versions=universe_counts[PLAN_COMMON],
    )


ANDROID_AGGRESSIVE_CARD_TRIGGER_EVIDENCE = {
    "field_enum": {
        "symbol": "ProduceExamFieldStatusType.CardPlayAggressiveUp",
        "value": 42,
        "source": "_research/android/game-v3.2.3/il2cppdumper/dump.cs",
    },
    "field_predicate": {
        "symbol": "ExamExtensions.IsEffectTriggerFieldValid",
        "rva": "0x68072F4",
        "helper_symbol": "ExamExtensions.IsFieldStatusTriggerStatusEffect",
        "helper_rva": "0x68082D4",
        "getter_symbol": "ExamStatusEffectCollection.GetAggressive",
        "getter_rva": "0x7E99350",
        "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt",
        "fact": "field predicate reads the current Aggressive status collection value",
        "comparison_fact": "native comparison branches are inclusive; Master descriptions say 3以上/6以上/9以上",
    },
    "direct_card_build": {
        "symbol": "ExamSequence.ExecuteCardCommandImpl",
        "rva": "0x7ECE628",
        "direct_predicate_symbol": "ExamExtensions.IsEffectTriggerFieldValid",
        "direct_command_symbol": "ExamPlayCommand.CreatePlayEffectCommand(effect)",
        "direct_command_rva": "0x7EC35E0",
        "fact": "card direct trigger is selected while the card command batch is built, before payment",
        "source": "docs/android-v323-card-transaction-order-report.md",
    },
    "status_listener_boundary": {
        "capture_symbol": "ExamStatusEffectCollection.GetCardPlayValidEffectList",
        "capture_rva": "0x7EA2318",
        "count_symbol": "TriggerEffectStatusEffect.SpendCount",
        "count_rva": "0x7EAE70C",
        "fact": "ExamCardPlay status listeners are captured/spent pre-payment and execute as children later",
    },
    "transaction_order": {
        "order": [
            "build direct candidates / card-play listener candidates",
            "payment and cost reactive commands",
            "captured listener children",
            "direct card PlayEffect slots in Master order",
            "UserCardAfterCheck",
            "MovePlayCard",
            "play-count increment/update",
        ],
        "play_effect_command_type": 5,
        "move_play_card_command_type": 6,
        "fact": "a direct card predicate is not re-run after payment or after direct effects",
        "source": "docs/android-v323-card-transaction-order-report.md",
    },
}


__all__ = [
    "ANDROID_AGGRESSIVE_CARD_TRIGGER_EVIDENCE",
    "AggressiveCardPlaySimulation",
    "AggressiveCardVersion",
    "AggressiveCatalogError",
    "AggressiveEffectRow",
    "AggressiveEvaluation",
    "AggressiveEvaluationInput",
    "AggressiveStatusEnchantRow",
    "AggressiveTriggerCatalog",
    "AggressiveTriggerContract",
    "AggressiveTriggerRow",
    "CARD_PLAY_AGGRESSIVE_EFFECT_TYPE",
    "CHECK_NOT",
    "ContractResolution",
    "DEFAULT_DATABASE",
    "DIRECT_DISPATCH_PHASE",
    "EvaluationBoundary",
    "EXAM_CARD_PLAY_TRIGGER_IDS",
    "FAMILY_TRIGGER_IDS",
    "FIELD_CARD_PLAY_AGGRESSIVE_UP",
    "LESSON_UNKNOWN",
    "MOVE_UNKNOWN",
    "NONE_TRIGGER_IDS",
    "PHASE_EXAM_CARD_PLAY",
    "PHASE_NONE",
    "PLAN2",
    "PLAN_COMMON",
    "TARGET_TRIGGER_ID",
    "evaluate_aggressive_trigger",
    "load_aggressive_card_trigger_catalog",
    "resolve_aggressive_trigger",
    "simulate_aggressive_card_play",
]
