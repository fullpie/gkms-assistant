"""Standalone native contract for Plan2/Common turn-progress card triggers.

The target is ``e_trigger-none-turn_progress_up-2``.  This module deliberately
does not import Plan2State, Plan3, the GUI, or the central coverage builder.
It reads the local Master database as an immutable catalog and evaluates only
the exact card-owned predicate proved by the Android v3.2.3 evidence:

``ExamParameterModel.CurrentTurn > fieldStatusValues[0]``

``ProduceExamPhaseType_None`` is a literal selector for this card-owned
``playTriggerId``.  It is not a wildcard, a status-listener subscription, or
an event-delta query.  The predicate is selected while
``ExamSequence.ExecuteCardCommandImpl`` builds the direct PlayEffect batch,
before card cost payment; the direct effect commands execute later and the
predicate is not rebuilt after cost, effects, move, or play-count update.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Mapping

from .plan2_aggressive_card_trigger import (
    ANDROID_AGGRESSIVE_CARD_TRIGGER_EVIDENCE,
    EvaluationBoundary,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "var" / "master.sqlite3"
DEFAULT_AUDIT_PATH = (
    PROJECT_ROOT / "var" / "coverage" / "plan2_turn_progress_trigger_native_audit.json"
)

PHASE_NONE = "ProduceExamPhaseType_None"
PHASE_EXAM_CARD_PLAY = "ProduceExamPhaseType_ExamCardPlay"
PHASE_EXAM_START_TURN = "ProduceExamPhaseType_ExamStartTurn"
FIELD_TURN_PROGRESS_UP = "ProduceExamFieldStatusType_TurnProgressUp"
CHECK_NOT = "ProduceExamTriggerCheckType_Not"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
DIRECT_DISPATCH_PHASE = "PlayEffect"
PLAN_COMMON = "ProducePlanType_Common"
PLAN2 = "ProducePlanType_Plan2"
MENTAL_SKILL_CATEGORY = "ProduceCardCategory_MentalSkill"

TARGET_TRIGGER_ID = "e_trigger-none-turn_progress_up-2"
NONE_TRIGGER_IDS = (
    TARGET_TRIGGER_ID,
    "e_trigger-none-turn_progress_up-3",
)
EXAM_START_TURN_TRIGGER_IDS = (
    "e_trigger-exam_start_turn-turn_progress_up-2",
    "e_trigger-exam_start_turn-turn_progress_up-3",
)
EXAM_CARD_PLAY_TRIGGER_IDS = (
    "e_trigger-exam_card_play-turn_progress_up-5",
)
FAMILY_TRIGGER_IDS = (
    *NONE_TRIGGER_IDS,
    *EXAM_START_TURN_TRIGGER_IDS,
    *EXAM_CARD_PLAY_TRIGGER_IDS,
)
SUPPORTED_NONE_TRIGGER_IDS = frozenset(NONE_TRIGGER_IDS)
SUPPORTED_PLAN_TYPES = (PLAN_COMMON, PLAN2)
_I32_MIN = -(2**31)
_I32_MAX = 2**31 - 1

# The existing aggressive audit owns the generic field-predicate call-chain
# evidence.  Keep this as a reference to that object rather than copying a
# second generic evidence block into this target-specific module.
GENERIC_FIELD_PREDICATE_EVIDENCE = ANDROID_AGGRESSIVE_CARD_TRIGGER_EVIDENCE[
    "field_predicate"
]


class EvaluationStage(str, Enum):
    """Transaction positions around the card-owned trigger predicate."""

    PRE_CARD_COST = "pre-card-cost-trigger-build"
    POST_CARD_COST = "post-card-cost"
    POST_DIRECT_EFFECTS = "post-direct-effects"
    POST_MOVE = "post-move"
    POST_PLAY_COUNT = "post-play-count"


class TurnProgressCatalogError(ValueError):
    """The local Master catalog is missing or outside the exact audit shape."""


@dataclass(frozen=True, slots=True)
class TurnProgressTriggerRow:
    """Exact normalized Master columns used by the generic field predicate."""

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
class TurnProgressTriggerContract:
    """Proven semantics for one exact ``None`` TurnProgressUp row."""

    row: TurnProgressTriggerRow
    threshold: int
    check_not: bool = False
    comparison: str = ">"
    value_source: str = "ExamParameterModel.CurrentTurn"
    current_turn_indexing: str = "1-based"
    turn_progress_source: str = "CurrentTurn directly; no event delta and no -1 conversion"
    threshold_signed: bool = True
    equality_fires: bool = False
    phase_selector: str = "literal"
    evaluation_boundary: str = EvaluationBoundary.PRE_PAYMENT_BUILD.value
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    listener_kind: str = "card-owned direct play gate"
    repeat_policy: str = "re-evaluate once per card play; target slots are not once-only"

    @property
    def result_expression(self) -> str:
        expression = f"current_turn > {self.threshold}"
        return f"not ({expression})" if self.check_not else expression

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.row.id,
            "phase": self.row.phase_types[0] if self.row.phase_types else None,
            "phaseSelector": self.phase_selector,
            "threshold": self.threshold,
            "thresholdSigned": self.threshold_signed,
            "checkNot": self.check_not,
            "comparison": self.comparison,
            "equalityFires": self.equality_fires,
            "resultExpression": self.result_expression,
            "valueSource": self.value_source,
            "currentTurnIndexing": self.current_turn_indexing,
            "turnProgressSource": self.turn_progress_source,
            "evaluationBoundary": self.evaluation_boundary,
            "dispatchPhase": self.dispatch_phase,
            "listenerKind": self.listener_kind,
            "repeatPolicy": self.repeat_policy,
            "slotTriggerPolicy": "target card play-effect slots have no triggerId",
        }


@dataclass(frozen=True, slots=True)
class ContractResolution:
    supported: bool
    contract: TurnProgressTriggerContract | None
    reasons: tuple[str, ...] = ()


def _shape_reasons(row: TurnProgressTriggerRow) -> tuple[str, ...]:
    reasons: list[str] = []
    if row.id not in SUPPORTED_NONE_TRIGGER_IDS:
        reasons.append("unknown-none-turn-progress-trigger-id")
    if row.phase_types != (PHASE_NONE,):
        reasons.append("phase-is-not-literal-none")
    if row.phase_values:
        reasons.append("phase-values-must-be-empty")
    if row.field_status_check_types:
        reasons.append("target-family-has-no-field-status-check-or-not")
    if row.field_status_types != (FIELD_TURN_PROGRESS_UP,):
        reasons.append("field-is-not-turn-progress-up")
    if len(row.field_status_values) != 1:
        reasons.append("one-signed-threshold-is-required")
    elif not _I32_MIN <= row.field_status_values[0] <= _I32_MAX:
        reasons.append("threshold-is-outside-signed-int32")
    expected_threshold = {
        TARGET_TRIGGER_ID: 2,
        "e_trigger-none-turn_progress_up-3": 3,
    }.get(row.id)
    if (
        expected_threshold is not None
        and row.field_status_values
        and row.field_status_values[0] != expected_threshold
    ):
        reasons.append("threshold-does-not-match-master-row")
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


def resolve_turn_progress_trigger(
    row: TurnProgressTriggerRow,
) -> ContractResolution:
    """Resolve only the exact ``None`` rows; every unknown shape fails closed."""

    reasons = _shape_reasons(row)
    if reasons:
        return ContractResolution(False, None, reasons)
    return ContractResolution(
        True,
        TurnProgressTriggerContract(
            row=row,
            threshold=row.field_status_values[0],
        ),
    )


@dataclass(frozen=True, slots=True)
class TurnProgressEvaluationInput:
    """Explicit immutable inputs at the pre-cost card-trigger boundary.

    Card/play counters are accepted as audit inputs so tests can prove they
    are inert.  They never enter the predicate.  A post-cost/effect/move/count
    snapshot is rejected instead of silently re-evaluating a stale direct
    candidate.
    """

    current_turn: int
    global_card_play_count: int = 0
    turn_card_play_count: int = 0
    repeat_index: int = 0
    phase: str = PHASE_NONE
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    boundary: EvaluationBoundary = EvaluationBoundary.PRE_PAYMENT_BUILD
    stage: EvaluationStage = EvaluationStage.PRE_CARD_COST
    card_cost_paid: bool = False
    direct_effects_applied: bool = False
    move_applied: bool = False
    play_count_incremented: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.current_turn, bool)
            or not isinstance(self.current_turn, int)
            or not _I32_MIN <= self.current_turn <= _I32_MAX
        ):
            raise TypeError("current_turn must be a signed Int32")
        for name in (
            "global_card_play_count",
            "turn_card_play_count",
            "repeat_index",
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
        if not isinstance(self.stage, EvaluationStage):
            object.__setattr__(self, "stage", EvaluationStage(self.stage))
        for name in (
            "card_cost_paid",
            "direct_effects_applied",
            "move_applied",
            "play_count_incremented",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")


@dataclass(frozen=True, slots=True)
class TurnProgressEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    current_turn: int | None
    threshold: int | None
    comparison: str | None
    check_not: bool | None
    phase: str
    dispatch_phase: str
    boundary: EvaluationBoundary
    stage: EvaluationStage
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.trigger_id,
            "supported": self.supported,
            "fires": self.fires,
            "currentTurn": self.current_turn,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "checkNot": self.check_not,
            "phase": self.phase,
            "dispatchPhase": self.dispatch_phase,
            "boundary": self.boundary.value,
            "stage": self.stage.value,
            "reasons": list(self.reasons),
        }


def evaluate_turn_progress_trigger(
    row: TurnProgressTriggerRow,
    inputs: TurnProgressEvaluationInput,
) -> TurnProgressEvaluation:
    """Evaluate the proved predicate without consulting or mutating game state."""

    resolution = resolve_turn_progress_trigger(row)
    if not resolution.supported:
        return TurnProgressEvaluation(
            trigger_id=row.id,
            supported=False,
            fires=None,
            current_turn=None,
            threshold=None,
            comparison=None,
            check_not=None,
            phase=inputs.phase,
            dispatch_phase=inputs.dispatch_phase,
            boundary=inputs.boundary,
            stage=inputs.stage,
            reasons=resolution.reasons,
        )
    contract = resolution.contract
    assert contract is not None
    reasons: list[str] = []
    if inputs.phase != PHASE_NONE:
        reasons.append("runtime-phase-is-not-literal-none")
    if inputs.dispatch_phase != contract.dispatch_phase:
        reasons.append("dispatch-phase-is-not-direct-play-effect")
    if inputs.boundary is not EvaluationBoundary.PRE_PAYMENT_BUILD:
        reasons.append("direct-trigger-is-not-rechecked-at-this-boundary")
    if inputs.stage is not EvaluationStage.PRE_CARD_COST:
        reasons.append("direct-trigger-is-not-rechecked-after-card-stage")
    if any(
        (
            inputs.card_cost_paid,
            inputs.direct_effects_applied,
            inputs.move_applied,
            inputs.play_count_incremented,
        )
    ):
        reasons.append("input-snapshot-is-after-card-transaction-step")
    if inputs.current_turn < 1:
        reasons.append("current-turn-is-not-active-1-based-value")
    if reasons:
        return TurnProgressEvaluation(
            trigger_id=row.id,
            supported=False,
            fires=None,
            current_turn=None,
            threshold=contract.threshold,
            comparison=contract.comparison,
            check_not=contract.check_not,
            phase=inputs.phase,
            dispatch_phase=inputs.dispatch_phase,
            boundary=inputs.boundary,
            stage=inputs.stage,
            reasons=tuple(reasons),
        )
    base_match = inputs.current_turn > contract.threshold
    fires = not base_match if contract.check_not else base_match
    return TurnProgressEvaluation(
        trigger_id=row.id,
        supported=True,
        fires=fires,
        current_turn=inputs.current_turn,
        threshold=contract.threshold,
        comparison=contract.comparison,
        check_not=contract.check_not,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        stage=inputs.stage,
    )


@dataclass(frozen=True, slots=True)
class TurnProgressCardPlaySimulation:
    """Pure repeated-play probe; supplied snapshots are never changed."""

    evaluations: tuple[TurnProgressEvaluation, ...]

    @property
    def repeatable(self) -> bool:
        return all(item.supported for item in self.evaluations)


def simulate_turn_progress_card_play(
    row: TurnProgressTriggerRow,
    inputs: TurnProgressEvaluationInput,
    *,
    repetitions: int = 1,
) -> TurnProgressCardPlaySimulation:
    """Re-evaluate the direct gate once for each independent play invocation."""

    if isinstance(repetitions, bool) or not isinstance(repetitions, int):
        raise TypeError("repetitions must be an integer")
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    return TurnProgressCardPlaySimulation(
        tuple(
            evaluate_turn_progress_trigger(
                row,
                replace(inputs, repeat_index=index),
            )
            for index in range(repetitions)
        )
    )


@dataclass(frozen=True, slots=True)
class TurnProgressCardEffectSlot:
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
class TurnProgressCardVersion:
    card_id: str
    upgrade_count: int
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    move_effect_trigger_type: str
    move_effect_ids: tuple[str, ...]
    effect_slots: tuple[TurnProgressCardEffectSlot, ...]

    @property
    def all_slots_repeatable(self) -> bool:
        return all(not slot.is_once_play_effect for slot in self.effect_slots)

    @property
    def slot_trigger_ids(self) -> tuple[str, ...]:
        return tuple(slot.trigger_id for slot in self.effect_slots)

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade_count,
            "planType": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "costType": self.cost_type,
            "costValue": self.cost_value,
            "playTriggerId": self.play_trigger_id,
            "movePositionType": self.move_position_type,
            "moveEffectTriggerType": self.move_effect_trigger_type,
            "moveEffectIds": list(self.move_effect_ids),
            "effectSlots": [slot.to_dict() for slot in self.effect_slots],
        }


@dataclass(frozen=True, slots=True)
class TurnProgressStatusEnchantRow:
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
class TurnProgressEffectRow:
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
class TurnProgressTriggerCatalog:
    """Immutable target-scoped Master catalog.

    ``trigger_rows`` contains the five exact TurnProgressUp sibling rows.  The
    card/effect portion is intentionally scoped to ``TARGET_TRIGGER_ID`` so
    the affected/direct result remains the requested four Plan2 versions.
    Sibling direct references are retained as counts for an explicit blocker
    report instead of being silently mixed into this target's affected set.
    """

    trigger_rows: tuple[TurnProgressTriggerRow, ...]
    card_versions: tuple[TurnProgressCardVersion, ...]
    status_enchants: tuple[TurnProgressStatusEnchantRow, ...]
    effect_rows: tuple[TurnProgressEffectRow, ...]
    sibling_direct_references: tuple[tuple[str, str, int], ...]
    universe_plan2_common_versions: int
    universe_plan2_versions: int
    universe_common_versions: int
    excluded_plan1_target_versions: int

    def trigger(self, trigger_id: str) -> TurnProgressTriggerRow:
        for row in self.trigger_rows:
            if row.id == trigger_id:
                return row
        raise KeyError(trigger_id)

    @property
    def target(self) -> TurnProgressTriggerRow:
        return self.trigger(TARGET_TRIGGER_ID)

    @property
    def none_rows(self) -> tuple[TurnProgressTriggerRow, ...]:
        return tuple(row for row in self.trigger_rows if row.id in NONE_TRIGGER_IDS)

    @property
    def exam_start_turn_rows(self) -> tuple[TurnProgressTriggerRow, ...]:
        return tuple(
            row for row in self.trigger_rows if row.id in EXAM_START_TURN_TRIGGER_IDS
        )

    @property
    def exam_card_play_rows(self) -> tuple[TurnProgressTriggerRow, ...]:
        return tuple(
            row for row in self.trigger_rows if row.id in EXAM_CARD_PLAY_TRIGGER_IDS
        )

    @property
    def standalone_executable_rows(self) -> tuple[TurnProgressTriggerRow, ...]:
        return tuple(
            row
            for row in self.trigger_rows
            if resolve_turn_progress_trigger(row).supported
        )

    def direct_versions_for_trigger(
        self, trigger_id: str
    ) -> tuple[TurnProgressCardVersion, ...]:
        return tuple(
            card for card in self.card_versions if card.play_trigger_id == trigger_id
        )

    def affected_versions_for_trigger(
        self, trigger_id: str
    ) -> tuple[TurnProgressCardVersion, ...]:
        return tuple(
            card
            for card in self.card_versions
            if card.play_trigger_id == trigger_id
            or any(slot.trigger_id == trigger_id for slot in card.effect_slots)
        )

    def slot_references_for_trigger(
        self, trigger_id: str
    ) -> tuple[TurnProgressCardEffectSlot, ...]:
        return tuple(
            slot
            for card in self.card_versions
            for slot in card.effect_slots
            if slot.trigger_id == trigger_id
        )

    @property
    def direct_card_play_reference_count(self) -> int:
        return sum(card.play_trigger_id == TARGET_TRIGGER_ID for card in self.card_versions)

    @property
    def target_effect_slot_count(self) -> int:
        return sum(len(card.effect_slots) for card in self.card_versions)

    @property
    def family_reference_count(self) -> int:
        return self.direct_card_play_reference_count + sum(
            slot.trigger_id in FAMILY_TRIGGER_IDS
            for card in self.card_versions
            for slot in card.effect_slots
        )

    @property
    def sibling_reference_summary(self) -> dict[str, dict[str, int]]:
        summary: dict[str, dict[str, int]] = {}
        for trigger_id, plan_type, count in self.sibling_direct_references:
            summary.setdefault(trigger_id, {})[plan_type] = count
        return summary

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
            "examStartTurnTriggerRowCount": len(self.exam_start_turn_rows),
            "examCardPlayTriggerRowCount": len(self.exam_card_play_rows),
            "notTriggerRowCount": sum(row.is_not for row in self.trigger_rows),
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
                {card.card_id for card in self.direct_versions_for_trigger(TARGET_TRIGGER_ID)}
            ),
            "targetAffectedUniqueCardCount": len(
                {
                    card.card_id
                    for card in self.affected_versions_for_trigger(TARGET_TRIGGER_ID)
                }
            ),
            "targetDirectEffectSlotCount": self.target_effect_slot_count,
            "targetSlotTriggerReferenceCount": len(
                self.slot_references_for_trigger(TARGET_TRIGGER_ID)
            ),
            "targetRepeatableVersionCount": sum(
                card.all_slots_repeatable for card in self.card_versions
            ),
            "siblingDirectReferencesOutsideTargetScope": self.sibling_reference_summary,
            "excludedPlan1TargetVersionCount": self.excluded_plan1_target_versions,
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
        raise TurnProgressCatalogError(f"{label}: invalid JSON") from error
    if not isinstance(parsed, list):
        raise TurnProgressCatalogError(f"{label}: expected JSON list")
    return parsed


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise TurnProgressCatalogError(f"{label}: invalid JSON object") from error
    if not isinstance(parsed, dict):
        raise TurnProgressCatalogError(f"{label}: expected JSON object")
    return parsed


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_list(value, label) if isinstance(value, str) else value
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise TurnProgressCatalogError(f"{label}: expected string entries")
    return tuple(parsed)


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_list(value, label)
    if any(isinstance(item, bool) or not isinstance(item, int) for item in parsed):
        raise TurnProgressCatalogError(f"{label}: expected integer entries")
    return tuple(parsed)


def _trigger_from_row(row: sqlite3.Row) -> TurnProgressTriggerRow:
    trigger_id = str(row["id"])
    return TurnProgressTriggerRow(
        id=trigger_id,
        phase_types=_string_tuple(
            row["phase_types_json"], f"{trigger_id}.phaseTypes"
        ),
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
        effect_types=_string_tuple(
            row["effect_types_json"], f"{trigger_id}.effectTypes"
        ),
        lesson_type=str(row["lesson_type"]),
    )


def _effect_groups(raw_json: object, effect_id: str) -> tuple[str, ...]:
    raw = _json_object(raw_json, f"{effect_id}.raw_json")
    groups = raw.get("effectGroupIds")
    if not isinstance(groups, list) or any(
        not isinstance(group, str) or not group for group in groups
    ):
        raise TurnProgressCatalogError(f"{effect_id}: invalid effectGroupIds")
    return tuple(groups)


def _effect_from_row(row: sqlite3.Row) -> TurnProgressEffectRow:
    effect_id = str(row["id"])
    return TurnProgressEffectRow(
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


def _status_from_row(row: sqlite3.Row) -> TurnProgressStatusEnchantRow:
    status_id = str(row["id"])
    return TurnProgressStatusEnchantRow(
        id=status_id,
        trigger_id=str(row["produce_exam_trigger_id"]),
        child_effect_ids=_string_tuple(
            row["produce_exam_effect_ids_json"],
            f"{status_id}.produceExamEffectIds",
        ),
    )


def _card_slots(
    card_id: str,
    upgrade_count: int,
    raw_effects: object,
    effect_groups: Mapping[str, tuple[str, ...]],
) -> tuple[TurnProgressCardEffectSlot, ...]:
    if not isinstance(raw_effects, list):
        raise TurnProgressCatalogError(f"{card_id}+{upgrade_count}: effects must be list")
    if len(raw_effects) != 3:
        raise TurnProgressCatalogError(
            f"{card_id}+{upgrade_count}: target exact row requires three effect slots"
        )
    slots: list[TurnProgressCardEffectSlot] = []
    for index, raw in enumerate(raw_effects):
        if not isinstance(raw, dict):
            raise TurnProgressCatalogError(f"{card_id}+{upgrade_count}: invalid slot")
        effect_id = raw.get("produceExamEffectId", "")
        trigger_id = raw.get("produceExamTriggerId", "")
        once = raw.get("isOncePlayEffect")
        if not isinstance(effect_id, str) or not effect_id:
            raise TurnProgressCatalogError(f"{card_id}+{upgrade_count}: invalid effect ID")
        if not isinstance(trigger_id, str):
            raise TurnProgressCatalogError(f"{card_id}+{upgrade_count}: invalid trigger ID")
        if not isinstance(once, bool):
            raise TurnProgressCatalogError(
                f"{card_id}+{upgrade_count}: isOncePlayEffect must be bool"
            )
        if effect_id not in effect_groups:
            raise TurnProgressCatalogError(f"{card_id}+{upgrade_count}: missing effect {effect_id}")
        slots.append(
            TurnProgressCardEffectSlot(
                card_id=card_id,
                upgrade_count=upgrade_count,
                slot_index=index,
                effect_id=effect_id,
                trigger_id=trigger_id,
                is_once_play_effect=once,
                effect_group_ids=effect_groups[effect_id],
            )
        )
    return tuple(slots)


def _card_from_row(
    row: sqlite3.Row,
    effect_groups: Mapping[str, tuple[str, ...]],
) -> TurnProgressCardVersion:
    card_id = str(row["id"])
    upgrade_count = int(row["upgrade_count"])
    raw = _json_object(row["raw_json"], f"{card_id}+{upgrade_count}.raw_json")
    move_trigger = raw.get("moveEffectTriggerType")
    move_effect_ids = raw.get("moveProduceExamEffectIds")
    if not isinstance(move_trigger, str) or not isinstance(move_effect_ids, list):
        raise TurnProgressCatalogError(f"{card_id}+{upgrade_count}: invalid move controls")
    if any(not isinstance(item, str) for item in move_effect_ids):
        raise TurnProgressCatalogError(f"{card_id}+{upgrade_count}: invalid move effect IDs")
    return TurnProgressCardVersion(
        card_id=card_id,
        upgrade_count=upgrade_count,
        plan_type=str(row["plan_type"]),
        category=str(row["category"]),
        stamina=int(row["stamina"]),
        cost_type=str(row["cost_type"]),
        cost_value=int(row["cost_value"]),
        play_trigger_id=str(row["play_trigger_id"]),
        move_position_type=str(row["move_position_type"]),
        move_effect_trigger_type=move_trigger,
        move_effect_ids=tuple(move_effect_ids),
        effect_slots=_card_slots(
            card_id,
            upgrade_count,
            _json_list(row["play_effects_json"], f"{card_id}+{upgrade_count}.playEffects"),
            effect_groups,
        ),
    )


def load_plan2_turn_progress_trigger_catalog(
    database: Path | str = DEFAULT_DATABASE,
) -> TurnProgressTriggerCatalog:
    """Load the sibling trigger rows and target ``None-2`` card slice read-only."""

    path = Path(database)
    if not path.is_file():
        raise TurnProgressCatalogError(f"Master database not found: {path}")
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise TurnProgressCatalogError(f"cannot open Master read-only: {path}") from error
    connection.row_factory = sqlite3.Row
    with closing(connection):
        placeholders = ",".join("?" for _ in FAMILY_TRIGGER_IDS)
        raw_triggers = connection.execute(
            f"SELECT * FROM produce_exam_trigger WHERE id IN ({placeholders})",
            FAMILY_TRIGGER_IDS,
        ).fetchall()
        trigger_by_id = {str(row["id"]): _trigger_from_row(row) for row in raw_triggers}
        missing = [
            trigger_id
            for trigger_id in FAMILY_TRIGGER_IDS
            if trigger_id not in trigger_by_id
        ]
        if missing:
            raise TurnProgressCatalogError(f"missing trigger rows: {missing}")
        trigger_rows = tuple(trigger_by_id[trigger_id] for trigger_id in FAMILY_TRIGGER_IDS)

        raw_statuses = connection.execute(
            f"""
            SELECT id, produce_exam_trigger_id, produce_exam_effect_ids_json
              FROM produce_exam_status_enchant
             WHERE produce_exam_trigger_id IN ({placeholders})
             ORDER BY produce_exam_trigger_id, id
            """,
            FAMILY_TRIGGER_IDS,
        ).fetchall()
        status_enchants = tuple(_status_from_row(row) for row in raw_statuses)

        target_rows = connection.execute(
            """
            SELECT id, upgrade_count, plan_type, category, stamina,
                   cost_type, cost_value, play_trigger_id,
                   move_position_type, play_effects_json, raw_json
              FROM card
             WHERE plan_type IN (?, ?) AND play_trigger_id = ?
             ORDER BY id, upgrade_count
            """,
            (*SUPPORTED_PLAN_TYPES, TARGET_TRIGGER_ID),
        ).fetchall()
        if not target_rows:
            raise TurnProgressCatalogError("target trigger has no scoped card versions")

        target_effect_ids: set[str] = set()
        for row in target_rows:
            effects = _json_list(
                row["play_effects_json"],
                f"{row['id']}+{row['upgrade_count']}.playEffects",
            )
            for slot in effects:
                if not isinstance(slot, dict):
                    raise TurnProgressCatalogError(
                        f"{row['id']}+{row['upgrade_count']}: invalid play effect"
                    )
                effect_id = slot.get("produceExamEffectId")
                if not isinstance(effect_id, str) or not effect_id:
                    raise TurnProgressCatalogError(
                        f"{row['id']}+{row['upgrade_count']}: invalid effect ID"
                    )
                target_effect_ids.add(effect_id)

        effect_placeholders = ",".join("?" for _ in target_effect_ids)
        raw_effects = connection.execute(
            f"SELECT * FROM effect WHERE id IN ({effect_placeholders})",
            tuple(sorted(target_effect_ids)),
        ).fetchall()
        effect_by_id = {str(row["id"]): row for row in raw_effects}
        missing_effects = sorted(target_effect_ids - set(effect_by_id))
        if missing_effects:
            raise TurnProgressCatalogError(f"missing target effect rows: {missing_effects}")
        effect_rows = tuple(
            _effect_from_row(effect_by_id[effect_id])
            for effect_id in sorted(effect_by_id)
        )
        effect_groups = {
            effect_id: effect.effect_group_ids
            for effect_id, effect in (
                (effect.id, effect) for effect in effect_rows
            )
        }
        card_versions = tuple(
            _card_from_row(row, effect_groups) for row in target_rows
        )

        sibling_rows = connection.execute(
            f"""
            SELECT play_trigger_id, plan_type, COUNT(*) AS count
              FROM card
             WHERE plan_type IN (?, ?)
               AND play_trigger_id IN ({placeholders})
             GROUP BY play_trigger_id, plan_type
             ORDER BY play_trigger_id, plan_type
            """,
            (*SUPPORTED_PLAN_TYPES, *FAMILY_TRIGGER_IDS),
        ).fetchall()
        sibling_direct_references = tuple(
            (
                str(row["play_trigger_id"]),
                str(row["plan_type"]),
                int(row["count"]),
            )
            for row in sibling_rows
            if str(row["play_trigger_id"]) != TARGET_TRIGGER_ID
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
        excluded_plan1_target_versions = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM card
                 WHERE plan_type = ? AND play_trigger_id = ?
                """,
                ("ProducePlanType_Plan1", TARGET_TRIGGER_ID),
            ).fetchone()[0]
        )
    return TurnProgressTriggerCatalog(
        trigger_rows=trigger_rows,
        card_versions=card_versions,
        status_enchants=status_enchants,
        effect_rows=effect_rows,
        sibling_direct_references=sibling_direct_references,
        universe_plan2_common_versions=sum(universe_counts.values()),
        universe_plan2_versions=universe_counts[PLAN2],
        universe_common_versions=universe_counts[PLAN_COMMON],
        excluded_plan1_target_versions=excluded_plan1_target_versions,
    )


