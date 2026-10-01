"""Exact bounded catalog for the remaining Plan2 card-owned predicates.

The target owned here is only the card admission predicate.  Direct effects,
payment, movement, timers, and any other card transaction stages remain
explicit companion handoffs; consequently this module never claims a whole
card executable.

All evaluations are pure.  A rejected gate or an unresolved input returns the
same immutable snapshot object as both ``before`` and ``after``.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Final, Mapping

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_aggressive_card_trigger import (
    AggressiveEvaluationInput,
    AggressiveTriggerRow,
    evaluate_aggressive_trigger,
)
from .plan2_card_and_effect_aggressive_up6 import (
    AggressiveGateInput as AggressiveUp6Input,
    EXACT_TARGET_TRIGGER as AGGRESSIVE_UP6_TRIGGER,
    evaluate_card_play_aggressive_up6_trigger,
)
from .plan2_card_trigger_aggressive_up9 import (
    AggressiveUp9GateInput,
    EXACT_TARGET_TRIGGER as AGGRESSIVE_UP9_TRIGGER,
    evaluate_card_play_aggressive_up9_trigger,
)
from .plan2_card_trigger_remaining_turn1 import (
    EXACT_TARGET_TRIGGER as REMAINING_TURN1_TRIGGER,
    RemainingTurnAdmissionInput,
    evaluate_card_play_remaining_turn1_trigger,
)
from .plan2_start_turn_condition_threshold_down import (
    ConditionThresholdDownEvaluationInput,
    ConditionThresholdDownTriggerRow,
    ExecutionMode as ConditionExecutionMode,
    evaluate_condition_threshold_down,
)
from .plan2_start_turn_no_block import (
    PlayOrigin as NoBlockOrigin,
    StartTurnEvaluationInput as NoBlockInput,
    TARGET_TRIGGER_ROW as NO_BLOCK_TRIGGER,
    evaluate_card_no_block_gate,
)
from .plan2_start_turn_review_stamina_recover import (
    CardExecutionKind as ReviewExecutionKind,
    EXACT_TARGET_TRIGGER as REVIEW_UP1_TRIGGER,
    ReviewUp1EvaluationInput,
    evaluate_review_up1_trigger,
)
from .plan2_start_turn_stamina_up_multiple import (
    CardExecutionKind as StaminaExecutionKind,
    Plan2StartTurnSnapshot as StaminaStartTurnSnapshot,
    TARGET_TRIGGER_ROW as STAMINA_UP500_TRIGGER,
    evaluate_plan2_start_turn_stamina_up_multiple,
)
from .plan2_start_turn_turn_progress_up2 import (
    ExecutionMode as StartTurnProgressExecutionMode,
    TurnProgressUp2EvaluationInput,
    evaluate_turn_progress_up2,
)
from .plan2_turn_progress_trigger import (
    TurnProgressEvaluationInput,
    TurnProgressTriggerRow,
    evaluate_turn_progress_trigger,
)


PLAN2_NATIVE_CATALOG_CARD_TRIGGERS_SCHEMA_VERSION: Final = 1
CARD_TRIGGER_TARGET_VERSION_COUNT: Final = 55
CARD_TRIGGER_TARGET_ID_COUNT: Final = 13
CARD_TRIGGER_ADAPTER_ID: Final = "plan2.native.catalog.card_triggers"

PHASE_NONE: Final = "ProduceExamPhaseType_None"
PHASE_START_TURN: Final = "ProduceExamPhaseType_ExamStartTurn"

AGGRESSIVE_UP3_ID: Final = "e_trigger-none-card_play_aggressive_up-3"
AGGRESSIVE_UP6_ID: Final = "e_trigger-none-card_play_aggressive_up-6"
AGGRESSIVE_UP9_ID: Final = "e_trigger-none-card_play_aggressive_up-9"
CONDITION_DOWN1000_ID: Final = (
    "e_trigger-exam_start_turn-condition_threshold_multiple_down-1000"
)
NO_BLOCK_ID: Final = "e_trigger-exam_start_turn-no_block"
STAMINA_UP500_ID: Final = "e_trigger-exam_start_turn-stamina_up_multiple-500"
START_TURN_PROGRESS_UP2_ID: Final = "e_trigger-exam_start_turn-turn_progress_up-2"
START_TURN_REVIEW_UP1_ID: Final = "e_trigger-exam_start_turn-review_up-1"
BLOCK_UP7_ID: Final = "e_trigger-none-block_up-7"
BLOCK_UP15_ID: Final = "e_trigger-none-block_up-15"
BLOCK_UP30_ID: Final = "e_trigger-none-block_up-30"
TURN_PROGRESS_UP2_ID: Final = "e_trigger-none-turn_progress_up-2"
REMAINING_TURN1_ID: Final = "e_trigger-none-remaining_turn-1"

FIELD_AGGRESSIVE: Final = "ProduceExamFieldStatusType_CardPlayAggressiveUp"
FIELD_CONDITION_DOWN: Final = (
    "ProduceExamFieldStatusType_ConditionThresholdMultipleDown"
)
FIELD_NO_BLOCK: Final = "ProduceExamFieldStatusType_NoBlock"
FIELD_STAMINA_MULTIPLE: Final = "ProduceExamFieldStatusType_StaminaUpMultiple"
FIELD_TURN_PROGRESS: Final = "ProduceExamFieldStatusType_TurnProgressUp"
FIELD_REVIEW: Final = "ProduceExamFieldStatusType_ReviewUp"
FIELD_BLOCK: Final = "ProduceExamFieldStatusType_BlockUp"
FIELD_REMAINING_TURN: Final = "ProduceExamFieldStatusType_RemainingTurn"

_PLAN_TYPES: Final = ("ProducePlanType_Common", "ProducePlanType_Plan2")
_MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
_LESSON_UNKNOWN: Final = "ProduceStepLessonType_Unknown"


class Plan2NativeCardTriggerContractError(ValueError):
    """The current Master rows no longer match this bounded catalog."""


class CardPlayOrigin(str, Enum):
    ORDINARY = "ordinary"
    FORCED = "forced"
    EXTRA = "extra"


class CardTriggerBoundary(str, Enum):
    PLAYING_PRE_PAYMENT = "playing-pre-payment"
    SETTLED_START_TURN_PRE_PAYMENT = "settled-start-turn-pre-payment"


@dataclass(frozen=True, slots=True)
class Plan2NativeCardTriggerPredicate:
    trigger_id: str
    phase: str
    field_status_type: str
    threshold: int | None
    comparison: str
    boundary: CardTriggerBoundary
    owner: str
    allowed_origins: tuple[CardPlayOrigin, ...]

    @property
    def ordinary_only(self) -> bool:
        return self.allowed_origins == (CardPlayOrigin.ORDINARY,)


_ALL_ORIGINS: Final = tuple(CardPlayOrigin)
_ORDINARY_ONLY: Final = (CardPlayOrigin.ORDINARY,)


def _predicate(
    trigger_id: str,
    phase: str,
    field: str,
    threshold: int | None,
    comparison: str,
    owner: str,
    origins: tuple[CardPlayOrigin, ...] = _ALL_ORIGINS,
) -> Plan2NativeCardTriggerPredicate:
    boundary = (
        CardTriggerBoundary.PLAYING_PRE_PAYMENT
        if phase == PHASE_NONE
        else CardTriggerBoundary.SETTLED_START_TURN_PRE_PAYMENT
    )
    return Plan2NativeCardTriggerPredicate(
        trigger_id,
        phase,
        field,
        threshold,
        comparison,
        boundary,
        owner,
        origins,
    )


TARGET_PREDICATES: Final = (
    _predicate(
        AGGRESSIVE_UP3_ID,
        PHASE_NONE,
        FIELD_AGGRESSIVE,
        3,
        "signed_current >= threshold",
        "plan2_aggressive_card_trigger",
    ),
    _predicate(
        AGGRESSIVE_UP6_ID,
        PHASE_NONE,
        FIELD_AGGRESSIVE,
        6,
        "signed_current >= threshold",
        "plan2_card_and_effect_aggressive_up6",
    ),
    _predicate(
        AGGRESSIVE_UP9_ID,
        PHASE_NONE,
        FIELD_AGGRESSIVE,
        9,
        "signed_current >= threshold",
        "plan2_card_trigger_aggressive_up9",
    ),
    _predicate(
        CONDITION_DOWN1000_ID,
        PHASE_START_TURN,
        FIELD_CONDITION_DOWN,
        1000,
        "float32(threshold/1000) >= float32(judge_parameter/clear_border)",
        "plan2_start_turn_condition_threshold_down",
    ),
    _predicate(
        NO_BLOCK_ID,
        PHASE_START_TURN,
        FIELD_NO_BLOCK,
        None,
        "signed_current == 0",
        "plan2_start_turn_no_block",
        _ORDINARY_ONLY,
    ),
    _predicate(
        STAMINA_UP500_ID,
        PHASE_START_TURN,
        FIELD_STAMINA_MULTIPLE,
        500,
        "float32(current/max) >= float32(threshold/1000)",
        "plan2_start_turn_stamina_up_multiple",
        _ORDINARY_ONLY,
    ),
    _predicate(
        START_TURN_PROGRESS_UP2_ID,
        PHASE_START_TURN,
        FIELD_TURN_PROGRESS,
        2,
        "signed_current_turn > threshold",
        "plan2_start_turn_turn_progress_up2",
    ),
    _predicate(
        START_TURN_REVIEW_UP1_ID,
        PHASE_START_TURN,
        FIELD_REVIEW,
        1,
        "signed_current >= threshold",
        "plan2_start_turn_review_stamina_recover",
        _ORDINARY_ONLY,
    ),
    _predicate(
        BLOCK_UP7_ID,
        PHASE_NONE,
        FIELD_BLOCK,
        7,
        "signed_current >= threshold",
        "plan2_native_card_trigger_block_predicate",
    ),
    _predicate(
        BLOCK_UP15_ID,
        PHASE_NONE,
        FIELD_BLOCK,
        15,
        "signed_current >= threshold",
        "plan2_native_card_trigger_block_predicate",
    ),
    _predicate(
        BLOCK_UP30_ID,
        PHASE_NONE,
        FIELD_BLOCK,
        30,
        "signed_current >= threshold",
        "plan2_native_card_trigger_block_predicate",
    ),
    _predicate(
        TURN_PROGRESS_UP2_ID,
        PHASE_NONE,
        FIELD_TURN_PROGRESS,
        2,
        "signed_current_turn > threshold",
        "plan2_turn_progress_trigger",
    ),
    _predicate(
        REMAINING_TURN1_ID,
        PHASE_NONE,
        FIELD_REMAINING_TURN,
        1,
        "signed_remaining_turn <= threshold",
        "plan2_card_trigger_remaining_turn1",
    ),
)
TARGET_TRIGGER_IDS: Final = tuple(row.trigger_id for row in TARGET_PREDICATES)
_PREDICATE_BY_ID: Final = MappingProxyType(
    {row.trigger_id: row for row in TARGET_PREDICATES}
)


def _refs(card_id: str, upgrades: int = 4) -> tuple[tuple[str, int], ...]:
    return tuple((card_id, upgrade) for upgrade in range(upgrades))


EXPECTED_TRIGGER_BY_REF: Final = MappingProxyType(
    {
        **{ref: AGGRESSIVE_UP3_ID for ref in _refs("p_card-02-act-0_037")},
        **{ref: AGGRESSIVE_UP3_ID for ref in _refs("p_card-02-act-3_045")},
        ("p_card-02-act-100_009", 0): AGGRESSIVE_UP6_ID,
        **{ref: AGGRESSIVE_UP6_ID for ref in _refs("p_card-02-sup-3_181")},
        **{ref: AGGRESSIVE_UP9_ID for ref in _refs("p_card-02-ido-3_140")},
        ("p_card-02-men-100_011", 0): AGGRESSIVE_UP9_ID,
        **{ref: CONDITION_DOWN1000_ID for ref in _refs("p_card-00-sup-2_025")},
        **{ref: NO_BLOCK_ID for ref in _refs("p_card-00-sup-2_026")},
        **{ref: STAMINA_UP500_ID for ref in _refs("p_card-00-sup-2_027")},
        **{
            ref: START_TURN_PROGRESS_UP2_ID
            for ref in _refs("p_card-00-sup-2_028")
        },
        **{ref: START_TURN_REVIEW_UP1_ID for ref in _refs("p_card-02-ido-3_081")},
        **{ref: BLOCK_UP7_ID for ref in _refs("p_card-02-act-3_039")},
        **{ref: BLOCK_UP15_ID for ref in _refs("p_card-02-ido-3_190")},
        **{ref: BLOCK_UP30_ID for ref in _refs("p_card-02-act-1_037")},
        **{ref: TURN_PROGRESS_UP2_ID for ref in _refs("p_card-02-sup-3_182")},
        ("p_card-02-act-100_010", 0): REMAINING_TURN1_ID,
    }
)


@dataclass(frozen=True, slots=True)
class Plan2NativeCardTriggerMasterRow:
    trigger_id: str
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


@dataclass(frozen=True, slots=True)
class Plan2NativeCardTriggerEffectSlot:
    index: int
    trigger_id: str
    effect_id: str
    effect_type: str
    hide_icon: bool
    once: bool


@dataclass(frozen=True, slots=True)
class Plan2NativeCardTriggerHandoff:
    card_id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    predicate: Plan2NativeCardTriggerPredicate
    companion_effect_slots: tuple[Plan2NativeCardTriggerEffectSlot, ...]
    target_predicate_executable: bool = True
    companion_owner: str = "other-native-effect-owners"
    whole_card_executable: bool = False

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade


@dataclass(frozen=True, slots=True)
class Plan2NativeCardTriggerAccounting:
    affected_versions: int
    target_predicate_executable_versions: int
    companion_handoff_versions: int
    whole_card_executable_versions: int
    per_trigger_versions: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class Plan2NativeCardTriggerCatalog:
    schema_version: int
    adapter_id: str
    trigger_rows: tuple[Plan2NativeCardTriggerMasterRow, ...]
    handoffs: tuple[Plan2NativeCardTriggerHandoff, ...]
    accounting: Plan2NativeCardTriggerAccounting

    def resolve(self, card_id: str, upgrade: int) -> Plan2NativeCardTriggerHandoff:
        for handoff in self.handoffs:
            if handoff.card_id == card_id and handoff.upgrade == upgrade:
                return handoff
        raise KeyError((card_id, upgrade))

    def trigger(self, trigger_id: str) -> Plan2NativeCardTriggerMasterRow:
        for row in self.trigger_rows:
            if row.trigger_id == trigger_id:
                return row
        raise KeyError(trigger_id)


def _plain_i32(value: object, label: str) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise Plan2NativeCardTriggerContractError(f"{label} must be a plain integer")
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2NativeCardTriggerContractError(f"{label} is outside Int32")
    return value


def _json_list(value: object, label: str) -> tuple[object, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2NativeCardTriggerContractError(f"{label} is not valid JSON") from error
    if not isinstance(parsed, list):
        raise Plan2NativeCardTriggerContractError(f"{label} must be a JSON array")
    return tuple(parsed)


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_list(value, label)
    if any(type(item) is not str for item in parsed):
        raise Plan2NativeCardTriggerContractError(f"{label} must contain strings")
    return parsed  # type: ignore[return-value]


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_list(value, label)
    return tuple(_plain_i32(item, label) for item in parsed)


def _raw_object(value: object, label: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2NativeCardTriggerContractError(f"{label} is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise Plan2NativeCardTriggerContractError(f"{label} must be a JSON object")
    return parsed


def _assert_raw(raw: Mapping[str, object], expected: Mapping[str, object], label: str) -> None:
    for key, value in expected.items():
        if raw.get(key) != value:
            raise Plan2NativeCardTriggerContractError(
                f"{label}.{key} changed: expected {value!r}, got {raw.get(key)!r}"
            )


def _load_trigger(row: sqlite3.Row) -> Plan2NativeCardTriggerMasterRow:
    result = Plan2NativeCardTriggerMasterRow(
        trigger_id=str(row["id"]),
        phase_types=_string_tuple(row["phase_types_json"], "trigger.phase_types"),
        phase_values=_int_tuple(row["phase_values_json"], "trigger.phase_values"),
        field_status_check_types=_string_tuple(
            row["field_status_check_types_json"], "trigger.field_status_check_types"
        ),
        field_status_types=_string_tuple(
            row["field_status_types_json"], "trigger.field_status_types"
        ),
        field_status_values=_int_tuple(
            row["field_status_values_json"], "trigger.field_status_values"
        ),
        field_status_card_search_ids=_string_tuple(
            row["field_status_produce_card_search_ids_json"],
            "trigger.field_status_card_search_ids",
        ),
        produce_card_search_id=str(row["produce_card_search_id"]),
        upper_search_count=_plain_i32(row["upper_search_count"], "upper_search_count"),
        lower_search_count=_plain_i32(row["lower_search_count"], "lower_search_count"),
        card_move_position_type=str(row["card_move_position_type"]),
        effect_types=_string_tuple(row["effect_types_json"], "trigger.effect_types"),
        lesson_type=str(row["lesson_type"]),
    )
    predicate = _PREDICATE_BY_ID.get(result.trigger_id)
    if predicate is None:
        raise Plan2NativeCardTriggerContractError(
            f"unknown target trigger: {result.trigger_id}"
        )
    expected_values = () if predicate.threshold is None else (predicate.threshold,)
    expected = Plan2NativeCardTriggerMasterRow(
        predicate.trigger_id,
        (predicate.phase,),
        (),
        (),
        (predicate.field_status_type,),
        expected_values,
        (),
        "",
        0,
        0,
        _MOVE_UNKNOWN,
        (),
        _LESSON_UNKNOWN,
    )
    if result != expected:
        raise Plan2NativeCardTriggerContractError(
            f"target trigger shape changed: {result.trigger_id}"
        )
    raw = _raw_object(row["raw_json"], f"trigger[{result.trigger_id}].raw_json")
    _assert_raw(
        raw,
        {
            "id": result.trigger_id,
            "phaseTypes": list(result.phase_types),
            "phaseValues": list(result.phase_values),
            "fieldStatusCheckTypes": list(result.field_status_check_types),
            "fieldStatusTypes": list(result.field_status_types),
            "fieldStatusValues": list(result.field_status_values),
            "fieldStatusProduceCardSearchIds": list(result.field_status_card_search_ids),
            "produceCardSearchId": result.produce_card_search_id,
            "upperSearchCount": result.upper_search_count,
            "lowerSearchCount": result.lower_search_count,
            "cardMovePositionType": result.card_move_position_type,
            "effectTypes": list(result.effect_types),
            "lessonType": result.lesson_type,
        },
        f"trigger[{result.trigger_id}]",
    )
    return result


def _effect_slots(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> tuple[Plan2NativeCardTriggerEffectSlot, ...]:
    raw_slots = _json_list(row["play_effects_json"], "card.play_effects")
    slots: list[Plan2NativeCardTriggerEffectSlot] = []
    for index, value in enumerate(raw_slots):
        if not isinstance(value, dict):
            raise Plan2NativeCardTriggerContractError("card.play_effects must contain objects")
        expected_keys = {
            "produceExamTriggerId",
            "produceExamEffectId",
            "hideIcon",
            "isOncePlayEffect",
        }
        if set(value) != expected_keys:
            raise Plan2NativeCardTriggerContractError(
                f"card.play_effects[{index}] keys changed"
            )
        trigger_id = value["produceExamTriggerId"]
        effect_id = value["produceExamEffectId"]
        hide_icon = value["hideIcon"]
        once = value["isOncePlayEffect"]
        if type(trigger_id) is not str or type(effect_id) is not str or not effect_id:
            raise Plan2NativeCardTriggerContractError(
                f"card.play_effects[{index}] identifiers are invalid"
            )
        if type(hide_icon) is not bool or type(once) is not bool:
            raise Plan2NativeCardTriggerContractError(
                f"card.play_effects[{index}] flags are invalid"
            )
        effect = connection.execute(
            "SELECT effect_type FROM effect WHERE id = ?", (effect_id,)
        ).fetchone()
        if effect is None or type(effect[0]) is not str or not effect[0]:
            raise Plan2NativeCardTriggerContractError(
                f"missing companion effect: {effect_id}"
            )
        slots.append(
            Plan2NativeCardTriggerEffectSlot(
                index, trigger_id, effect_id, effect[0], hide_icon, once
            )
        )
    if not slots:
        raise Plan2NativeCardTriggerContractError(
            f"target card has no explicit companion effects: {row['id']}+{row['upgrade_count']}"
        )
    return tuple(slots)


def load_plan2_native_catalog_card_triggers(
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeCardTriggerCatalog:
    """Load all 55 exact current-Master versions or fail closed."""

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in TARGET_TRIGGER_IDS)
        raw_triggers = connection.execute(
            f"SELECT * FROM produce_exam_trigger WHERE id IN ({placeholders}) ORDER BY id",
            TARGET_TRIGGER_IDS,
        ).fetchall()
        if len(raw_triggers) != CARD_TRIGGER_TARGET_ID_COUNT:
            raise Plan2NativeCardTriggerContractError(
                f"expected 13 target triggers, found {len(raw_triggers)}"
            )
        triggers = tuple(_load_trigger(row) for row in raw_triggers)

        raw_cards = connection.execute(
            f"""
            SELECT * FROM card
            WHERE plan_type IN (?, ?) AND play_trigger_id IN ({placeholders})
            ORDER BY id, upgrade_count
            """,
            (*_PLAN_TYPES, *TARGET_TRIGGER_IDS),
        ).fetchall()
        found_refs = tuple((str(row["id"]), row["upgrade_count"]) for row in raw_cards)
        expected_refs = tuple(sorted(EXPECTED_TRIGGER_BY_REF))
        if found_refs != expected_refs:
            missing = tuple(sorted(set(expected_refs) - set(found_refs)))
            extra = tuple(sorted(set(found_refs) - set(expected_refs)))
            raise Plan2NativeCardTriggerContractError(
                f"target card-version set changed; missing={missing!r}; extra={extra!r}"
            )

        handoffs: list[Plan2NativeCardTriggerHandoff] = []
        for row in raw_cards:
            card_id = str(row["id"])
            upgrade = _plain_i32(row["upgrade_count"], "card.upgrade_count")
            ref = card_id, upgrade
            trigger_id = str(row["play_trigger_id"])
            if EXPECTED_TRIGGER_BY_REF.get(ref) != trigger_id:
                raise Plan2NativeCardTriggerContractError(
                    f"card trigger link changed: {ref!r} -> {trigger_id}"
                )
            predicate = _PREDICATE_BY_ID[trigger_id]
            slots = _effect_slots(connection, row)
            raw_card = _raw_object(row["raw_json"], f"card[{ref!r}].raw_json")
            raw_slots = json.loads(str(row["play_effects_json"]))
            _assert_raw(
                raw_card,
                {
                    "id": card_id,
                    "upgradeCount": upgrade,
                    "name": str(row["name"]),
                    "planType": str(row["plan_type"]),
                    "category": str(row["category"]),
                    "stamina": row["stamina"],
                    "costType": str(row["cost_type"]),
                    "costValue": row["cost_value"],
                    "playProduceExamTriggerId": trigger_id,
                    "playEffects": raw_slots,
                    "playMovePositionType": str(row["move_position_type"]),
                },
                f"card[{ref!r}]",
            )
            handoffs.append(
                Plan2NativeCardTriggerHandoff(
                    card_id,
                    upgrade,
                    str(row["name"]),
                    str(row["plan_type"]),
                    str(row["category"]),
                    predicate,
                    slots,
                )
            )

    counts = Counter(item.predicate.trigger_id for item in handoffs)
    accounting = Plan2NativeCardTriggerAccounting(
        affected_versions=len(handoffs),
        target_predicate_executable_versions=sum(
            item.target_predicate_executable for item in handoffs
        ),
        companion_handoff_versions=sum(bool(item.companion_effect_slots) for item in handoffs),
        whole_card_executable_versions=sum(item.whole_card_executable for item in handoffs),
        per_trigger_versions=tuple((key, counts[key]) for key in TARGET_TRIGGER_IDS),
    )
    if accounting != Plan2NativeCardTriggerAccounting(
        55,
        55,
        55,
        0,
        tuple((key, counts[key]) for key in TARGET_TRIGGER_IDS),
    ):
        raise Plan2NativeCardTriggerContractError("card-trigger accounting changed")
    return Plan2NativeCardTriggerCatalog(
        PLAN2_NATIVE_CATALOG_CARD_TRIGGERS_SCHEMA_VERSION,
        CARD_TRIGGER_ADAPTER_ID,
        triggers,
        tuple(handoffs),
        accounting,
    )


compile_plan2_native_catalog_card_triggers = load_plan2_native_catalog_card_triggers


@dataclass(frozen=True, slots=True)
class Plan2NativeCardTriggerSnapshot:
    """Caller-supplied read-only predicate snapshot.

    Fields not used by the selected predicate may remain ``None``.  Values are
    checked as signed Int32 immediately before delegation.
    """

    phase: object
    boundary: object
    origin: object = CardPlayOrigin.ORDINARY
    aggressive: object | None = None
    block: object | None = None
    current_stamina: object | None = None
    max_stamina: object | None = None
    current_turn: object | None = None
    remaining_turn: object | None = None
    review: object | None = None
    judge_parameter: object | None = None
    clear_border: object | None = None
    global_card_play_count: object = 0
    turn_card_play_count: object = 0


@dataclass(frozen=True, slots=True)
class Plan2NativeCardTriggerEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    phase: object
    boundary: object
    origin: object
    before: Plan2NativeCardTriggerSnapshot
    after: Plan2NativeCardTriggerSnapshot
    target_predicate_executable: bool
    companion_effects_executed: bool
    whole_card_executable: bool
    reasons: tuple[str, ...]

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    @property
    def state_unchanged(self) -> bool:
        return self.before is self.after


def _origin(value: object) -> CardPlayOrigin | None:
    try:
        return CardPlayOrigin(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _runtime_i32(value: object, label: str, reasons: list[str]) -> int | None:
    if isinstance(value, bool) or type(value) is not int:
        reasons.append(f"{label}-is-not-plain-int32")
        return None
    if not INT32_MIN <= value <= INT32_MAX:
        reasons.append(f"{label}-is-outside-int32")
        return None
    return value


def _unsupported(
    trigger_id: str,
    snapshot: Plan2NativeCardTriggerSnapshot,
    reasons: tuple[str, ...] | list[str],
) -> Plan2NativeCardTriggerEvaluation:
    return Plan2NativeCardTriggerEvaluation(
        trigger_id,
        False,
        None,
        snapshot.phase,
        snapshot.boundary,
        snapshot.origin,
        snapshot,
        snapshot,
        False,
        False,
        False,
        tuple(dict.fromkeys(reasons)),
    )


def _delegate_row(
    cls: type[AggressiveTriggerRow] | type[ConditionThresholdDownTriggerRow] | type[TurnProgressTriggerRow],
    predicate: Plan2NativeCardTriggerPredicate,
) -> AggressiveTriggerRow | ConditionThresholdDownTriggerRow | TurnProgressTriggerRow:
    return cls(
        id=predicate.trigger_id,
        phase_types=(predicate.phase,),
        phase_values=(),
        field_status_check_types=(),
        field_status_types=(predicate.field_status_type,),
        field_status_values=(() if predicate.threshold is None else (predicate.threshold,)),
        field_status_card_search_ids=(),
        produce_card_search_id="",
        upper_search_count=0,
        lower_search_count=0,
        card_move_position_type=_MOVE_UNKNOWN,
        effect_types=(),
        lesson_type=_LESSON_UNKNOWN,
    )


_AGGRESSIVE_UP3_TRIGGER: Final = _delegate_row(
    AggressiveTriggerRow, _PREDICATE_BY_ID[AGGRESSIVE_UP3_ID]
)
_CONDITION_DOWN_TRIGGER: Final = _delegate_row(
    ConditionThresholdDownTriggerRow, _PREDICATE_BY_ID[CONDITION_DOWN1000_ID]
)
_START_TURN_PROGRESS_TRIGGER: Final = _delegate_row(
    TurnProgressTriggerRow, _PREDICATE_BY_ID[START_TURN_PROGRESS_UP2_ID]
)
_TURN_PROGRESS_TRIGGER: Final = _delegate_row(
    TurnProgressTriggerRow, _PREDICATE_BY_ID[TURN_PROGRESS_UP2_ID]
)


def evaluate_plan2_native_card_trigger(
    source: Plan2NativeCardTriggerHandoff | Plan2NativeCardTriggerPredicate | str | object,
    snapshot: Plan2NativeCardTriggerSnapshot,
) -> Plan2NativeCardTriggerEvaluation:
    """Evaluate one known card gate and never mutate or execute companions."""

    if not isinstance(snapshot, Plan2NativeCardTriggerSnapshot):
        raise TypeError("snapshot must be Plan2NativeCardTriggerSnapshot")
    if isinstance(source, Plan2NativeCardTriggerHandoff):
        predicate = source.predicate
    elif isinstance(source, Plan2NativeCardTriggerPredicate):
        predicate = source
    elif type(source) is str:
        predicate = _PREDICATE_BY_ID.get(source)
    else:
        predicate = None
    trigger_id = (
        source if type(source) is str else getattr(predicate, "trigger_id", "unknown")
    )
    if predicate is None or _PREDICATE_BY_ID.get(predicate.trigger_id) != predicate:
        return _unsupported(str(trigger_id), snapshot, ("unknown-trigger-failed-closed",))

    reasons: list[str] = []
    origin = _origin(snapshot.origin)
    if snapshot.phase != predicate.phase:
        reasons.append("evaluation-phase-mismatch")
    boundary_value = (
        snapshot.boundary.value
        if isinstance(snapshot.boundary, CardTriggerBoundary)
        else snapshot.boundary
    )
    if boundary_value != predicate.boundary.value:
        reasons.append("snapshot-boundary-mismatch")
    if origin is None:
        reasons.append("unknown-card-origin")
    elif origin not in predicate.allowed_origins:
        reasons.append("card-origin-unproven-for-trigger")
    global_count = _runtime_i32(
        snapshot.global_card_play_count, "global-card-play-count", reasons
    )
    turn_count = _runtime_i32(
        snapshot.turn_card_play_count, "turn-card-play-count", reasons
    )
    if reasons:
        return _unsupported(predicate.trigger_id, snapshot, reasons)
    assert origin is not None and global_count is not None and turn_count is not None

    exact: object
    if predicate.trigger_id in (AGGRESSIVE_UP3_ID, AGGRESSIVE_UP6_ID, AGGRESSIVE_UP9_ID):
        value = _runtime_i32(snapshot.aggressive, "aggressive", reasons)
        if value is None:
            return _unsupported(predicate.trigger_id, snapshot, reasons)
        native_origin = {
            CardPlayOrigin.ORDINARY: "normal",
            CardPlayOrigin.FORCED: "forced",
            CardPlayOrigin.EXTRA: "extra",
        }[origin]
        if predicate.trigger_id == AGGRESSIVE_UP3_ID:
            exact = evaluate_aggressive_trigger(
                _AGGRESSIVE_UP3_TRIGGER,
                AggressiveEvaluationInput(
                    value,
                    global_card_play_count=global_count,
                    turn_card_play_count=turn_count,
                ),
            )
        elif predicate.trigger_id == AGGRESSIVE_UP6_ID:
            exact = evaluate_card_play_aggressive_up6_trigger(
                AggressiveUp6Input(
                    value,
                    card_origin=native_origin,  # type: ignore[arg-type]
                    global_card_play_count=global_count,
                    turn_card_play_count=turn_count,
                ),
                AGGRESSIVE_UP6_TRIGGER,
            )
        else:
            exact = evaluate_card_play_aggressive_up9_trigger(
                AggressiveUp9GateInput(
                    value,
                    card_origin=native_origin,  # type: ignore[arg-type]
                    global_card_play_count=global_count,
                    turn_card_play_count=turn_count,
                ),
                AGGRESSIVE_UP9_TRIGGER,
            )
    elif predicate.trigger_id == CONDITION_DOWN1000_ID:
        judge = _runtime_i32(snapshot.judge_parameter, "judge-parameter", reasons)
        border = _runtime_i32(snapshot.clear_border, "clear-border", reasons)
        if judge is None or border is None:
            return _unsupported(predicate.trigger_id, snapshot, reasons)
        exact = evaluate_condition_threshold_down(
            _CONDITION_DOWN_TRIGGER,
            ConditionThresholdDownEvaluationInput(
                judge,
                border,
                execution_mode=ConditionExecutionMode(origin.value),
            ),
        )
    elif predicate.trigger_id == NO_BLOCK_ID:
        block = _runtime_i32(snapshot.block, "block", reasons)
        if block is None:
            return _unsupported(predicate.trigger_id, snapshot, reasons)
        exact = evaluate_card_no_block_gate(
            NO_BLOCK_TRIGGER,
            NoBlockInput(block=block, play_origin=NoBlockOrigin(origin.value)),
        )
    elif predicate.trigger_id == STAMINA_UP500_ID:
        current = _runtime_i32(snapshot.current_stamina, "current-stamina", reasons)
        maximum = _runtime_i32(snapshot.max_stamina, "max-stamina", reasons)
        if current is None or maximum is None:
            return _unsupported(predicate.trigger_id, snapshot, reasons)
        exact = evaluate_plan2_start_turn_stamina_up_multiple(
            STAMINA_UP500_TRIGGER,
            StaminaStartTurnSnapshot(current, maximum),
            execution_kind=StaminaExecutionKind.ORDINARY,
        )
    elif predicate.trigger_id == START_TURN_PROGRESS_UP2_ID:
        current_turn = _runtime_i32(snapshot.current_turn, "current-turn", reasons)
        if current_turn is None:
            return _unsupported(predicate.trigger_id, snapshot, reasons)
        exact = evaluate_turn_progress_up2(
            _START_TURN_PROGRESS_TRIGGER,
            TurnProgressUp2EvaluationInput(
                current_turn,
                execution_mode=StartTurnProgressExecutionMode(origin.value),
            ),
        )
    elif predicate.trigger_id == START_TURN_REVIEW_UP1_ID:
        review = _runtime_i32(snapshot.review, "review", reasons)
        if review is None:
            return _unsupported(predicate.trigger_id, snapshot, reasons)
        exact = evaluate_review_up1_trigger(
            ReviewUp1EvaluationInput(
                review,
                execution_kind=ReviewExecutionKind.ORDINARY,
            ),
            REVIEW_UP1_TRIGGER,
        )
    elif predicate.trigger_id in (BLOCK_UP7_ID, BLOCK_UP15_ID, BLOCK_UP30_ID):
        block = _runtime_i32(snapshot.block, "block", reasons)
        if block is None:
            return _unsupported(predicate.trigger_id, snapshot, reasons)
        assert predicate.threshold is not None
        return Plan2NativeCardTriggerEvaluation(
            predicate.trigger_id,
            True,
            block >= predicate.threshold,
            snapshot.phase,
            snapshot.boundary,
            snapshot.origin,
            snapshot,
            snapshot,
            True,
            False,
            False,
            (),
        )
    elif predicate.trigger_id == TURN_PROGRESS_UP2_ID:
        current_turn = _runtime_i32(snapshot.current_turn, "current-turn", reasons)
        if current_turn is None:
            return _unsupported(predicate.trigger_id, snapshot, reasons)
        exact = evaluate_turn_progress_trigger(
            _TURN_PROGRESS_TRIGGER,
            TurnProgressEvaluationInput(
                current_turn,
                global_card_play_count=global_count,
                turn_card_play_count=turn_count,
            ),
        )
    elif predicate.trigger_id == REMAINING_TURN1_ID:
        remaining = _runtime_i32(snapshot.remaining_turn, "remaining-turn", reasons)
        if remaining is None:
            return _unsupported(predicate.trigger_id, snapshot, reasons)
        native_origin = {
            CardPlayOrigin.ORDINARY: "normal",
            CardPlayOrigin.FORCED: "forced",
            CardPlayOrigin.EXTRA: "extra-play",
        }[origin]
        exact = evaluate_card_play_remaining_turn1_trigger(
            RemainingTurnAdmissionInput(
                remaining,
                card_origin=native_origin,  # type: ignore[arg-type]
            ),
            REMAINING_TURN1_TRIGGER,
        )
    else:  # pragma: no cover - guarded by the immutable predicate table
        return _unsupported(predicate.trigger_id, snapshot, ("unknown-trigger-failed-closed",))

    supported = bool(getattr(exact, "supported", False))
    fires = getattr(exact, "fires", None)
    exact_reasons = tuple(getattr(exact, "reasons", ()))
    if not supported or type(fires) is not bool:
        return _unsupported(
            predicate.trigger_id,
            snapshot,
            exact_reasons or ("exact-trigger-delegate-failed-closed",),
        )
    return Plan2NativeCardTriggerEvaluation(
        predicate.trigger_id,
        True,
        fires,
        snapshot.phase,
        snapshot.boundary,
        snapshot.origin,
        snapshot,
        snapshot,
        True,
        False,
        False,
        (),
    )


__all__ = [
    "AGGRESSIVE_UP3_ID",
    "AGGRESSIVE_UP6_ID",
    "AGGRESSIVE_UP9_ID",
    "BLOCK_UP7_ID",
    "BLOCK_UP15_ID",
    "BLOCK_UP30_ID",
    "CARD_TRIGGER_ADAPTER_ID",
    "CARD_TRIGGER_TARGET_ID_COUNT",
    "CARD_TRIGGER_TARGET_VERSION_COUNT",
    "CONDITION_DOWN1000_ID",
    "CardPlayOrigin",
    "CardTriggerBoundary",
    "EXPECTED_TRIGGER_BY_REF",
    "NO_BLOCK_ID",
    "PHASE_NONE",
    "PHASE_START_TURN",
    "PLAN2_NATIVE_CATALOG_CARD_TRIGGERS_SCHEMA_VERSION",
    "Plan2NativeCardTriggerAccounting",
    "Plan2NativeCardTriggerCatalog",
    "Plan2NativeCardTriggerContractError",
    "Plan2NativeCardTriggerEffectSlot",
    "Plan2NativeCardTriggerEvaluation",
    "Plan2NativeCardTriggerHandoff",
    "Plan2NativeCardTriggerMasterRow",
    "Plan2NativeCardTriggerPredicate",
    "Plan2NativeCardTriggerSnapshot",
    "REMAINING_TURN1_ID",
    "STAMINA_UP500_ID",
    "START_TURN_PROGRESS_UP2_ID",
    "START_TURN_REVIEW_UP1_ID",
    "TARGET_PREDICATES",
    "TARGET_TRIGGER_IDS",
    "TURN_PROGRESS_UP2_ID",
    "compile_plan2_native_catalog_card_triggers",
    "evaluate_plan2_native_card_trigger",
    "load_plan2_native_catalog_card_triggers",
]
