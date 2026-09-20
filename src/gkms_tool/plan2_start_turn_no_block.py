"""Standalone Plan2/Common ``ExamStartTurn + NoBlock`` contract.

This leaf deliberately owns only the exact trigger and the two audited card
families.  It reads the local Master database in read-only mode and imports
only the already-proven listener budget/order primitives.  It does not import
the Plan2 state, a central coverage builder, the Plan3 engine, or GUI code.

The native field predicate is current ``ExamParameterModel.Block == 0``.  The
event input is therefore an explicit settled StartTurn snapshot; an absent
status, a delta, or a pre-settlement snapshot is not silently treated as
``NoBlock``.  The status family keeps its ordered child plan, but its
``ExamBlockDependBlockConsumptionSum`` child is intentionally not executed by
this standalone leaf.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Sequence

from .plan3_start_play_trigger import (
    ListenerBudget,
    ListenerConsumption,
    consume_listener,
    effect_group_execution_order,
    ordered_effect_ids,
    reset_listener_turn,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "var" / "master.sqlite3"

PLAN_COMMON = "ProducePlanType_Common"
PLAN2 = "ProducePlanType_Plan2"

TARGET_TRIGGER_ID = "e_trigger-exam_start_turn-no_block"
PHASE_EXAM_START_TURN = "ProduceExamPhaseType_ExamStartTurn"
FIELD_NO_BLOCK = "ProduceExamFieldStatusType_NoBlock"
CHECK_NOT = "ProduceExamTriggerCheckType_Not"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"

COMMON_CARD_ID = "p_card-00-sup-2_026"
STATUS_CARD_ID = "p_card-02-ido-3_176"
TARGET_UPGRADES = (0, 1, 2, 3)
STATUS_ENCHANT_ID = "enchant-p_card-02-ido-3_176-enc01"
STATUS_CHILD_ID = "e_effect-exam_block_depend_block_consumption_sum-0800-01"
STATUS_CHILD_TYPE = (
    "ProduceExamEffectType_ExamBlockDependBlockConsumptionSum"
)
STATUS_WRAPPER_ID = (
    "e_effect-exam_status_enchant-03-inf-enchant-p_card-02-ido-3_176-enc01"
)
STATUS_ENCHANT_EFFECT_TYPE = "ProduceExamEffectType_ExamStatusEnchant"
BLOCK_EFFECT_TYPE = "ProduceExamEffectType_ExamBlock"
AGGRESSIVE_EFFECT_TYPE = "ProduceExamEffectType_ExamCardPlayAggressive"
STAMINA_DOWN_EFFECT_TYPE = "ProduceExamEffectType_ExamStaminaConsumptionDown"
TIMER_EFFECT_TYPE = "ProduceExamEffectType_ExamEffectTimer"
CARD_DRAW_EFFECT_ID = "e_effect-exam_card_draw-0001"

CARD_GATE_BLOCKER = f"C:card-trigger:{TARGET_TRIGGER_ID}"
STATUS_GATE_BLOCKER = f"C:status-trigger:{TARGET_TRIGGER_ID}"
CHILD_BLOCKER = f"C:effect:{STATUS_CHILD_TYPE}"

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1


class StartTurnNoBlockCatalogError(ValueError):
    """The local Master graph is absent or outside this exact audit shape."""


class SourceKind(str, Enum):
    CARD_GATE = "card-level-gate"
    STATUS_LISTENER = "status-listener-gate"


class PlayOrigin(str, Enum):
    """Origins accepted by this leaf's card-level claim."""

    ORDINARY = "ordinary"
    FORCED = "forced"
    EXTRA = "extra"


class EvaluationBoundary(str, Enum):
    """The native boundary represented by an evaluation input."""

    SETTLED_START_TURN = "settled-start-turn"
    STATUS_COMMAND_BUILD = "status-command-build"
    CHILD_EXECUTION = "child-execution"
    PRE_SETTLEMENT = "pre-settlement"