ANDROID_TURN_PROGRESS_TRIGGER_EVIDENCE = {
    "field_enum": {
        "symbol": FIELD_TURN_PROGRESS_UP,
        "value": 22,
        "source": (
            "_research/android/game-v3.2.3/"
            "cpp2il-plugin/DiffableCs/Assembly-CSharp/"
            "Campus/Common/Proto/Client/Enums/ProduceExamFieldStatusType.cs:18"
        ),
        "dump_source": "_research/android/game-v3.2.3/il2cppdumper/dump.cs:697001-697002",
    },
    "phase_none_enum": {
        "symbol": PHASE_NONE,
        "value": 999,
        "source": "_research/android/game-v3.2.3/il2cppdumper/dump.cs:697196-697197",
        "fact": "None is a concrete enum value, not a wildcard sentinel",
    },
    "master_trigger": {
        "id": TARGET_TRIGGER_ID,
        "source": "var/master.sqlite3:produce_exam_trigger and _research/gakumasu-diff/ProduceExamTrigger.yaml:98753-98768",
        "fact": "phaseTypes=[None], fieldStatusTypes=[TurnProgressUp], fieldStatusValues=[2], all search/effect/lesson filters empty or Unknown",
    },
    "generic_field_predicate": GENERIC_FIELD_PREDICATE_EVIDENCE,
    "turn_progress_predicate": {
        "symbol": "ExamExtensions.IsFieldStatusTriggerStatusEffect",
        "rva": "0x68082D4",
        "body_locator": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt:14735-15476",
        "current_turn_getter": "ExamParameterModel.get_CurrentTurn",
        "current_turn_source": "_research/android/game-v3.2.3/il2cppdumper/dump.cs:137223,137373",
        "comparison": "signed current_turn > signed fieldStatusValues[0]",
        "equality": "false; the native/Plan3 crosswalk is strict GT, not GE",
        "crosswalk": "src/gkms_tool/plan3_start_turn_trigger.py:1264-1284",
        "native_comparison_crosswalk": {
            "source": "src/gkms_tool/plan3_start_turn_trigger.py:421-423",
            "locators": [
                "IsFieldStatusTriggerStatusEffect:0x6808794",
                "IsFieldStatusTriggerStatusEffect:0x68089E0",
                "IsFieldStatusTriggerStatusEffect:0x6808BBC",
                "IsFieldStatusTriggerStatusEffect:0x6808834",
            ],
            "fact": "TurnProgressUp uses signed currentTurn > value",
        },
    },
    "not": {
        "generic_caller": "src/gkms_tool/plan3_start_turn_trigger.py:425-428",
        "native_locator": "IsEffectTriggerFieldStatusValid:0x6808278..0x68082A0",
        "fact": "Not inversion is outside the generic field helper; this exact target has fieldStatusCheckTypes=[] and therefore does not invert",
        "target_policy": "unexpected Not shape fails closed",
    },
    "direct_card_build": ANDROID_AGGRESSIVE_CARD_TRIGGER_EVIDENCE[
        "direct_card_build"
    ],
    "transaction_order": {
        **ANDROID_AGGRESSIVE_CARD_TRIGGER_EVIDENCE["transaction_order"],
        "target_predicate_position": "pre-card-cost direct candidate build",
        "target_predicate_recheck_after": [
            "card cost",
            "direct effect execution",
            "UserCardAfterCheck",
            "MovePlayCard",
            "play-count increment/update",
        ],
        "source_detail": "docs/android-v323-card-transaction-order-report.md:30-36,214-227,231-245",
    },
    "repeat": {
        "source": "_research/gakumasu-diff/ProduceCard.yaml:730438-730465,731189-731216,731942-731969,732695-732722",
        "fact": "all three direct slots on all four target versions have isOncePlayEffect=false; target gate is evaluated per play invocation",
    },
    "effect_and_slot_order": {
        "source": "_research/gakumasu-diff/ProduceCard.yaml target rows and ProduceExamEffect.yaml rows",
        "effect_row_locators": [
            "ProduceExamEffect.yaml:99896-99924",
            "ProduceExamEffect.yaml:100134-100162",
            "ProduceExamEffect.yaml:100372-100400",
            "ProduceExamEffect.yaml:40381-40409",
            "ProduceExamEffect.yaml:486921-486949",
        ],
        "fact": "slot indices are Master order 0,1,2; effectGroupIds are read from each effect row without reordering",
    },
}


