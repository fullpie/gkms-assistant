"""Bounded catalog for Plan2 triggered direct-effect links.

This catalog owns only the predicate/listener handoff on a card ``playEffects``
link whose ``produceExamTriggerId`` is non-empty.  The linked effect is retained
as an ordered companion and is never interpreted as a trigger child.  No row
in this module claims payment, effect execution, movement, or a whole-card
transaction.

Only occurrence families with an existing bounded Plan2 effect-trigger
evaluator are executable.  Similar-looking field predicates without that
effect-slot proof remain explicit blockers.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import Final, Mapping

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_aggressive_card_trigger import AggressiveTriggerRow
from .plan2_card_and_effect_aggressive_up6 import (
    AggressiveGateBoundary as AggressiveUp6Boundary,
    AggressiveGateInput as AggressiveUp6Input,
    EXACT_TARGET_TRIGGER as AGGRESSIVE_UP6_TRIGGER,
    evaluate_direct_effect_aggressive_up6_trigger,
)
from .plan2_card_play_stamina_trigger import (
    CandidateBoundary as CardPlayStaminaBoundary,
    evaluate_plan2_card_play_stamina_trigger,
)
from .plan2_effect_trigger_aggressive_up3 import (
    EXACT_TARGET_TRIGGER as AGGRESSIVE_UP3_TRIGGER,
    EffectTriggerBoundary as AggressiveUp3Boundary,
    EffectTriggerEvaluationInput as AggressiveUp3Input,
    evaluate_effect_trigger_aggressive_up3,
)
from .plan2_effect_triggers_aggressive_up9 import (
    EffectTriggerBoundary as AggressiveUp9Boundary,
    EffectTriggerEvaluationInput as AggressiveUp9Input,
    evaluate_aggressive_effect_trigger,
)
from .plan2_effect_triggers_stamina_thresholds import (
    CardOrigin as StaminaThresholdOrigin,
    TriggerSnapshotBoundary as StaminaThresholdBoundary,
    evaluate_effect_trigger as evaluate_stamina_threshold_effect_trigger,
)
from .plan2_stamina_up500_block_fix import (
    CardExecutionKind as StaminaUp500ExecutionKind,
    TriggerSnapshotBoundary as StaminaUp500Boundary,
    evaluate_plan2_none_stamina_up_multiple,
)


PLAN2_NATIVE_CATALOG_EFFECT_TRIGGERS_SCHEMA_VERSION: Final = 1
EFFECT_TRIGGER_ADAPTER_ID: Final = "plan2.native.catalog.effect_triggers"
EXPECTED_OCCURRENCE_COUNT: Final = 123
EXPECTED_UNIQUE_EFFECT_COUNT: Final = 57
EXPECTED_TRIGGER_ID_COUNT: Final = 17
EXPECTED_UNIQUE_VERSION_COUNT: Final = 103
EXPECTED_CARD_ID_COUNT: Final = 26
EXPECTED_EXECUTABLE_OCCURRENCE_COUNT: Final = 36
EXPECTED_EXECUTABLE_VERSION_COUNT: Final = 24
EXPECTED_EXECUTABLE_EFFECT_COUNT: Final = 16

PHASE_NONE: Final = "ProduceExamPhaseType_None"
PHASE_EXAM_CARD_PLAY: Final = "ProduceExamPhaseType_ExamCardPlay"
_PLAN_TYPES: Final = ("ProducePlanType_Common", "ProducePlanType_Plan2")

TRIGGER_AGGRESSIVE_UP3: Final = "e_trigger-none-card_play_aggressive_up-3"
TRIGGER_AGGRESSIVE_UP6: Final = "e_trigger-none-card_play_aggressive_up-6"
TRIGGER_AGGRESSIVE_UP9: Final = "e_trigger-none-card_play_aggressive_up-9"
TRIGGER_NOT_AGGRESSIVE_UP9: Final = (
    "e_trigger-none-not-card_play_aggressive_up-9"
)
TRIGGER_STAMINA_UP500: Final = "e_trigger-none-stamina_up_multiple-500"
TRIGGER_STAMINA_UP800: Final = "e_trigger-none-stamina_up_multiple-800"
TRIGGER_STAMINA_UP1000: Final = "e_trigger-none-stamina_up_multiple-1000"
TRIGGER_CARD_PLAY_STAMINA_UP500: Final = (
    "e_trigger-exam_card_play-stamina_up_multiple-500"
)


EXPECTED_TRIGGER_OCCURRENCE_COUNTS: Final = (
    ("e_trigger-none-review_up-10", 20),
    ("e_trigger-exam_card_play-card_play_aggressive_up-3", 15),
    ("e_trigger-exam_card_play-card_play_aggressive_up-6", 13),
    (
        "e_trigger-none-card_search_count_up-2-p_card_search-trouble-not_lost",
        12,
    ),
    ("e_trigger-exam_card_play-review_up-3", 10),
    (TRIGGER_STAMINA_UP500, 8),
    ("e_trigger-none-review_up-15", 8),
    (TRIGGER_CARD_PLAY_STAMINA_UP500, 4),
    (TRIGGER_NOT_AGGRESSIVE_UP9, 4),
    (TRIGGER_AGGRESSIVE_UP9, 4),
    (TRIGGER_STAMINA_UP800, 4),
    (TRIGGER_STAMINA_UP1000, 4),
    ("e_trigger-none-block_up-30", 4),
    (TRIGGER_AGGRESSIVE_UP6, 4),
    (TRIGGER_AGGRESSIVE_UP3, 4),
    ("e_trigger-exam_card_play-review_up-10", 3),
    ("e_trigger-exam_card_play-review_up-1", 2),
)
TARGET_TRIGGER_IDS: Final = tuple(
    trigger_id for trigger_id, _ in EXPECTED_TRIGGER_OCCURRENCE_COUNTS
)

_FIELD_AGGRESSIVE: Final = "ProduceExamFieldStatusType_CardPlayAggressiveUp"
_FIELD_REVIEW: Final = "ProduceExamFieldStatusType_ReviewUp"
_FIELD_STAMINA: Final = "ProduceExamFieldStatusType_StaminaUpMultiple"
_FIELD_BLOCK: Final = "ProduceExamFieldStatusType_BlockUp"
_FIELD_SEARCH_COUNT: Final = "ProduceExamFieldStatusType_CardSearchCountUp"
_CHECK_NOT: Final = "ProduceExamTriggerCheckType_Not"
_MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
_LESSON_UNKNOWN: Final = "ProduceStepLessonType_Unknown"
_TROUBLE_SEARCH: Final = "p_card_search-trouble-not_lost"


def _expected_trigger_shape(
    phase: str,
    field: str,
    threshold: int,
    *,
    check_types: tuple[str, ...] = (),
    field_search_ids: tuple[str, ...] = (),
) -> tuple[object, ...]:
    return phase, check_types, field, threshold, field_search_ids


_EXPECTED_TRIGGER_SHAPES: Final = {
    "e_trigger-none-review_up-10": _expected_trigger_shape(
        PHASE_NONE, _FIELD_REVIEW, 10
    ),
    "e_trigger-exam_card_play-card_play_aggressive_up-3": _expected_trigger_shape(
        PHASE_EXAM_CARD_PLAY, _FIELD_AGGRESSIVE, 3
    ),
    "e_trigger-exam_card_play-card_play_aggressive_up-6": _expected_trigger_shape(
        PHASE_EXAM_CARD_PLAY, _FIELD_AGGRESSIVE, 6
    ),
    "e_trigger-none-card_search_count_up-2-p_card_search-trouble-not_lost": (
        _expected_trigger_shape(
            PHASE_NONE,
            _FIELD_SEARCH_COUNT,
            2,
            field_search_ids=(_TROUBLE_SEARCH,),
        )
    ),
    "e_trigger-exam_card_play-review_up-3": _expected_trigger_shape(
        PHASE_EXAM_CARD_PLAY, _FIELD_REVIEW, 3
    ),
    TRIGGER_STAMINA_UP500: _expected_trigger_shape(
        PHASE_NONE, _FIELD_STAMINA, 500
    ),
    "e_trigger-none-review_up-15": _expected_trigger_shape(
        PHASE_NONE, _FIELD_REVIEW, 15
    ),
    TRIGGER_CARD_PLAY_STAMINA_UP500: _expected_trigger_shape(
        PHASE_EXAM_CARD_PLAY, _FIELD_STAMINA, 500
    ),
    TRIGGER_NOT_AGGRESSIVE_UP9: _expected_trigger_shape(
        PHASE_NONE,
        _FIELD_AGGRESSIVE,
        9,
        check_types=(_CHECK_NOT,),
    ),
    TRIGGER_AGGRESSIVE_UP9: _expected_trigger_shape(
        PHASE_NONE, _FIELD_AGGRESSIVE, 9
    ),
    TRIGGER_STAMINA_UP800: _expected_trigger_shape(
        PHASE_NONE, _FIELD_STAMINA, 800
    ),
    TRIGGER_STAMINA_UP1000: _expected_trigger_shape(
        PHASE_NONE, _FIELD_STAMINA, 1000
    ),
    "e_trigger-none-block_up-30": _expected_trigger_shape(
        PHASE_NONE, _FIELD_BLOCK, 30
    ),
    TRIGGER_AGGRESSIVE_UP6: _expected_trigger_shape(
        PHASE_NONE, _FIELD_AGGRESSIVE, 6
    ),
    TRIGGER_AGGRESSIVE_UP3: _expected_trigger_shape(
        PHASE_NONE, _FIELD_AGGRESSIVE, 3
    ),
    "e_trigger-exam_card_play-review_up-10": _expected_trigger_shape(
        PHASE_EXAM_CARD_PLAY, _FIELD_REVIEW, 10
    ),
    "e_trigger-exam_card_play-review_up-1": _expected_trigger_shape(
        PHASE_EXAM_CARD_PLAY, _FIELD_REVIEW, 1
    ),
}


class Plan2NativeEffectTriggerContractError(ValueError):
    """Current Master linkage is outside this bounded catalog."""


class EffectTriggerOrigin(str, Enum):
    NORMAL = "normal"
    FORCED = "forced"
    EXTRA = "extra"


class EffectTriggerBoundary(str, Enum):
    PRE_PAYMENT_DIRECT_EFFECT_BUILD = "pre-payment-direct-effect-build"
    POST_PAYMENT_DIRECT_EFFECT_BUILD = "post-payment-direct-effect-build"
    DIRECT_EFFECT_SEQUENCE_BUILD = "direct-effect-sequence-build"
    EXAM_CARD_PLAY_PRE_PAYMENT = "exam-card-play-pre-payment"


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectTriggerMasterRow:
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
class Plan2NativeEffectTriggerBlocker:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectTriggerHandoff:
    card_id: str
    upgrade: int
    slot_index: int
    ordered_effect_ids: tuple[str, ...]
    effect_id: str
    effect_type: str
    effect_value1: int
    effect_value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    trigger: Plan2NativeEffectTriggerMasterRow
    hide_icon: bool
    is_once_play_effect: bool
    executor_family: str
    supported_boundaries: tuple[EffectTriggerBoundary, ...]
    allowed_origins: tuple[EffectTriggerOrigin, ...]
    blockers: tuple[Plan2NativeEffectTriggerBlocker, ...]
    target_predicate_executable: bool
    companion_effect_executable_here: bool = False
    whole_card_executable: bool = False
    listener_kind: str = "card-owned-triggered-direct-effect-slot"
    count_scope: str = "none"
    lifetime_scope: str = "one-command-candidate-build"

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def occurrence_key(self) -> tuple[str, int, int]:
        return self.card_id, self.upgrade, self.slot_index

    @property
    def trigger_effect_ids(self) -> tuple[str, ...]:
        """The predicate IDs attached to this effect link, not child effects."""

        return (self.trigger.trigger_id,)

    @property
    def effects_before(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[: self.slot_index]

    @property
    def effects_after(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[self.slot_index + 1 :]


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectTriggerAccounting:
    occurrence_count: int
    unique_effect_count: int
    trigger_id_count: int
    unique_version_count: int
    executable_occurrence_count: int
    blocked_occurrence_count: int
    executable_version_count: int
    executable_effect_count: int
    companion_handoff_occurrence_count: int
    whole_card_executable_version_count: int
    executable_family_occurrences: tuple[tuple[str, int], ...]
    trigger_occurrences: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectTriggerCatalog:
    schema_version: int
    adapter_id: str
    trigger_rows: tuple[Plan2NativeEffectTriggerMasterRow, ...]
    handoffs: tuple[Plan2NativeEffectTriggerHandoff, ...]
    accounting: Plan2NativeEffectTriggerAccounting

    def occurrence(
        self, card_id: str, upgrade: int, slot_index: int
    ) -> Plan2NativeEffectTriggerHandoff:
        for handoff in self.handoffs:
            if handoff.occurrence_key == (card_id, upgrade, slot_index):
                return handoff
        raise KeyError((card_id, upgrade, slot_index))

    def for_version(
        self, card_id: str, upgrade: int
    ) -> tuple[Plan2NativeEffectTriggerHandoff, ...]:
        return tuple(row for row in self.handoffs if row.ref == (card_id, upgrade))


_ALL_ORIGINS: Final = tuple(EffectTriggerOrigin)
_NORMAL_ONLY: Final = (EffectTriggerOrigin.NORMAL,)


@dataclass(frozen=True, slots=True)
class _ExactOwner:
    family: str
    boundaries: tuple[EffectTriggerBoundary, ...]
    origins: tuple[EffectTriggerOrigin, ...]


_EXACT_OWNER_BY_CARD_SLOT_TRIGGER: Final = {
    (
        "p_card-00-men-1_007",
        1,
        TRIGGER_CARD_PLAY_STAMINA_UP500,
    ): _ExactOwner(
        "card_play_stamina_up500",
        (EffectTriggerBoundary.EXAM_CARD_PLAY_PRE_PAYMENT,),
        _NORMAL_ONLY,
    ),
    (
        "p_card-00-sup-3_161",
        1,
        TRIGGER_STAMINA_UP500,
    ): _ExactOwner(
        "none_stamina_up500_block_fix",
        (EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,),
        _NORMAL_ONLY,
    ),
    (
        "p_card-02-ido-3_165",
        1,
        TRIGGER_NOT_AGGRESSIVE_UP9,
    ): _ExactOwner(
        "aggressive_up9_effect_triggers",
        (EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,),
        _ALL_ORIGINS,
    ),
    (
        "p_card-02-ido-3_165",
        2,
        TRIGGER_AGGRESSIVE_UP9,
    ): _ExactOwner(
        "aggressive_up9_effect_triggers",
        (EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,),
        _ALL_ORIGINS,
    ),
    (
        "p_card-02-ido-3_167",
        2,
        TRIGGER_STAMINA_UP500,
    ): _ExactOwner(
        "stamina_threshold_effect_triggers",
        (EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,),
        _ALL_ORIGINS,
    ),
    (
        "p_card-02-ido-3_167",
        3,
        TRIGGER_STAMINA_UP800,
    ): _ExactOwner(
        "stamina_threshold_effect_triggers",
        (EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,),
        _ALL_ORIGINS,
    ),
    (
        "p_card-02-ido-3_167",
        4,
        TRIGGER_STAMINA_UP1000,
    ): _ExactOwner(
        "stamina_threshold_effect_triggers",
        (EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,),
        _ALL_ORIGINS,
    ),
    (
        "p_card-02-sup-3_178",
        3,
        TRIGGER_AGGRESSIVE_UP6,
    ): _ExactOwner(
        "aggressive_up6_direct_effect",
        (EffectTriggerBoundary.DIRECT_EFFECT_SEQUENCE_BUILD,),
        _ALL_ORIGINS,
    ),
    (
        "p_card-02-sup-3_180",
        1,
        TRIGGER_AGGRESSIVE_UP3,
    ): _ExactOwner(
        "aggressive_up3_effect_trigger",
        (
            EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,
            EffectTriggerBoundary.POST_PAYMENT_DIRECT_EFFECT_BUILD,
        ),
        _ALL_ORIGINS,
    ),
}


def _plain_i32(value: object, label: str) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise Plan2NativeEffectTriggerContractError(f"{label} must be a plain integer")
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2NativeEffectTriggerContractError(f"{label} is outside Int32")
    return value


def _json(value: object, label: str) -> object:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2NativeEffectTriggerContractError(f"{label} is invalid JSON") from error


def _json_array(value: object, label: str) -> tuple[object, ...]:
    parsed = _json(value, label)
    if not isinstance(parsed, list):
        raise Plan2NativeEffectTriggerContractError(f"{label} must be an array")
    return tuple(parsed)


def _json_object(value: object, label: str) -> Mapping[str, object]:
    parsed = _json(value, label)
    if not isinstance(parsed, dict):
        raise Plan2NativeEffectTriggerContractError(f"{label} must be an object")
    return parsed


def _strings(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_array(value, label)
    if any(type(item) is not str for item in parsed):
        raise Plan2NativeEffectTriggerContractError(f"{label} must contain strings")
    return parsed  # type: ignore[return-value]


def _ints(value: object, label: str) -> tuple[int, ...]:
    return tuple(_plain_i32(item, label) for item in _json_array(value, label))


def _assert_raw(
    raw: Mapping[str, object], expected: Mapping[str, object], label: str
) -> None:
    for key, value in expected.items():
        if raw.get(key) != value:
            raise Plan2NativeEffectTriggerContractError(
                f"{label}.{key} changed: expected {value!r}, got {raw.get(key)!r}"
            )


def _trigger_from_row(row: sqlite3.Row) -> Plan2NativeEffectTriggerMasterRow:
    trigger = Plan2NativeEffectTriggerMasterRow(
        str(row["id"]),
        _strings(row["phase_types_json"], "trigger.phase_types"),
        _ints(row["phase_values_json"], "trigger.phase_values"),
        _strings(row["field_status_check_types_json"], "trigger.check_types"),
        _strings(row["field_status_types_json"], "trigger.field_types"),
        _ints(row["field_status_values_json"], "trigger.field_values"),
        _strings(
            row["field_status_produce_card_search_ids_json"],
            "trigger.field_card_search_ids",
        ),
        str(row["produce_card_search_id"]),
        _plain_i32(row["upper_search_count"], "trigger.upper_search_count"),
        _plain_i32(row["lower_search_count"], "trigger.lower_search_count"),
        str(row["card_move_position_type"]),
        _strings(row["effect_types_json"], "trigger.effect_types"),
        str(row["lesson_type"]),
    )
    expected_shape = _EXPECTED_TRIGGER_SHAPES.get(trigger.trigger_id)
    actual_shape = (
        trigger.phase_types[0] if len(trigger.phase_types) == 1 else None,
        trigger.field_status_check_types,
        trigger.field_status_types[0]
        if len(trigger.field_status_types) == 1
        else None,
        trigger.field_status_values[0]
        if len(trigger.field_status_values) == 1
        else None,
        trigger.field_status_card_search_ids,
    )
    if (
        expected_shape is None
        or actual_shape != expected_shape
        or trigger.phase_values
        or trigger.produce_card_search_id
        or trigger.upper_search_count != 0
        or trigger.lower_search_count != 0
        or trigger.card_move_position_type != _MOVE_UNKNOWN
        or trigger.effect_types
        or trigger.lesson_type != _LESSON_UNKNOWN
    ):
        raise Plan2NativeEffectTriggerContractError(
            f"trigger shape changed: {trigger.trigger_id}"
        )
    raw = _json_object(row["raw_json"], f"trigger[{trigger.trigger_id}].raw")
    _assert_raw(
        raw,
        {
            "id": trigger.trigger_id,
            "phaseTypes": list(trigger.phase_types),
            "phaseValues": list(trigger.phase_values),
            "fieldStatusCheckTypes": list(trigger.field_status_check_types),
            "fieldStatusTypes": list(trigger.field_status_types),
            "fieldStatusValues": list(trigger.field_status_values),
            "fieldStatusProduceCardSearchIds": list(
                trigger.field_status_card_search_ids
            ),
            "produceCardSearchId": trigger.produce_card_search_id,
            "upperSearchCount": trigger.upper_search_count,
            "lowerSearchCount": trigger.lower_search_count,
            "cardMovePositionType": trigger.card_move_position_type,
            "effectTypes": list(trigger.effect_types),
            "lessonType": trigger.lesson_type,
        },
        f"trigger[{trigger.trigger_id}]",
    )
    return trigger


def _blocked(trigger_id: str) -> tuple[Plan2NativeEffectTriggerBlocker, ...]:
    return (
        Plan2NativeEffectTriggerBlocker(
            "no-exact-effect-trigger-evaluator",
            trigger_id,
        ),
    )


def load_plan2_native_catalog_effect_triggers(
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeEffectTriggerCatalog:
    """Load all current triggered effect links, retaining unsupported rows."""

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        cards = connection.execute(
            """
            SELECT * FROM card
            WHERE plan_type IN (?, ?)
            ORDER BY id, upgrade_count
            """,
            _PLAN_TYPES,
        ).fetchall()
        candidate_links: list[
            tuple[sqlite3.Row, int, Mapping[str, object], tuple[str, ...]]
        ] = []
        trigger_ids: set[str] = set()
        for card in cards:
            links = _json_array(
                card["play_effects_json"],
                f"card[{card['id']}+{card['upgrade_count']}].play_effects",
            )
            ordered_ids: list[str] = []
            for index, value in enumerate(links):
                if not isinstance(value, dict):
                    raise Plan2NativeEffectTriggerContractError(
                        f"card play link {card['id']}+{card['upgrade_count']}:{index} is invalid"
                    )
                effect_id = value.get("produceExamEffectId")
                if type(effect_id) is not str or not effect_id:
                    raise Plan2NativeEffectTriggerContractError("effect link id is invalid")
                ordered_ids.append(effect_id)
            ordered = tuple(ordered_ids)
            raw_card = _json_object(
                card["raw_json"], f"card[{card['id']}+{card['upgrade_count']}].raw"
            )
            if raw_card.get("playEffects") != list(links):
                raise Plan2NativeEffectTriggerContractError(
                    f"card playEffects raw mismatch: {card['id']}+{card['upgrade_count']}"
                )
            for index, value in enumerate(links):
                assert isinstance(value, dict)
                trigger_id = value.get("produceExamTriggerId")
                if trigger_id:
                    if type(trigger_id) is not str:
                        raise Plan2NativeEffectTriggerContractError(
                            "trigger link id is invalid"
                        )
                    trigger_ids.add(trigger_id)
                    candidate_links.append((card, index, value, ordered))

        placeholders = ",".join("?" for _ in trigger_ids)
        trigger_db_rows = connection.execute(
            f"SELECT * FROM produce_exam_trigger WHERE id IN ({placeholders}) ORDER BY id",
            tuple(sorted(trigger_ids)),
        ).fetchall()
        trigger_rows = tuple(_trigger_from_row(row) for row in trigger_db_rows)
        trigger_by_id = {row.trigger_id: row for row in trigger_rows}
        if set(trigger_by_id) != trigger_ids:
            raise Plan2NativeEffectTriggerContractError("one or more linked triggers are missing")

        handoffs: list[Plan2NativeEffectTriggerHandoff] = []
        for card, slot_index, link, ordered_ids in candidate_links:
            card_id = str(card["id"])
            upgrade = _plain_i32(card["upgrade_count"], "card.upgrade_count")
            effect_id = str(link["produceExamEffectId"])
            trigger_id = str(link["produceExamTriggerId"])
            if set(link) != {
                "produceExamTriggerId",
                "produceExamEffectId",
                "hideIcon",
                "isOncePlayEffect",
            }:
                raise Plan2NativeEffectTriggerContractError(
                    f"effect link shape changed: {card_id}+{upgrade}:{slot_index}"
                )
            if type(link["hideIcon"]) is not bool or type(
                link["isOncePlayEffect"]
            ) is not bool:
                raise Plan2NativeEffectTriggerContractError("effect link flags are invalid")
            effect = connection.execute(
                "SELECT * FROM effect WHERE id = ?", (effect_id,)
            ).fetchone()
            if effect is None:
                raise Plan2NativeEffectTriggerContractError(
                    f"linked effect is missing: {effect_id}"
                )
            value1 = _plain_i32(effect["value1"], "effect.value1")
            value2 = _plain_i32(effect["value2"], "effect.value2")
            count = _plain_i32(effect["effect_count"], "effect.effect_count")
            turn = _plain_i32(effect["effect_turn"], "effect.effect_turn")
            effect_raw = _json_object(effect["raw_json"], f"effect[{effect_id}].raw")
            _assert_raw(
                effect_raw,
                {
                    "id": effect_id,
                    "effectType": str(effect["effect_type"]),
                    "effectValue1": value1,
                    "effectValue2": value2,
                    "effectCount": count,
                    "effectTurn": turn,
                    "produceExamStatusEnchantId": str(effect["status_enchant_id"]),
                    "chainProduceExamEffectId": str(effect["chain_effect_id"]),
                },
                f"effect[{effect_id}]",
            )
            owner = _EXACT_OWNER_BY_CARD_SLOT_TRIGGER.get(
                (card_id, slot_index, trigger_id)
            )
            executable = owner is not None
            handoffs.append(
                Plan2NativeEffectTriggerHandoff(
                    card_id=card_id,
                    upgrade=upgrade,
                    slot_index=slot_index,
                    ordered_effect_ids=ordered_ids,
                    effect_id=effect_id,
                    effect_type=str(effect["effect_type"]),
                    effect_value1=value1,
                    effect_value2=value2,
                    effect_count=count,
                    effect_turn=turn,
                    status_enchant_id=str(effect["status_enchant_id"]),
                    chain_effect_id=str(effect["chain_effect_id"]),
                    trigger=trigger_by_id[trigger_id],
                    hide_icon=link["hideIcon"],
                    is_once_play_effect=link["isOncePlayEffect"],
                    executor_family="" if owner is None else owner.family,
                    supported_boundaries=() if owner is None else owner.boundaries,
                    allowed_origins=() if owner is None else owner.origins,
                    blockers=() if executable else _blocked(trigger_id),
                    target_predicate_executable=executable,
                )
            )

    trigger_counts = Counter(row.trigger.trigger_id for row in handoffs)
    actual_trigger_counts = tuple(
        (trigger_id, trigger_counts[trigger_id])
        for trigger_id, _ in EXPECTED_TRIGGER_OCCURRENCE_COUNTS
    )
    if actual_trigger_counts != EXPECTED_TRIGGER_OCCURRENCE_COUNTS:
        raise Plan2NativeEffectTriggerContractError(
            "trigger occurrence inventory changed"
        )
    refs = {row.ref for row in handoffs}
    card_ids = {row.card_id for row in handoffs}
    effect_ids = {row.effect_id for row in handoffs}
    executable_rows = tuple(row for row in handoffs if row.target_predicate_executable)
    executable_refs = {row.ref for row in executable_rows}
    executable_effect_ids = {row.effect_id for row in executable_rows}
    family_counts = Counter(row.executor_family for row in executable_rows)
    family_accounting = tuple(sorted(family_counts.items()))
    accounting = Plan2NativeEffectTriggerAccounting(
        occurrence_count=len(handoffs),
        unique_effect_count=len(effect_ids),
        trigger_id_count=len(trigger_by_id),
        unique_version_count=len(refs),
        executable_occurrence_count=len(executable_rows),
        blocked_occurrence_count=len(handoffs) - len(executable_rows),
        executable_version_count=len(executable_refs),
        executable_effect_count=len(executable_effect_ids),
        companion_handoff_occurrence_count=len(handoffs),
        whole_card_executable_version_count=0,
        executable_family_occurrences=family_accounting,
        trigger_occurrences=actual_trigger_counts,
    )
    expected_scalars = (
        EXPECTED_OCCURRENCE_COUNT,
        EXPECTED_UNIQUE_EFFECT_COUNT,
        EXPECTED_TRIGGER_ID_COUNT,
        EXPECTED_UNIQUE_VERSION_COUNT,
        EXPECTED_EXECUTABLE_OCCURRENCE_COUNT,
        EXPECTED_EXECUTABLE_VERSION_COUNT,
        EXPECTED_EXECUTABLE_EFFECT_COUNT,
        EXPECTED_CARD_ID_COUNT,
    )
    actual_scalars = (
        accounting.occurrence_count,
        accounting.unique_effect_count,
        accounting.trigger_id_count,
        accounting.unique_version_count,
        accounting.executable_occurrence_count,
        accounting.executable_version_count,
        accounting.executable_effect_count,
        len(card_ids),
    )
    if actual_scalars != expected_scalars:
        raise Plan2NativeEffectTriggerContractError(
            f"effect-trigger inventory changed: {actual_scalars!r}"
        )
    return Plan2NativeEffectTriggerCatalog(
        PLAN2_NATIVE_CATALOG_EFFECT_TRIGGERS_SCHEMA_VERSION,
        EFFECT_TRIGGER_ADAPTER_ID,
        trigger_rows,
        tuple(handoffs),
        accounting,
    )


compile_plan2_native_catalog_effect_triggers = (
    load_plan2_native_catalog_effect_triggers
)


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectTriggerSnapshot:
    phase: object
    boundary: object
    origin: object = EffectTriggerOrigin.NORMAL
    aggressive: object | None = None
    current_stamina: object | None = None
    max_stamina: object | None = None
    review: object | None = None
    block: object | None = None
    search_count: object | None = None
    global_card_play_count: object = 0
    turn_card_play_count: object = 0
    payment_committed_before_build: object | None = False
    direct_effects_executed_before_build: object = 0
    listener_spend_count: object = 0
    listener_remaining_turn: object | None = None


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectTriggerEvaluation:
    occurrence_key: tuple[str, int, int] | None
    trigger_id: str
    supported: bool
    fires: bool | None
    executor_family: str
    phase: object
    boundary: object
    origin: object
    payment_committed_before_build: object | None
    direct_effects_executed_before_build: object
    before: Plan2NativeEffectTriggerSnapshot
    after: Plan2NativeEffectTriggerSnapshot
    target_predicate_executable: bool
    companion_effect_executed: bool
    whole_card_executable: bool
    reasons: tuple[str, ...]

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    @property
    def state_unchanged(self) -> bool:
        return self.before is self.after


def _runtime_i32(
    value: object, label: str, reasons: list[str], *, nonnegative: bool = False
) -> int | None:
    if isinstance(value, bool) or type(value) is not int:
        reasons.append(f"{label}-is-not-plain-int32")
        return None
    if not INT32_MIN <= value <= INT32_MAX:
        reasons.append(f"{label}-is-outside-int32")
        return None
    if nonnegative and value < 0:
        reasons.append(f"{label}-is-negative")
        return None
    return value


def _origin(value: object) -> EffectTriggerOrigin | None:
    try:
        return EffectTriggerOrigin(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _boundary(value: object) -> EffectTriggerBoundary | None:
    try:
        return EffectTriggerBoundary(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _unsupported(
    handoff: Plan2NativeEffectTriggerHandoff | None,
    snapshot: Plan2NativeEffectTriggerSnapshot,
    reasons: tuple[str, ...] | list[str],
) -> Plan2NativeEffectTriggerEvaluation:
    return Plan2NativeEffectTriggerEvaluation(
        None if handoff is None else handoff.occurrence_key,
        "unknown" if handoff is None else handoff.trigger.trigger_id,
        False,
        None,
        "" if handoff is None else handoff.executor_family,
        snapshot.phase,
        snapshot.boundary,
        snapshot.origin,
        snapshot.payment_committed_before_build,
        snapshot.direct_effects_executed_before_build,
        snapshot,
        snapshot,
        False,
        False,
        False,
        tuple(dict.fromkeys(reasons)),
    )


def _aggressive_row(
    trigger: Plan2NativeEffectTriggerMasterRow,
) -> AggressiveTriggerRow:
    return AggressiveTriggerRow(
        id=trigger.trigger_id,
        phase_types=trigger.phase_types,
        phase_values=trigger.phase_values,
        field_status_check_types=trigger.field_status_check_types,
        field_status_types=trigger.field_status_types,
        field_status_values=trigger.field_status_values,
        field_status_card_search_ids=trigger.field_status_card_search_ids,
        produce_card_search_id=trigger.produce_card_search_id,
        upper_search_count=trigger.upper_search_count,
        lower_search_count=trigger.lower_search_count,
        card_move_position_type=trigger.card_move_position_type,
        effect_types=trigger.effect_types,
        lesson_type=trigger.lesson_type,
    )


def evaluate_plan2_native_effect_trigger(
    source: Plan2NativeEffectTriggerHandoff | object,
    snapshot: Plan2NativeEffectTriggerSnapshot,
) -> Plan2NativeEffectTriggerEvaluation:
    """Evaluate one bounded occurrence and never run its companion effect."""

    if not isinstance(snapshot, Plan2NativeEffectTriggerSnapshot):
        raise TypeError("snapshot must be Plan2NativeEffectTriggerSnapshot")
    if not isinstance(source, Plan2NativeEffectTriggerHandoff):
        return _unsupported(None, snapshot, ("unknown-occurrence-failed-closed",))
    handoff = source
    if not handoff.target_predicate_executable:
        return _unsupported(
            handoff,
            snapshot,
            tuple(f"{item.code}:{item.detail}" for item in handoff.blockers),
        )

    reasons: list[str] = []
    origin = _origin(snapshot.origin)
    boundary = _boundary(snapshot.boundary)
    expected_phase = handoff.trigger.phase_types[0]
    if snapshot.phase != expected_phase:
        reasons.append("effect-trigger-phase-mismatch")
    if boundary is None or boundary not in handoff.supported_boundaries:
        reasons.append("effect-trigger-boundary-unproven")
    if origin is None:
        reasons.append("card-origin-unknown")
    elif origin not in handoff.allowed_origins:
        reasons.append("card-origin-unproven-for-occurrence")
    global_count = _runtime_i32(
        snapshot.global_card_play_count,
        "global-card-play-count",
        reasons,
        nonnegative=True,
    )
    turn_count = _runtime_i32(
        snapshot.turn_card_play_count,
        "turn-card-play-count",
        reasons,
        nonnegative=True,
    )
    direct_count = _runtime_i32(
        snapshot.direct_effects_executed_before_build,
        "direct-effects-executed-before-build",
        reasons,
        nonnegative=True,
    )
    listener_count = _runtime_i32(
        snapshot.listener_spend_count,
        "listener-spend-count",
        reasons,
        nonnegative=True,
    )
    if direct_count is not None and direct_count != 0:
        reasons.append("direct-effects-already-executed")
    if listener_count is not None and listener_count != 0:
        reasons.append("direct-effect-trigger-is-not-listener-counted")
    if snapshot.listener_remaining_turn is not None:
        reasons.append("direct-effect-trigger-has-no-listener-lifetime")
    payment = snapshot.payment_committed_before_build
    if payment is not None and type(payment) is not bool:
        reasons.append("payment-state-is-not-boolean-or-none")
    if boundary is EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD and payment is not False:
        reasons.append("pre-payment-boundary-payment-state-mismatch")
    if boundary is EffectTriggerBoundary.POST_PAYMENT_DIRECT_EFFECT_BUILD and payment is not True:
        reasons.append("post-payment-boundary-payment-state-mismatch")
    if boundary is EffectTriggerBoundary.EXAM_CARD_PLAY_PRE_PAYMENT and payment is not False:
        reasons.append("pre-payment-boundary-payment-state-mismatch")
    if reasons:
        return _unsupported(handoff, snapshot, reasons)
    assert origin is not None and boundary is not None
    assert global_count is not None and turn_count is not None

    exact: object
    if handoff.executor_family == "aggressive_up3_effect_trigger":
        aggressive = _runtime_i32(snapshot.aggressive, "aggressive", reasons)
        if aggressive is None:
            return _unsupported(handoff, snapshot, reasons)
        native_boundary = {
            EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD: (
                AggressiveUp3Boundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD
            ),
            EffectTriggerBoundary.POST_PAYMENT_DIRECT_EFFECT_BUILD: (
                AggressiveUp3Boundary.POST_PAYMENT_DIRECT_EFFECT_BUILD
            ),
        }[boundary]
        exact = evaluate_effect_trigger_aggressive_up3(
            AggressiveUp3Input(
                aggressive,
                global_card_play_count=global_count,
                turn_card_play_count=turn_count,
                boundary=native_boundary,
                card_origin=origin.value,
                payment_committed_before_build=payment,
                direct_effects_executed_before_build=0,
            ),
            AGGRESSIVE_UP3_TRIGGER,
        )
    elif handoff.executor_family == "aggressive_up6_direct_effect":
        aggressive = _runtime_i32(snapshot.aggressive, "aggressive", reasons)
        if aggressive is None:
            return _unsupported(handoff, snapshot, reasons)
        exact = evaluate_direct_effect_aggressive_up6_trigger(
            AggressiveUp6Input(
                aggressive,
                boundary=AggressiveUp6Boundary.DIRECT_EFFECT_SEQUENCE_BUILD,
                card_origin=origin.value,
                global_card_play_count=global_count,
                turn_card_play_count=turn_count,
            ),
            AGGRESSIVE_UP6_TRIGGER,
        )
    elif handoff.executor_family == "aggressive_up9_effect_triggers":
        aggressive = _runtime_i32(snapshot.aggressive, "aggressive", reasons)
        if aggressive is None:
            return _unsupported(handoff, snapshot, reasons)
        exact = evaluate_aggressive_effect_trigger(
            AggressiveUp9Input(
                aggressive,
                global_card_play_count=global_count,
                turn_card_play_count=turn_count,
                boundary=AggressiveUp9Boundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,
                card_origin=origin.value,
            ),
            _aggressive_row(handoff.trigger),
        )
    elif handoff.executor_family == "none_stamina_up500_block_fix":
        current = _runtime_i32(snapshot.current_stamina, "current-stamina", reasons)
        maximum = _runtime_i32(snapshot.max_stamina, "max-stamina", reasons)
        if current is None or maximum is None:
            return _unsupported(handoff, snapshot, reasons)
        exact = evaluate_plan2_none_stamina_up_multiple(
            handoff.trigger.trigger_id,
            current_stamina=current,
            max_stamina=maximum,
            boundary=StaminaUp500Boundary.PRE_PAYMENT_CARD_PLAY,
            execution_kind=StaminaUp500ExecutionKind.ORDINARY,
        )
    elif handoff.executor_family == "stamina_threshold_effect_triggers":
        current = _runtime_i32(snapshot.current_stamina, "current-stamina", reasons)
        maximum = _runtime_i32(snapshot.max_stamina, "max-stamina", reasons)
        if current is None or maximum is None:
            return _unsupported(handoff, snapshot, reasons)
        exact = evaluate_stamina_threshold_effect_trigger(
            handoff.trigger.trigger_id,
            current_stamina=current,
            max_stamina=maximum,
            boundary=StaminaThresholdBoundary.PRE_PAYMENT_CARD_PLAY,
            card_origin=StaminaThresholdOrigin(origin.value),
        )
    elif handoff.executor_family == "card_play_stamina_up500":
        current = _runtime_i32(snapshot.current_stamina, "current-stamina", reasons)
        maximum = _runtime_i32(snapshot.max_stamina, "max-stamina", reasons)
        if current is None or maximum is None:
            return _unsupported(handoff, snapshot, reasons)
        exact = evaluate_plan2_card_play_stamina_trigger(
            handoff.trigger.trigger_id,
            current_stamina=current,
            max_stamina=maximum,
            capture_boundary=CardPlayStaminaBoundary.PRE_PAYMENT,
        )
    else:
        return _unsupported(
            handoff, snapshot, ("unknown-executor-family-failed-closed",)
        )

    fires = getattr(exact, "fires", None)
    exact_reasons = tuple(getattr(exact, "reasons", ()))
    if type(fires) is not bool or exact_reasons:
        return _unsupported(
            handoff,
            snapshot,
            exact_reasons or ("exact-effect-trigger-evaluator-failed-closed",),
        )
    return Plan2NativeEffectTriggerEvaluation(
        handoff.occurrence_key,
        handoff.trigger.trigger_id,
        True,
        fires,
        handoff.executor_family,
        snapshot.phase,
        snapshot.boundary,
        snapshot.origin,
        snapshot.payment_committed_before_build,
        snapshot.direct_effects_executed_before_build,
        snapshot,
        snapshot,
        True,
        False,
        False,
        (),
    )


__all__ = [
    "EFFECT_TRIGGER_ADAPTER_ID",
    "EXPECTED_CARD_ID_COUNT",
    "EXPECTED_EXECUTABLE_EFFECT_COUNT",
    "EXPECTED_EXECUTABLE_OCCURRENCE_COUNT",
    "EXPECTED_EXECUTABLE_VERSION_COUNT",
    "EXPECTED_OCCURRENCE_COUNT",
    "EXPECTED_TRIGGER_ID_COUNT",
    "EXPECTED_TRIGGER_OCCURRENCE_COUNTS",
    "EXPECTED_UNIQUE_EFFECT_COUNT",
    "EXPECTED_UNIQUE_VERSION_COUNT",
    "EffectTriggerBoundary",
    "EffectTriggerOrigin",
    "PHASE_EXAM_CARD_PLAY",
    "PHASE_NONE",
    "PLAN2_NATIVE_CATALOG_EFFECT_TRIGGERS_SCHEMA_VERSION",
    "Plan2NativeEffectTriggerAccounting",
    "Plan2NativeEffectTriggerBlocker",
    "Plan2NativeEffectTriggerCatalog",
    "Plan2NativeEffectTriggerContractError",
    "Plan2NativeEffectTriggerEvaluation",
    "Plan2NativeEffectTriggerHandoff",
    "Plan2NativeEffectTriggerMasterRow",
    "Plan2NativeEffectTriggerSnapshot",
    "TARGET_TRIGGER_IDS",
    "TRIGGER_AGGRESSIVE_UP3",
    "TRIGGER_AGGRESSIVE_UP6",
    "TRIGGER_AGGRESSIVE_UP9",
    "TRIGGER_CARD_PLAY_STAMINA_UP500",
    "TRIGGER_NOT_AGGRESSIVE_UP9",
    "TRIGGER_STAMINA_UP500",
    "TRIGGER_STAMINA_UP800",
    "TRIGGER_STAMINA_UP1000",
    "compile_plan2_native_catalog_effect_triggers",
    "evaluate_plan2_native_effect_trigger",
    "load_plan2_native_catalog_effect_triggers",
]