@dataclass(frozen=True, slots=True)
class StartTurnNoBlockTriggerRow:
    """Typed copy of every ``produce_exam_trigger`` Master column."""

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
        for name in (
            "phase_types",
            "field_status_check_types",
            "field_status_types",
            "field_status_card_search_ids",
            "effect_types",
        ):
            if any(type(value) is not str for value in getattr(self, name)):
                raise TypeError(f"{name} must contain strings")
        for name in ("phase_values", "field_status_values"):
            if any(type(value) is not int for value in getattr(self, name)):
                raise TypeError(f"{name} must contain plain integers")
        for name in (
            "produce_card_search_id",
            "card_move_position_type",
            "lesson_type",
        ):
            if type(getattr(self, name)) is not str:
                raise TypeError(f"{name} must be a string")
        for name in ("upper_search_count", "lower_search_count"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an integer")

    @property
    def field_status_produce_card_search_ids(self) -> tuple[str, ...]:
        """Schema-name alias used by the Master importer."""

        return self.field_status_card_search_ids

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


TARGET_TRIGGER_ROW = StartTurnNoBlockTriggerRow(
    id=TARGET_TRIGGER_ID,
    phase_types=(PHASE_EXAM_START_TURN,),
    phase_values=(),
    field_status_check_types=(),
    field_status_types=(FIELD_NO_BLOCK,),
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
class NoBlockContract:
    row: StartTurnNoBlockTriggerRow
    predicate: str = "current signed int32 ExamParameterModel.Block == 0"
    native_getter: str = "ExamParameterModel.get_Block"
    status_presence_required: bool = False
    settled_snapshot_required: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.row.id,
            "predicate": self.predicate,
            "nativeGetter": self.native_getter,
            "statusPresenceRequired": self.status_presence_required,
            "settledSnapshotRequired": self.settled_snapshot_required,
            "zeroBoundary": {"negative": False, "zero": True, "positive": False},
        }


@dataclass(frozen=True, slots=True)
class TriggerResolution:
    supported: bool
    contract: NoBlockContract | None
    reasons: tuple[str, ...] = ()


def _shape_reasons(row: StartTurnNoBlockTriggerRow) -> tuple[str, ...]:
    reasons: list[str] = []
    if row.id != TARGET_TRIGGER_ID:
        reasons.append("unknown-trigger-id")
    if row.phase_types != (PHASE_EXAM_START_TURN,):
        reasons.append("phase-must-be-exam-start-turn")
    if row.phase_values:
        reasons.append("phase-values-must-be-empty")
    if row.field_status_check_types:
        reasons.append("field-check-must-be-empty")
    if row.field_status_types != (FIELD_NO_BLOCK,):
        reasons.append("field-must-be-no-block")
    if row.field_status_values:
        reasons.append("field-values-must-be-empty")
    if row.field_status_card_search_ids:
        reasons.append("field-card-search-must-be-empty")
    if row.produce_card_search_id:
        reasons.append("card-search-must-be-empty")
    if row.upper_search_count != 0 or row.lower_search_count != 0:
        reasons.append("search-count-bounds-must-be-zero")
    if row.card_move_position_type != MOVE_UNKNOWN:
        reasons.append("card-move-condition-must-be-unknown")
    if row.effect_types:
        reasons.append("effect-condition-must-be-empty")
    if row.lesson_type != LESSON_UNKNOWN:
        reasons.append("lesson-condition-must-be-unknown")
    return tuple(reasons)


def resolve_no_block_trigger(
    row: StartTurnNoBlockTriggerRow,
) -> TriggerResolution:
    """Resolve only the exact full Master shape; all deviations fail closed."""

    reasons = _shape_reasons(row)
    if reasons:
        return TriggerResolution(False, None, reasons)
    return TriggerResolution(True, NoBlockContract(row))


resolve_plan2_start_turn_no_block_trigger = resolve_no_block_trigger


@dataclass(frozen=True, slots=True)
class StartTurnEvaluationInput:
    """One explicit native StartTurn snapshot and command boundary."""

    phase: str = PHASE_EXAM_START_TURN
    block: object = 0
    boundary: EvaluationBoundary | str = EvaluationBoundary.SETTLED_START_TURN
    snapshot_settled: bool = True
    turn_spend_applied: bool = True
    draw_settled: bool = True
    play_origin: PlayOrigin | str = PlayOrigin.ORDINARY

    def __post_init__(self) -> None:
        if type(self.phase) is not str:
            raise TypeError("phase must be a string")
        boundary = self.boundary.value if isinstance(self.boundary, EvaluationBoundary) else self.boundary
        if type(boundary) is not str:
            raise TypeError("boundary must be a string or EvaluationBoundary")
        origin = self.play_origin.value if isinstance(self.play_origin, PlayOrigin) else self.play_origin
        if type(origin) is not str:
            raise TypeError("play_origin must be a string or PlayOrigin")
        if any(
            type(value) is not bool
            for value in (
                self.snapshot_settled,
                self.turn_spend_applied,
                self.draw_settled,
            )
        ):
            raise TypeError("settlement flags must be booleans")
        object.__setattr__(self, "boundary", boundary)
        object.__setattr__(self, "play_origin", origin)


@dataclass(frozen=True, slots=True)
class NoBlockEvaluation:
    trigger_id: str
    source_kind: SourceKind
    phase: str
    boundary: str
    supported: bool
    predicate_matched: bool | None
    block: object
    reasons: tuple[str, ...] = ()

    @property
    def fires(self) -> bool | None:
        return self.predicate_matched if self.supported else None

    @property
    def fail_closed(self) -> bool:
        return not self.supported

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.trigger_id,
            "sourceKind": self.source_kind.value,
            "phase": self.phase,
            "boundary": self.boundary,
            "supported": self.supported,
            "fires": self.fires,
            "block": self.block,
            "reasons": list(self.reasons),
        }


def _source_kind(value: SourceKind | str) -> SourceKind:
    if isinstance(value, SourceKind):
        return value
    try:
        return SourceKind(value)
    except ValueError as error:
        raise ValueError(f"unknown source kind: {value}") from error


def _evaluate_shared_predicate(
    row: StartTurnNoBlockTriggerRow,
    inputs: StartTurnEvaluationInput,
    source_kind: SourceKind,
) -> NoBlockEvaluation:
    reasons = list(_shape_reasons(row))
    if source_kind is SourceKind.CARD_GATE:
        if inputs.boundary != EvaluationBoundary.SETTLED_START_TURN.value:
            reasons.append("card-gate-requires-settled-start-turn-boundary")
        if inputs.play_origin != PlayOrigin.ORDINARY.value:
            reasons.append("forced-and-extra-card-origins-are-not-asserted")
    else:
        if inputs.boundary != EvaluationBoundary.STATUS_COMMAND_BUILD.value:
            reasons.append("status-listener-requires-command-build-boundary")
    if not inputs.snapshot_settled:
        reasons.append("snapshot-is-not-settled")
    if not inputs.turn_spend_applied:
        reasons.append("turn-start-spend-is-not-applied")
    if not inputs.draw_settled:
        reasons.append("ordinary-draw-is-not-settled")
    if type(inputs.block) is not int:
        reasons.append("block-must-be-a-signed-int32-scalar")
    elif not INT32_MIN <= inputs.block <= INT32_MAX:
        reasons.append("block-is-outside-signed-int32")

    if reasons:
        return NoBlockEvaluation(
            trigger_id=row.id,
            source_kind=source_kind,
            phase=inputs.phase,
            boundary=inputs.boundary,
            supported=False,
            predicate_matched=None,
            block=inputs.block,
            reasons=tuple(dict.fromkeys(reasons)),
        )
    if inputs.phase != PHASE_EXAM_START_TURN:
        return NoBlockEvaluation(
            trigger_id=row.id,
            source_kind=source_kind,
            phase=inputs.phase,
            boundary=inputs.boundary,
            supported=True,
            predicate_matched=False,
            block=inputs.block,
            reasons=("event-phase-does-not-match-target",),
        )
    return NoBlockEvaluation(
        trigger_id=row.id,
        source_kind=source_kind,
        phase=inputs.phase,
        boundary=inputs.boundary,
        supported=True,
        predicate_matched=inputs.block == 0,
        block=inputs.block,
    )