def build_plan2_turn_progress_trigger_audit(
    database: Path | str = DEFAULT_DATABASE,
) -> dict[str, object]:
    """Build the standalone audit payload without touching central coverage."""

    catalog = load_plan2_turn_progress_trigger_catalog(database)
    payload = catalog.to_dict()
    target_resolution = resolve_turn_progress_trigger(catalog.target)
    payload.update(
        {
            "schemaVersion": 1,
            "auditId": "plan2-turn-progress-trigger-native-audit",
            "scope": {
                "planTypes": list(SUPPORTED_PLAN_TYPES),
                "targetTriggerId": TARGET_TRIGGER_ID,
                "targetCardScope": "direct playTriggerId references only",
                "centralCoverageRebuilt": False,
            },
            "standaloneContracts": [
                resolve_turn_progress_trigger(row).contract.to_dict()
                for row in catalog.standalone_executable_rows
                if resolve_turn_progress_trigger(row).contract is not None
            ],
            "targetResolution": {
                "supported": target_resolution.supported,
                "reasons": list(target_resolution.reasons),
                "contract": (
                    target_resolution.contract.to_dict()
                    if target_resolution.contract is not None
                    else None
                ),
            },
            "nativeEvidence": ANDROID_TURN_PROGRESS_TRIGGER_EVIDENCE,
            "evaluationPosition": {
                "predicate": "current-turn direct card gate",
                "phaseSelector": "literal ProduceExamPhaseType_None",
                "currentTurn": "ExamParameterModel.CurrentTurn, active exam turn is 1-based",
                "formula": "current_turn > threshold; equality does not fire",
                "not": "target has no Not; unexpected Not fails closed",
                "beforeCost": True,
                "afterCostRecheck": False,
                "afterDirectEffectsRecheck": False,
                "afterMoveRecheck": False,
                "afterPlayCountRecheck": False,
                "repeatPlay": "re-evaluate per play invocation; counters are inert",
            },
            "blockers": [
                {
                    "scope": "cost",
                    "status": "not implemented here",
                    "detail": "target Master cards carry ExamCostType_Unknown; central coverage keeps this as a Plan2 scalar cost adapter gap",
                },
                {
                    "scope": "direct-effects",
                    "status": "cataloged, not implemented here",
                    "detail": "all 12 slots resolve to three effect families marked B in central coverage",
                },
                {
                    "scope": "sibling-start-turn",
                    "status": "outside target affected set",
                    "detail": "e_trigger-exam_start_turn-turn_progress_up-2 has four Common direct versions; it is cataloged as a sibling but not counted in target direct=4",
                },
                {
                    "scope": "plan1",
                    "status": "outside requested Plan2/Common scope",
                    "detail": "four Plan1 versions also reference target trigger",
                },
            ],
        }
    )
    return payload