def evaluate_no_block_trigger(
    row: StartTurnNoBlockTriggerRow,
    inputs: StartTurnEvaluationInput,
    *,
    source_kind: SourceKind | str = SourceKind.CARD_GATE,
) -> NoBlockEvaluation:
    """Evaluate the one shared predicate for either source family."""

    return _evaluate_shared_predicate(row, inputs, _source_kind(source_kind))


def evaluate_card_no_block_gate(
    row: StartTurnNoBlockTriggerRow,
    inputs: StartTurnEvaluationInput,
) -> NoBlockEvaluation:
    return evaluate_no_block_trigger(row, inputs, source_kind=SourceKind.CARD_GATE)


def evaluate_status_no_block_listener(
    row: StartTurnNoBlockTriggerRow,
    inputs: StartTurnEvaluationInput,
) -> NoBlockEvaluation:
    return evaluate_no_block_trigger(
        row, inputs, source_kind=SourceKind.STATUS_LISTENER
    )


def fires_no_block(
    block: object,
    *,
    source_kind: SourceKind | str = SourceKind.CARD_GATE,
    boundary: EvaluationBoundary | str = EvaluationBoundary.SETTLED_START_TURN,
    phase: str = PHASE_EXAM_START_TURN,
    snapshot_settled: bool = True,
    turn_spend_applied: bool = True,
    draw_settled: bool = True,
    play_origin: PlayOrigin | str = PlayOrigin.ORDINARY,
) -> bool | None:
    """Convenience scalar probe; unresolved input returns ``None``."""

    result = evaluate_no_block_trigger(
        TARGET_TRIGGER_ROW,
        StartTurnEvaluationInput(
            phase=phase,
            block=block,
            boundary=boundary,
            snapshot_settled=snapshot_settled,
            turn_spend_applied=turn_spend_applied,
            draw_settled=draw_settled,
            play_origin=play_origin,
        ),
        source_kind=source_kind,
    )
    return result.fires


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

    def __post_init__(self) -> None:
        for name in ("id", "effect_type", "status_enchant_id", "chain_effect_id"):
            if type(getattr(self, name)) is not str:
                raise TypeError(f"{name} must be a string")
        for name in ("value1", "value2", "effect_count", "effect_turn"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an integer")
        if not isinstance(self.effect_group_ids, tuple) or any(
            type(value) is not str or not value for value in self.effect_group_ids
        ):
            raise TypeError("effect_group_ids must be non-empty string tuples")

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
class StatusEnchantRow:
    id: str
    asset_id: str
    trigger_id: str
    child_effect_ids: tuple[str, ...]
    wrapper_effect_id: str

    def __post_init__(self) -> None:
        if any(
            type(getattr(self, name)) is not str
            for name in ("id", "asset_id", "trigger_id", "wrapper_effect_id")
        ):
            raise TypeError("status row text fields must be strings")
        if any(type(value) is not str or not value for value in self.child_effect_ids):
            raise TypeError("child_effect_ids must contain non-empty strings")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "assetId": self.asset_id,
            "triggerId": self.trigger_id,
            "childEffectIds": list(self.child_effect_ids),
            "wrapperEffectId": self.wrapper_effect_id,
        }


@dataclass(frozen=True, slots=True)
class StatusProgram:
    status: StatusEnchantRow
    wrapper: EffectRow
    children: tuple[EffectRow, ...]

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
    def ordered_child_ids(self) -> tuple[str, ...]:
        return ordered_effect_ids(tuple(child.id for child in self.children))

    @property
    def co_blockers(self) -> tuple[str, ...]:
        return tuple(
            f"C:effect:{child.effect_type}"
            for child in self.children
            if child.effect_type == STATUS_CHILD_TYPE
        )

    @property
    def child_execution_supported(self) -> bool:
        return not self.co_blockers

    def to_dict(self) -> dict[str, object]:
        budget = self.listener_budget
        return {
            "status": self.status.to_dict(),
            "wrapper": self.wrapper.to_dict(),
            "children": [child.to_dict() for child in self.children],
            "orderedChildIds": list(self.ordered_child_ids),
            "coBlockers": list(self.co_blockers),
            "childExecutionSupported": self.child_execution_supported,
            "listenerBudget": {
                "totalRemaining": budget.total_remaining,
                "perTurnLimit": budget.per_turn_limit,
                "perTurnRemaining": budget.per_turn_remaining,
            },
            "lifetimeTurns": self.lifetime_turns,
        }


@dataclass(frozen=True, slots=True)
class CardEffectSlot:
    card_id: str
    upgrade_count: int
    slot_index: int
    effect_id: str
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool
    effect_group_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgradeCount": self.upgrade_count,
            "slotIndex": self.slot_index,
            "effectId": self.effect_id,
            "triggerId": self.trigger_id,
            "hideIcon": self.hide_icon,
            "isOncePlayEffect": self.is_once_play_effect,
            "effectGroupIds": list(self.effect_group_ids),
        }


@dataclass(frozen=True, slots=True)
class CardVersion:
    card_id: str
    upgrade_count: int
    name: str
    plan_type: str
    category: str
    play_trigger_id: str
    move_position_type: str
    effect_slots: tuple[CardEffectSlot, ...]
    blockers: tuple[str, ...]
    direct: bool
    co_blocked: bool

    @property
    def affected(self) -> bool:
        return bool(self.blockers)

    @property
    def direct_blockers(self) -> tuple[str, ...]:
        return self.blockers if self.direct else ()

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return ordered_effect_ids(tuple(slot.effect_id for slot in self.effect_slots))

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgradeCount": self.upgrade_count,
            "name": self.name,
            "planType": self.plan_type,
            "category": self.category,
            "playTriggerId": self.play_trigger_id,
            "movePositionType": self.move_position_type,
            "effectSlots": [slot.to_dict() for slot in self.effect_slots],
            "affected": self.affected,
            "direct": self.direct,
            "coBlocked": self.co_blocked,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True, slots=True)
class FamilyCounts:
    affected: int
    direct: int
    co: int

    def to_dict(self) -> dict[str, int]:
        return {
            "affected": self.affected,
            "direct": self.direct,
            "co": self.co,
            "coBlocked": self.co,
        }


@dataclass(frozen=True, slots=True)
class NoBlockCatalog:
    trigger: StartTurnNoBlockTriggerRow
    status_program: StatusProgram
    effects: tuple[EffectRow, ...]
    card_versions: tuple[CardVersion, ...]
    status_listener_versions: tuple[CardVersion, ...]

    @property
    def card_family_counts(self) -> FamilyCounts:
        versions = self.card_versions
        return FamilyCounts(
            affected=sum(version.affected for version in versions),
            direct=sum(version.direct for version in versions),
            co=sum(version.co_blocked for version in versions),
        )

    @property
    def status_family_counts(self) -> FamilyCounts:
        versions = self.status_listener_versions
        return FamilyCounts(
            affected=sum(version.affected for version in versions),
            direct=sum(version.direct for version in versions),
            co=sum(version.co_blocked for version in versions),
        )

    @property
    def families(self) -> dict[str, FamilyCounts]:
        return {
            "card-level-gate": self.card_family_counts,
            "status-listener-gate": self.status_family_counts,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "trigger": self.trigger.to_dict(),
            "statusProgram": self.status_program.to_dict(),
            "effects": [effect.to_dict() for effect in self.effects],
            "cardVersions": [card.to_dict() for card in self.card_versions],
            "statusListenerVersions": [
                card.to_dict() for card in self.status_listener_versions
            ],
            "families": {
                name: counts.to_dict() for name, counts in self.families.items()
            },
        }