def write_plan2_turn_progress_trigger_audit(
    path: Path | str = DEFAULT_AUDIT_PATH,
    database: Path | str = DEFAULT_DATABASE,
) -> Path:
    """Write only the standalone audit artifact requested by this task."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            build_plan2_turn_progress_trigger_audit(database),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return output


__all__ = [
    "ANDROID_TURN_PROGRESS_TRIGGER_EVIDENCE",
    "CHECK_NOT",
    "ContractResolution",
    "DEFAULT_AUDIT_PATH",
    "DEFAULT_DATABASE",
    "DIRECT_DISPATCH_PHASE",
    "EXAM_CARD_PLAY_TRIGGER_IDS",
    "EXAM_START_TURN_TRIGGER_IDS",
    "EvaluationBoundary",
    "EvaluationStage",
    "FAMILY_TRIGGER_IDS",
    "FIELD_TURN_PROGRESS_UP",
    "GENERIC_FIELD_PREDICATE_EVIDENCE",
    "LESSON_UNKNOWN",
    "MOVE_UNKNOWN",
    "NONE_TRIGGER_IDS",
    "PHASE_EXAM_CARD_PLAY",
    "PHASE_EXAM_START_TURN",
    "PHASE_NONE",
    "PLAN2",
    "PLAN_COMMON",
    "SUPPORTED_NONE_TRIGGER_IDS",
    "TARGET_TRIGGER_ID",
    "TurnProgressCardEffectSlot",
    "TurnProgressCardPlaySimulation",
    "TurnProgressCardVersion",
    "TurnProgressCatalogError",
    "TurnProgressEffectRow",
    "TurnProgressEvaluation",
    "TurnProgressEvaluationInput",
    "TurnProgressStatusEnchantRow",
    "TurnProgressTriggerCatalog",
    "TurnProgressTriggerContract",
    "TurnProgressTriggerRow",
    "build_plan2_turn_progress_trigger_audit",
    "evaluate_turn_progress_trigger",
    "load_plan2_turn_progress_trigger_catalog",
    "resolve_turn_progress_trigger",
    "simulate_turn_progress_card_play",
    "write_plan2_turn_progress_trigger_audit",
]