def _json_list(value: object, label: str) -> list[object]:
    if type(value) is not str:
        raise StartTurnNoBlockCatalogError(f"{label}: expected JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise StartTurnNoBlockCatalogError(f"{label}: invalid JSON") from error
    if not isinstance(parsed, list):
        raise StartTurnNoBlockCatalogError(f"{label}: expected JSON list")
    return parsed


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_list(value, label)
    if any(type(item) is not str for item in parsed):
        raise StartTurnNoBlockCatalogError(f"{label}: expected string entries")
    return tuple(parsed)


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_list(value, label)
    if any(type(item) is not int for item in parsed):
        raise StartTurnNoBlockCatalogError(f"{label}: expected integer entries")
    return tuple(parsed)


def _text(value: object, label: str) -> str:
    if type(value) is not str:
        raise StartTurnNoBlockCatalogError(f"{label}: expected text")
    return value


def _db_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise StartTurnNoBlockCatalogError(f"{label}: expected integer")
    return value


def _trigger_from_row(row: sqlite3.Row) -> StartTurnNoBlockTriggerRow:
    trigger_id = _text(row["id"], "trigger.id")
    return StartTurnNoBlockTriggerRow(
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
        produce_card_search_id=_text(
            row["produce_card_search_id"], f"{trigger_id}.produceCardSearchId"
        ),
        upper_search_count=_db_int(row["upper_search_count"], f"{trigger_id}.upperSearchCount"),
        lower_search_count=_db_int(row["lower_search_count"], f"{trigger_id}.lowerSearchCount"),
        card_move_position_type=_text(
            row["card_move_position_type"], f"{trigger_id}.cardMovePositionType"
        ),
        effect_types=_string_tuple(row["effect_types_json"], f"{trigger_id}.effectTypes"),
        lesson_type=_text(row["lesson_type"], f"{trigger_id}.lessonType"),
    )


def _effect_groups(raw_json: object, effect_id: str) -> tuple[str, ...]:
    if type(raw_json) is not str:
        raise StartTurnNoBlockCatalogError(f"{effect_id}: raw_json must be text")
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError as error:
        raise StartTurnNoBlockCatalogError(f"{effect_id}: invalid raw_json") from error
    if not isinstance(raw, dict):
        raise StartTurnNoBlockCatalogError(f"{effect_id}: raw_json must be an object")
    groups = raw.get("effectGroupIds", [])
    if not isinstance(groups, list) or any(
        type(group) is not str or not group for group in groups
    ):
        raise StartTurnNoBlockCatalogError(f"{effect_id}: invalid effectGroupIds")
    return tuple(groups)


def _effect_from_row(row: sqlite3.Row) -> EffectRow:
    effect_id = _text(row["id"], "effect.id")
    return EffectRow(
        id=effect_id,
        effect_type=_text(row["effect_type"], f"{effect_id}.effectType"),
        value1=_db_int(row["value1"], f"{effect_id}.value1"),
        value2=_db_int(row["value2"], f"{effect_id}.value2"),
        effect_count=_db_int(row["effect_count"], f"{effect_id}.effectCount"),
        effect_turn=_db_int(row["effect_turn"], f"{effect_id}.effectTurn"),
        status_enchant_id=_text(
            row["status_enchant_id"], f"{effect_id}.statusEnchantId"
        ),
        chain_effect_id=_text(row["chain_effect_id"], f"{effect_id}.chainEffectId"),
        effect_group_ids=_effect_groups(row["raw_json"], effect_id),
    )


def _status_from_row(row: sqlite3.Row) -> StatusEnchantRow:
    status_id = _text(row["id"], "status.id")
    return StatusEnchantRow(
        id=status_id,
        asset_id=_text(row["asset_id"], f"{status_id}.assetId"),
        trigger_id=_text(row["produce_exam_trigger_id"], f"{status_id}.triggerId"),
        child_effect_ids=_string_tuple(
            row["produce_exam_effect_ids_json"],
            f"{status_id}.produceExamEffectIds",
        ),
        wrapper_effect_id=STATUS_WRAPPER_ID,
    )


def _card_slots(
    card_id: str,
    upgrade_count: int,
    raw_effects: object,
    effect_groups: dict[str, tuple[str, ...]],
) -> tuple[CardEffectSlot, ...]:
    effects = (
        raw_effects
        if isinstance(raw_effects, list)
        else _json_list(raw_effects, f"{card_id}+{upgrade_count}.playEffects")
    )
    slots: list[CardEffectSlot] = []
    for index, raw in enumerate(effects):
        if not isinstance(raw, dict):
            raise StartTurnNoBlockCatalogError(f"{card_id}+{upgrade_count}: invalid slot")
        effect_id = raw.get("produceExamEffectId")
        trigger_id = raw.get("produceExamTriggerId")
        hide_icon = raw.get("hideIcon")
        once = raw.get("isOncePlayEffect")
        if type(effect_id) is not str or not effect_id:
            raise StartTurnNoBlockCatalogError(f"{card_id}+{upgrade_count}: invalid effect id")
        if type(trigger_id) is not str:
            raise StartTurnNoBlockCatalogError(f"{card_id}+{upgrade_count}: invalid trigger id")
        if type(hide_icon) is not bool or type(once) is not bool:
            raise StartTurnNoBlockCatalogError(f"{card_id}+{upgrade_count}: invalid slot flags")
        if effect_id not in effect_groups:
            raise StartTurnNoBlockCatalogError(
                f"{card_id}+{upgrade_count}: missing effect {effect_id}"
            )
        slots.append(
            CardEffectSlot(
                card_id=card_id,
                upgrade_count=upgrade_count,
                slot_index=index,
                effect_id=effect_id,
                trigger_id=trigger_id,
                hide_icon=hide_icon,
                is_once_play_effect=once,
                effect_group_ids=effect_groups[effect_id],
            )
        )
    return tuple(slots)


def _open_master_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise StartTurnNoBlockCatalogError(f"Master database not found: {path}")
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise StartTurnNoBlockCatalogError(f"cannot open Master read-only: {path}") from error
    connection.row_factory = sqlite3.Row
    return connection


COMMON_EFFECT_IDS = {
    0: (
        "e_effect-exam_block-0005",
        "e_effect-exam_stamina_consumption_down-02",
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
    ),
    1: (
        "e_effect-exam_block-0006",
        "e_effect-exam_stamina_consumption_down-03",
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
    ),
    2: (
        "e_effect-exam_block-0008",
        "e_effect-exam_stamina_consumption_down-03",
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
    ),
    3: (
        "e_effect-exam_block-0010",
        "e_effect-exam_stamina_consumption_down-03",
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
    ),
}
STATUS_EFFECT_IDS = {
    0: (
        "e_effect-exam_block-0002",
        "e_effect-exam_card_play_aggressive-0001",
        STATUS_WRAPPER_ID,
    ),
    1: (
        "e_effect-exam_block-0002",
        "e_effect-exam_card_play_aggressive-0003",
        STATUS_WRAPPER_ID,
    ),
    2: (
        "e_effect-exam_block-0002",
        "e_effect-exam_card_play_aggressive-0003",
        STATUS_WRAPPER_ID,
    ),
    3: (
        "e_effect-exam_block-0002",
        "e_effect-exam_card_play_aggressive-0003",
        STATUS_WRAPPER_ID,
    ),
}


def _validate_effect_shape(effect: EffectRow) -> None:
    if effect.effect_type == BLOCK_EFFECT_TYPE:
        if effect.value2 != 0 or effect.effect_count != 0 or effect.effect_turn != 0:
            raise StartTurnNoBlockCatalogError(f"{effect.id}: block payload changed")
        if effect.status_enchant_id or effect.chain_effect_id:
            raise StartTurnNoBlockCatalogError(f"{effect.id}: block links changed")
        return
    if effect.effect_type == STAMINA_DOWN_EFFECT_TYPE:
        if effect.value1 != 0 or effect.value2 != 0 or effect.effect_count != 0:
            raise StartTurnNoBlockCatalogError(f"{effect.id}: stamina payload changed")
        if effect.effect_turn not in (2, 3):
            raise StartTurnNoBlockCatalogError(f"{effect.id}: stamina turn payload changed")
        if effect.status_enchant_id or effect.chain_effect_id:
            raise StartTurnNoBlockCatalogError(f"{effect.id}: stamina links changed")
        return
    if effect.effect_type == TIMER_EFFECT_TYPE:
        if (effect.value1, effect.value2, effect.effect_count, effect.effect_turn) != (1, 0, 1, 0):
            raise StartTurnNoBlockCatalogError(f"{effect.id}: timer payload changed")
        if effect.status_enchant_id or effect.chain_effect_id != CARD_DRAW_EFFECT_ID:
            raise StartTurnNoBlockCatalogError(f"{effect.id}: timer chain changed")
        return
    if effect.effect_type == AGGRESSIVE_EFFECT_TYPE:
        if effect.value1 not in (1, 3) or (effect.value2, effect.effect_count, effect.effect_turn) != (0, 0, 0):
            raise StartTurnNoBlockCatalogError(f"{effect.id}: aggressive payload changed")
        if effect.status_enchant_id or effect.chain_effect_id:
            raise StartTurnNoBlockCatalogError(f"{effect.id}: aggressive links changed")
        return
    if effect.id == STATUS_WRAPPER_ID:
        if effect.effect_type != STATUS_ENCHANT_EFFECT_TYPE:
            raise StartTurnNoBlockCatalogError(f"{effect.id}: wrapper type changed")
        if (effect.value1, effect.value2, effect.effect_count, effect.effect_turn) != (0, 0, 3, -1):
            raise StartTurnNoBlockCatalogError(f"{effect.id}: wrapper payload changed")
        if effect.status_enchant_id != STATUS_ENCHANT_ID or effect.chain_effect_id:
            raise StartTurnNoBlockCatalogError(f"{effect.id}: wrapper links changed")
        return
    if effect.id == STATUS_CHILD_ID:
        if effect.effect_type != STATUS_CHILD_TYPE:
            raise StartTurnNoBlockCatalogError(f"{effect.id}: child type changed")
        if (effect.value1, effect.value2, effect.effect_count, effect.effect_turn) != (800, 0, 1, 0):
            raise StartTurnNoBlockCatalogError(f"{effect.id}: child payload changed")
        if effect.status_enchant_id or effect.chain_effect_id:
            raise StartTurnNoBlockCatalogError(f"{effect.id}: child links changed")
        return
    raise StartTurnNoBlockCatalogError(f"{effect.id}: effect type is outside this leaf")


def load_plan2_start_turn_no_block_catalog(
    database: Path | str = DEFAULT_DATABASE,
) -> NoBlockCatalog:
    """Load exactly the two four-upgrade source families from Master."""

    connection = _open_master_read_only(Path(database))
    with closing(connection):
        trigger_raw = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id=?", (TARGET_TRIGGER_ID,)
        ).fetchone()
        if trigger_raw is None:
            raise StartTurnNoBlockCatalogError(f"missing trigger row: {TARGET_TRIGGER_ID}")
        trigger = _trigger_from_row(trigger_raw)
        resolution = resolve_no_block_trigger(trigger)
        if not resolution.supported:
            raise StartTurnNoBlockCatalogError(
                f"target trigger shape is not exact: {resolution.reasons}"
            )

        card_rows = connection.execute(
            """
            SELECT * FROM card
             WHERE id IN (?, ?)
             ORDER BY id, upgrade_count
            """,
            (COMMON_CARD_ID, STATUS_CARD_ID),
        ).fetchall()
        expected_keys = {
            *( (COMMON_CARD_ID, upgrade) for upgrade in TARGET_UPGRADES ),
            *( (STATUS_CARD_ID, upgrade) for upgrade in TARGET_UPGRADES ),
        }
        actual_keys = {
            (_text(row["id"], "card.id"), _db_int(row["upgrade_count"], "card.upgrade_count"))
            for row in card_rows
        }
        if actual_keys != expected_keys:
            raise StartTurnNoBlockCatalogError(
                f"target card universe changed: expected={sorted(expected_keys)} actual={sorted(actual_keys)}"
            )

        raw_effect_lists: dict[tuple[str, int], list[object]] = {}
        effect_ids: set[str] = {STATUS_CHILD_ID}
        for row in card_rows:
            key = (_text(row["id"], "card.id"), _db_int(row["upgrade_count"], "card.upgrade_count"))
            raw_effects = _json_list(row["play_effects_json"], f"{key}.playEffects")
            raw_effect_lists[key] = raw_effects
            for raw in raw_effects:
                if not isinstance(raw, dict) or type(raw.get("produceExamEffectId")) is not str:
                    raise StartTurnNoBlockCatalogError(f"{key}: invalid effect slot")
                effect_ids.add(raw["produceExamEffectId"])

        placeholders = ",".join("?" for _ in effect_ids)
        effect_rows = connection.execute(
            f"SELECT * FROM effect WHERE id IN ({placeholders})",
            tuple(sorted(effect_ids)),
        ).fetchall()
        effect_by_id = { _text(row["id"], "effect.id"): _effect_from_row(row) for row in effect_rows }
        missing = sorted(effect_ids - set(effect_by_id))
        if missing:
            raise StartTurnNoBlockCatalogError(f"missing effect rows: {missing}")
        for effect in effect_by_id.values():
            _validate_effect_shape(effect)

        status_raw = connection.execute(
            """
            SELECT * FROM produce_exam_status_enchant
             WHERE id=?
            """,
            (STATUS_ENCHANT_ID,),
        ).fetchone()
        if status_raw is None:
            raise StartTurnNoBlockCatalogError(f"missing status row: {STATUS_ENCHANT_ID}")
        status = _status_from_row(status_raw)
        if status.trigger_id != TARGET_TRIGGER_ID:
            raise StartTurnNoBlockCatalogError("status listener trigger changed")
        if status.child_effect_ids != (STATUS_CHILD_ID,):
            raise StartTurnNoBlockCatalogError("status child order or shape changed")
        wrapper = effect_by_id.get(STATUS_WRAPPER_ID)
        if wrapper is None:
            raise StartTurnNoBlockCatalogError(f"missing wrapper effect: {STATUS_WRAPPER_ID}")
        program = StatusProgram(
            status=replace(status, wrapper_effect_id=wrapper.id),
            wrapper=wrapper,
            children=tuple(effect_by_id[child_id] for child_id in status.child_effect_ids),
        )
        if program.co_blockers != (CHILD_BLOCKER,):
            raise StartTurnNoBlockCatalogError("status child is not the exact co-blocked effect")

        effect_groups = {effect.id: effect.effect_group_ids for effect in effect_by_id.values()}
        common_versions: list[CardVersion] = []
        status_versions: list[CardVersion] = []
        for row in card_rows:
            card_id = _text(row["id"], "card.id")
            upgrade = _db_int(row["upgrade_count"], "card.upgrade_count")
            expected_ids = (
                COMMON_EFFECT_IDS[upgrade]
                if card_id == COMMON_CARD_ID
                else STATUS_EFFECT_IDS[upgrade]
            )
            slots = _card_slots(card_id, upgrade, raw_effect_lists[(card_id, upgrade)], effect_groups)
            actual_ids = tuple(slot.effect_id for slot in slots)
            if actual_ids != expected_ids:
                raise StartTurnNoBlockCatalogError(
                    f"{card_id}+{upgrade}: effect order changed: {actual_ids}"
                )
            if card_id == COMMON_CARD_ID:
                if _text(row["plan_type"], "card.plan_type") != PLAN_COMMON:
                    raise StartTurnNoBlockCatalogError("Common card plan type changed")
                if _text(row["play_trigger_id"], "card.play_trigger_id") != TARGET_TRIGGER_ID:
                    raise StartTurnNoBlockCatalogError("card-level trigger reference changed")
                blockers = (CARD_GATE_BLOCKER,)
                direct = True
                co_blocked = False
            else:
                if _text(row["plan_type"], "card.plan_type") != PLAN2:
                    raise StartTurnNoBlockCatalogError("Plan2 card plan type changed")
                if _text(row["play_trigger_id"], "card.play_trigger_id") != "":
                    raise StartTurnNoBlockCatalogError("status card unexpectedly has a card trigger")
                blockers = (STATUS_GATE_BLOCKER, CHILD_BLOCKER)
                direct = False
                co_blocked = True
            version = CardVersion(
                card_id=card_id,
                upgrade_count=upgrade,
                name=_text(row["name"], "card.name"),
                plan_type=_text(row["plan_type"], "card.plan_type"),
                category=_text(row["category"], "card.category"),
                play_trigger_id=_text(row["play_trigger_id"], "card.play_trigger_id"),
                move_position_type=_text(row["move_position_type"], "card.move_position_type"),
                effect_slots=slots,
                blockers=blockers,
                direct=direct,
                co_blocked=co_blocked,
            )
            if version.move_position_type != "ProduceCardMovePositionType_Lost":
                raise StartTurnNoBlockCatalogError("target card move position changed")
            if version.category != "ProduceCardCategory_MentalSkill":
                raise StartTurnNoBlockCatalogError("target card category changed")
            (common_versions if card_id == COMMON_CARD_ID else status_versions).append(version)

    common_versions.sort(key=lambda card: card.upgrade_count)
    status_versions.sort(key=lambda card: card.upgrade_count)
    if len(common_versions) != 4 or len(status_versions) != 4:
        raise StartTurnNoBlockCatalogError("each target family must contain four upgrades")
    effects = tuple(effect_by_id[key] for key in sorted(effect_by_id))
    return NoBlockCatalog(
        trigger=trigger,
        status_program=program,
        effects=effects,
        card_versions=tuple(common_versions),
        status_listener_versions=tuple(status_versions),
    )


load_plan2_start_turn_no_block = load_plan2_start_turn_no_block_catalog


@dataclass(frozen=True, slots=True)
class Plan2StartTurnNoBlockListener:
    """One installed status listener, independent of Plan2 state."""

    status_uid: int
    status_enchant_id: str
    trigger_id: str
    child_effects: tuple[EffectRow, ...]
    wrapper_effect_group_ids: tuple[str, ...]
    turn: int
    budget: ListenerBudget
    turn_count: int = 0
    is_passing_turn_start: bool = False
    child_execution_supported: bool = False
    co_blockers: tuple[str, ...] = (CHILD_BLOCKER,)

    def __post_init__(self) -> None:
        if type(self.status_uid) is not int or self.status_uid < 1:
            raise ValueError("status_uid must be positive")
        if type(self.status_enchant_id) is not str or not self.status_enchant_id:
            raise ValueError("status_enchant_id must be non-empty")
        if self.trigger_id != TARGET_TRIGGER_ID:
            raise ValueError("listener trigger must be the exact target trigger")
        if type(self.turn) is not int or self.turn == 0 or self.turn < -1:
            raise ValueError("turn must be -1 or positive")
        if type(self.turn_count) is not int or self.turn_count < 0:
            raise ValueError("turn_count must be non-negative")
        if not isinstance(self.budget, ListenerBudget):
            raise TypeError("budget must be ListenerBudget")
        if type(self.child_execution_supported) is not bool:
            raise TypeError("child_execution_supported must be bool")
        if self.child_execution_supported:
            raise ValueError("this leaf does not execute the BlockDepend child")
        if self.co_blockers != (CHILD_BLOCKER,):
            raise ValueError("listener must retain the exact co-blocker")
        children = tuple(self.child_effects)
        if any(not isinstance(child, EffectRow) for child in children):
            raise TypeError("child_effects must contain EffectRow values")
        object.__setattr__(self, "child_effects", children)

    @classmethod
    def from_program(cls, status_uid: int, program: StatusProgram) -> "Plan2StartTurnNoBlockListener":
        return cls(
            status_uid=status_uid,
            status_enchant_id=program.status.id,
            trigger_id=program.status.trigger_id,
            child_effects=program.children,
            wrapper_effect_group_ids=program.wrapper.effect_group_ids,
            turn=program.lifetime_turns,
            budget=program.listener_budget,
            co_blockers=program.co_blockers,
        )

    @property
    def can_trigger(self) -> bool:
        return self.budget.can_fire

    @property
    def child_effect_ids(self) -> tuple[str, ...]:
        return ordered_effect_ids(tuple(child.id for child in self.child_effects))

    @property
    def ordered_child_plan(self) -> tuple[EffectRow, ...]:
        return self.child_effects

    @property
    def wrapper_effect_group_execution_order(self) -> tuple[str, ...]:
        return effect_group_execution_order(self.wrapper_effect_group_ids)


def simulate_install_plan2_start_turn_listener(
    status_uid: int,
    program: StatusProgram,
) -> Plan2StartTurnNoBlockListener:
    """Install the status wrapper; its child remains deferred."""

    if program.status.trigger_id != TARGET_TRIGGER_ID:
        raise ValueError("program trigger is not the target StartTurn trigger")
    return Plan2StartTurnNoBlockListener.from_program(status_uid, program)


@dataclass(frozen=True, slots=True)
class StartTurnListenerConsumption:
    before: Plan2StartTurnNoBlockListener
    after: Plan2StartTurnNoBlockListener | None
    native: ListenerConsumption

    @property
    def fired(self) -> bool:
        return self.native.fired

    @property
    def removed(self) -> bool:
        return self.native.removed


def consume_plan2_start_turn_listener(
    listener: Plan2StartTurnNoBlockListener,
    *,
    predicate_matched: bool,
) -> StartTurnListenerConsumption:
    """Apply native SpendCount at command-build time, before child execution."""

    native = consume_listener(listener.budget, predicate_matched=predicate_matched)
    after = None if native.removed else replace(listener, budget=native.after)
    return StartTurnListenerConsumption(listener, after, native)


@dataclass(frozen=True, slots=True)
class StartTurnActivation:
    listener_before: Plan2StartTurnNoBlockListener
    listener_after: Plan2StartTurnNoBlockListener | None
    consumption: ListenerConsumption
    ordered_child_plan: tuple[EffectRow, ...]
    child_execution_supported: bool = False
    co_blockers: tuple[str, ...] = (CHILD_BLOCKER,)

    @property
    def child_effect_ids(self) -> tuple[str, ...]:
        return ordered_effect_ids(tuple(child.id for child in self.ordered_child_plan))

    @property
    def co_blocked(self) -> bool:
        return bool(self.co_blockers)


@dataclass(frozen=True, slots=True)
class StartTurnCapture:
    before_listeners: tuple[Plan2StartTurnNoBlockListener, ...]
    evaluations: tuple[NoBlockEvaluation, ...]
    activations: tuple[StartTurnActivation, ...]
    after_count_spend: tuple[Plan2StartTurnNoBlockListener, ...]
    event_trace: tuple[str, ...]

    @property
    def child_effects_in_order(self) -> tuple[EffectRow, ...]:
        return tuple(
            child
            for activation in self.activations
            for child in activation.ordered_child_plan
        )

    @property
    def child_effect_ids_in_order(self) -> tuple[str, ...]:
        return ordered_effect_ids(tuple(child.id for child in self.child_effects_in_order))

    @property
    def co_blocked(self) -> bool:
        return any(activation.co_blocked for activation in self.activations)

    def execute_children(self) -> tuple[EffectRow, ...]:
        """Never execute the unsupported BlockDepend child in this leaf."""

        return ()


def capture_plan2_start_turn_no_block(
    listeners: Sequence[Plan2StartTurnNoBlockListener],
    *,
    row: StartTurnNoBlockTriggerRow = TARGET_TRIGGER_ROW,
    inputs: StartTurnEvaluationInput = StartTurnEvaluationInput(
        boundary=EvaluationBoundary.STATUS_COMMAND_BUILD
    ),
) -> StartTurnCapture:
    """Capture a stable active snapshot and retain, but do not run, children."""

    before = tuple(listeners)
    remaining: list[Plan2StartTurnNoBlockListener] = []
    evaluations: list[NoBlockEvaluation] = []
    activations: list[StartTurnActivation] = []
    trace = [f"capture:{inputs.phase}", "snapshot:settled"]
    for listener in before:
        evaluation = evaluate_status_no_block_listener(row, inputs)
        evaluations.append(evaluation)
        result = consume_plan2_start_turn_listener(
            listener,
            predicate_matched=evaluation.fires is True,
        )
        if result.fired:
            trace.append(f"spend-count:{listener.status_uid}")
            activations.append(
                StartTurnActivation(
                    listener_before=listener,
                    listener_after=result.after,
                    consumption=result.native,
                    ordered_child_plan=listener.ordered_child_plan,
                    child_execution_supported=False,
                    co_blockers=listener.co_blockers,
                )
            )
            trace.append(f"child-plan-retained:{listener.status_uid}")
        if result.after is not None:
            remaining.append(result.after)
    return StartTurnCapture(
        before_listeners=before,
        evaluations=tuple(evaluations),
        activations=tuple(activations),
        after_count_spend=tuple(remaining),
        event_trace=tuple(trace),
    )


@dataclass(frozen=True, slots=True)
class StartTurnTransition:
    before: Plan2StartTurnNoBlockListener
    after: Plan2StartTurnNoBlockListener | None
    spent: bool
    expired: bool
    fresh: bool
    permanent: bool


def advance_plan2_start_turn_listener_turn(
    listener: Plan2StartTurnNoBlockListener,
) -> StartTurnTransition:
    """Mirror SpendTurn: reset per-turn count and age only prior listeners."""

    next_listener = replace(
        listener,
        turn_count=listener.turn_count + 1,
        budget=reset_listener_turn(listener.budget),
        is_passing_turn_start=True,
    )
    if listener.turn == -1:
        return StartTurnTransition(listener, next_listener, False, False, False, True)
    if not listener.is_passing_turn_start:
        return StartTurnTransition(listener, next_listener, False, False, True, False)
    next_turn = listener.turn - 1
    if next_turn <= 0:
        return StartTurnTransition(listener, None, True, True, False, False)
    return StartTurnTransition(
        listener,
        replace(next_listener, turn=next_turn),
        True,
        False,
        False,
        False,
    )


__all__ = [
    "AGGRESSIVE_EFFECT_TYPE",
    "BLOCK_EFFECT_TYPE",
    "CARD_GATE_BLOCKER",
    "COMMON_CARD_ID",
    "DEFAULT_DATABASE",
    "EvaluationBoundary",
    "EffectRow",
    "FamilyCounts",
    "FIELD_NO_BLOCK",
    "NoBlockCatalog",
    "NoBlockContract",
    "NoBlockEvaluation",
    "PHASE_EXAM_START_TURN",
    "PLAN2",
    "PLAN_COMMON",
    "PlayOrigin",
    "Plan2StartTurnNoBlockListener",
    "STATUS_CARD_ID",
    "STATUS_CHILD_ID",
    "STATUS_CHILD_TYPE",
    "STATUS_GATE_BLOCKER",
    "STATUS_ENCHANT_ID",
    "STATUS_WRAPPER_ID",
    "SourceKind",
    "StartTurnActivation",
    "StartTurnCapture",
    "StartTurnEvaluationInput",
    "StartTurnNoBlockCatalogError",
    "StartTurnNoBlockTriggerRow",
    "StartTurnTransition",
    "StatusEnchantRow",
    "StatusProgram",
    "TARGET_TRIGGER_ID",
    "TARGET_TRIGGER_ROW",
    "advance_plan2_start_turn_listener_turn",
    "capture_plan2_start_turn_no_block",
    "consume_plan2_start_turn_listener",
    "evaluate_card_no_block_gate",
    "evaluate_no_block_trigger",
    "evaluate_status_no_block_listener",
    "fires_no_block",
    "load_plan2_start_turn_no_block",
    "load_plan2_start_turn_no_block_catalog",
    "resolve_no_block_trigger",
    "resolve_plan2_start_turn_no_block_trigger",
    "simulate_install_plan2_start_turn_listener",
]
