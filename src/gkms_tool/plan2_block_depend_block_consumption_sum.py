"""Exact standalone Plan2 listener child for ``BlockConsumptionSum``.

The scope is deliberately limited to ``p_card-02-ido-3_176`` upgrades 0..3.
The card installs one permanent ``TriggerEffectStatusEffect`` with three
lifetime activations.  Its ``ExamStartTurn`` trigger also requires ``NoBlock``;
that predicate is an external co-block and is never guessed here.

Android v3.2.3 proves two distinct transaction times:

* command construction snapshots the ordered child list and spends the
  listener count; and
* child execution later reads the then-current
  ``ExamParameterModel.BlockConsumptionSumCount``.

Keeping those stages separate prevents a preview from reading the accumulator
too early and prevents an exhausted listener from losing its already-captured
third child command.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    AddBlockSettings,
    AddBlockStatus,
    calculate_add_block,
    ceil_f32_to_i32,
    f32,
    permille_to_f32,
)
from .plan2_state import Plan2EndTurnEffect


TARGET_CARD_ID = "p_card-02-ido-3_176"
TARGET_UPGRADES = (0, 1, 2, 3)
TARGET_CARD_NAME = "\u3042\u305f\u3057\u304c\u3044\u308b\u3088"
TARGET_WRAPPER_EFFECT_ID = (
    "e_effect-exam_status_enchant-03-inf-"
    "enchant-p_card-02-ido-3_176-enc01"
)
TARGET_STATUS_ENCHANT_ID = "enchant-p_card-02-ido-3_176-enc01"
TARGET_TRIGGER_ID = "e_trigger-exam_start_turn-no_block"
TARGET_CHILD_EFFECT_ID = (
    "e_effect-exam_block_depend_block_consumption_sum-0800-01"
)

STATUS_ENCHANT_EFFECT_TYPE = "ProduceExamEffectType_ExamStatusEnchant"
BLOCK_DEPEND_CONSUMPTION_SUM_EFFECT_TYPE = (
    "ProduceExamEffectType_ExamBlockDependBlockConsumptionSum"
)
START_TURN_PHASE_TYPE = "ProduceExamPhaseType_ExamStartTurn"
NO_BLOCK_FIELD_STATUS_TYPE = "ProduceExamFieldStatusType_NoBlock"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
MOVE_LOST = "ProduceCardMovePositionType_Lost"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
PLAN2 = "ProducePlanType_Plan2"
MENTAL_SKILL = "ProduceCardCategory_MentalSkill"
UNKNOWN_COST = "ExamCostType_Unknown"

TARGET_VALUE_PERMILLE = 800
TARGET_EFFECT_COUNT = 1
TARGET_EFFECT_TURN = 0
TARGET_LISTENER_LIMIT = 3
PERMANENT_TURN = -1
UNLIMITED_COUNT = -1
BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE = 3

FORMAL_AFFECTED_VERSION_REFS = tuple(
    f"{TARGET_CARD_ID}#{upgrade}" for upgrade in TARGET_UPGRADES
)
FORMAL_DIRECT_VERSION_REFS: tuple[str, ...] = ()
FORMAL_CO_BLOCKED_VERSION_REFS = FORMAL_AFFECTED_VERSION_REFS

ANDROID_BLOCK_DEPEND_CONSUMPTION_SUM_EVIDENCE = {
    "executor": {
        "type": "Campus.InGame.Exam.BlockDependBlockConsumptionSumEffectExecutor",
        "ctor_va": "0x7E71E9C",
        "execute_va": "0x7E71F54",
        "fact": "ctor copies only EffectValue1; Execute reads the global sum",
    },
    "formula": {
        "sum_getter_va": "0x7EB8AD8",
        "float_from_permil_va": "0x701E940",
        "calculate_add_block_va": "0x7E5E6CC",
        "add_block_fix_va": "0x7E5EA94",
        "expression": (
            "ceil_f32(FloatFromPermil(value1) * "
            "f32(BlockConsumptionSumCount))"
        ),
    },
    "accumulator": {
        "field_offset": "0x36C",
        "set_block_va": "0x7EBB524",
        "add_sum_va": "0x7EBB5C8",
        "fact": (
            "only SetBlock(..., isConsumption=true) adds "
            "max(i32(oldBlock-newBlock), 0), using wrapping Int32 addition"
        ),
    },
    "listener_order": {
        "get_commands_va": "0x7ECF230",
        "get_insert_commands_va": "0x7ED0690",
        "spend_count_va": "0x7EAE70C",
        "remove_count_limited_va": "0x7EA52B0",
        "fact": (
            "open marker, ordered child commands, SpendCount during queue "
            "construction, close marker; captured children survive removal"
        ),
    },
    "start_turn": {
        "exam_loop_move_next_va": "0x7EDCE78",
        "isil_lines": [2617, 2619, 2623, 2626, 2652, 2656],
        "fact": (
            "phase-4 listeners are captured before the ExamStartTurn phase "
            "marker is queued and before the command stack is awaited"
        ),
    },
}

PC_BLOCK_DEPEND_CONSUMPTION_SUM_METADATA = {
    "BlockDependBlockConsumptionSumEffectExecutor": {
        "type_index": 3514,
        "ctor": {"method_index": 17780, "token": 100681077},
        "ExecuteEffect": {"method_index": 17781, "token": 100681078},
    },
    "StatusEnchantEffectExecutor.ExecuteEffect": {
        "method_index": 18108,
        "token": 100681405,
    },
    "TriggerEffectStatusEffect": {
        "ctor": {"method_index": 19067, "token": 100682364},
        "SpendCount": {"method_index": 19075, "token": 100682372},
    },
    "ExamParameterModel": {
        "get_BlockConsumptionSumCount": {
            "method_index": 19517,
            "token": 100682814,
        },
        "set_BlockConsumptionSumCount": {
            "method_index": 19518,
            "token": 100682815,
        },
        "SetBlock": {"method_index": 19620, "token": 100682917},
        "AddBlockConsumptionSumCount": {
            "method_index": 19640,
            "token": 100682937,
        },
    },
    "ExamSequence": {
        "GetTriggerEffectListCommand": {
            "method_index": 19963,
            "token": 100683260,
        },
        "GetInsertTriggerEffectListCommand": {
            "method_index": 19966,
            "token": 100683263,
        },
    },
}


class Plan2BlockDependConsumptionSumContractError(ValueError):
    """The supplied Master/runtime record is outside the proven slice."""


def _plain_i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2BlockDependConsumptionSumContractError(
            f"{label} must be a plain Int32"
        )
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2BlockDependConsumptionSumContractError(
            f"{label} is outside Int32"
        )
    return value


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _json_object(raw_json: object, row_id: str) -> dict[str, object]:
    try:
        value = json.loads(str(raw_json))
    except json.JSONDecodeError as error:
        raise Plan2BlockDependConsumptionSumContractError(
            f"{row_id}: invalid raw_json"
        ) from error
    if not isinstance(value, dict):
        raise Plan2BlockDependConsumptionSumContractError(
            f"{row_id}: raw_json must be an object"
        )
    return value


def _json_array(raw_json: object, label: str) -> list[object]:
    try:
        value = json.loads(str(raw_json))
    except json.JSONDecodeError as error:
        raise Plan2BlockDependConsumptionSumContractError(
            f"{label}: invalid JSON"
        ) from error
    if not isinstance(value, list):
        raise Plan2BlockDependConsumptionSumContractError(
            f"{label} must be an array"
        )
    return value


def _require_equal(
    raw: dict[str, object], key: str, expected: object, row_id: str
) -> None:
    if raw.get(key) != expected:
        raise Plan2BlockDependConsumptionSumContractError(
            f"{row_id}: {key} must be {expected!r}, got {raw.get(key)!r}"
        )


def _require_exact_keys(
    raw: dict[str, object], expected: set[str], row_id: str
) -> None:
    actual = set(raw)
    if actual != expected:
        raise Plan2BlockDependConsumptionSumContractError(
            f"{row_id}: raw key set changed; missing={sorted(expected-actual)!r}, "
            f"extra={sorted(actual-expected)!r}"
        )


_DESCRIPTION_KEYS = {
    "produceDescriptionType",
    "examDescriptionType",
    "examEffectType",
    "produceCardGrowEffectType",
    "produceCardCategory",
    "produceCardMovePositionType",
    "produceStepType",
    "produceStepBusinessType",
    "text",
    "targetId",
    "targetLevel",
    "effectValue1",
    "effectValue2",
    "effectCount",
    "turn",
    "costValue",
    "produceDescriptionSwapId",
    "originProduceExamTriggerId",
    "originProduceExamEffectId",
    "originProduceCardStatusEnchantId",
    "isCost",
    "isOnlyOutGame",
    "changeColor",
}


def _validate_descriptions(
    value: object, expected_count: int, label: str
) -> None:
    if not isinstance(value, list) or len(value) != expected_count:
        raise Plan2BlockDependConsumptionSumContractError(
            f"{label} must contain exactly {expected_count} entries"
        )
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != _DESCRIPTION_KEYS:
            raise Plan2BlockDependConsumptionSumContractError(
                f"{label}[{index}] description shape changed"
            )


_EFFECT_RAW_KEYS = {
    "id",
    "effectType",
    "effectValue1",
    "effectValue2",
    "effectCount",
    "effectTurn",
    "targetProduceCardId",
    "targetUpgradeCount",
    "targetExamEffectType",
    "produceCardSearchId",
    "movePositionType",
    "pickRangeType",
    "pickCountReferenceProduceCardSearchId",
    "pickCountType",
    "pickCountMin",
    "pickCountMax",
    "produceCardSearchId2",
    "pickRangeType2",
    "pickCountReferenceProduceCardSearchId2",
    "pickCountType2",
    "pickCountMin2",
    "pickCountMax2",
    "chainProduceExamEffectId",
    "chainProduceExamEffectIds",
    "produceExamStatusEnchantId",
    "produceCardStatusEnchantId",
    "produceCardGrowEffectIds",
    "effectGroupIds",
    "produceDescriptions",
    "customizeProduceDescriptions",
}


def _validate_effect_raw(
    row: sqlite3.Row,
    *,
    effect_type: str,
    value1: int,
    value2: int,
    count: int,
    turn: int,
    status_id: str,
    effect_groups: list[str],
    description_counts: tuple[int, int],
) -> dict[str, object]:
    row_id = str(row["id"])
    expected_columns = (
        ("effect_type", effect_type),
        ("value1", value1),
        ("value2", value2),
        ("effect_count", count),
        ("effect_turn", turn),
        ("status_enchant_id", status_id),
        ("chain_effect_id", ""),
    )
    for key, expected in expected_columns:
        if row[key] != expected:
            raise Plan2BlockDependConsumptionSumContractError(
                f"{row_id}: column {key} must be {expected!r}"
            )
    raw = _json_object(row["raw_json"], row_id)
    _require_exact_keys(raw, _EFFECT_RAW_KEYS, row_id)
    expected_raw = (
        ("id", row_id),
        ("effectType", effect_type),
        ("effectValue1", value1),
        ("effectValue2", value2),
        ("effectCount", count),
        ("effectTurn", turn),
        ("targetProduceCardId", ""),
        ("targetUpgradeCount", 0),
        ("targetExamEffectType", "ProduceExamEffectType_Unknown"),
        ("produceCardSearchId", ""),
        ("movePositionType", MOVE_UNKNOWN),
        ("pickRangeType", "ProducePickRangeType_Unknown"),
        ("pickCountReferenceProduceCardSearchId", ""),
        ("pickCountType", "ProducePickCountType_Unknown"),
        ("pickCountMin", 0),
        ("pickCountMax", 0),
        ("produceCardSearchId2", ""),
        ("pickRangeType2", "ProducePickRangeType_Unknown"),
        ("pickCountReferenceProduceCardSearchId2", ""),
        ("pickCountType2", "ProducePickCountType_Unknown"),
        ("pickCountMin2", 0),
        ("pickCountMax2", 0),
        ("chainProduceExamEffectId", ""),
        ("chainProduceExamEffectIds", []),
        ("produceExamStatusEnchantId", status_id),
        ("produceCardStatusEnchantId", ""),
        ("produceCardGrowEffectIds", []),
        ("effectGroupIds", effect_groups),
    )
    for key, expected in expected_raw:
        _require_equal(raw, key, expected, row_id)
    _validate_descriptions(
        raw["produceDescriptions"], description_counts[0],
        f"{row_id}.produceDescriptions",
    )
    _validate_descriptions(
        raw["customizeProduceDescriptions"], description_counts[1],
        f"{row_id}.customizeProduceDescriptions",
    )
    return raw


_CARD_RAW_KEYS = {
    "id",
    "upgradeCount",
    "name",
    "assetId",
    "isCharacterAsset",
    "voiceAssetId",
    "rarity",
    "planType",
    "category",
    "stamina",
    "forceStamina",
    "costType",
    "costValue",
    "playProduceExamTriggerId",
    "playEffects",
    "playMovePositionType",
    "moveEffectTriggerType",
    "moveProduceExamEffectIds",
    "isEndTurnLost",
    "isInitial",
    "isRestrict",
    "produceCardStatusEnchantId",
    "searchTag",
    "libraryHidden",
    "noDeckDuplication",
    "isReward",
    "produceDescriptions",
    "unlockProducerLevel",
    "rentalUnlockProducerLevel",
    "evaluation",
    "originIdolCardId",
    "originSupportCardId",
    "isInitialDeckProduceCard",
    "effectGroupIds",
    "produceCardCustomizeIds",
    "maxCustomizeCount",
    "isConversion",
    "moveProduceExamTriggerIds",
    "originCharacterId",
    "originPrimaStellaIdolCardId",
    "viewStartTime",
    "isLimited",
    "order",
}

_CARD_STAMINA_BY_UPGRADE = {0: 6, 1: 5, 2: 3, 3: 2}
_CARD_EVALUATION_BY_UPGRADE = {0: 1, 1: 5, 2: 0, 3: 0}
_CARD_AGGRESSIVE_EFFECT_BY_UPGRADE = {
    0: "e_effect-exam_card_play_aggressive-0001",
    1: "e_effect-exam_card_play_aggressive-0003",
    2: "e_effect-exam_card_play_aggressive-0003",
    3: "e_effect-exam_card_play_aggressive-0003",
}
_CARD_EFFECT_GROUPS = [
    "effect_group-visible-exam_block-000",
    "effect_group-visible-exam_status_enchant-000",
    "effect_group-visible-exam_card_play_aggressive-000",
]


def _expected_play_effects(upgrade: int) -> list[dict[str, object]]:
    return [
        {
            "produceExamTriggerId": "",
            "produceExamEffectId": "e_effect-exam_block-0002",
            "hideIcon": False,
            "isOncePlayEffect": False,
        },
        {
            "produceExamTriggerId": "",
            "produceExamEffectId": _CARD_AGGRESSIVE_EFFECT_BY_UPGRADE[upgrade],
            "hideIcon": False,
            "isOncePlayEffect": False,
        },
        {
            "produceExamTriggerId": "",
            "produceExamEffectId": TARGET_WRAPPER_EFFECT_ID,
            "hideIcon": False,
            "isOncePlayEffect": False,
        },
    ]


@dataclass(frozen=True, slots=True)
class Plan2BlockDependCardVersion:
    upgrade: int
    name: str
    stamina: int
    play_effect_ids: tuple[str, ...]
    installer_index: int = 2


@dataclass(frozen=True, slots=True)
class Plan2BlockDependConsumptionSumProgram:
    """Fully validated installer, listener, trigger, and child program."""

    cards: tuple[Plan2BlockDependCardVersion, ...]
    wrapper_effect_id: str
    status_enchant_id: str
    trigger_id: str
    phase_type: str
    field_status_type: str
    effects: tuple[Plan2EndTurnEffect, ...]
    wrapper_effect_groups: tuple[str, ...]
    listener_turn: int
    listener_limit_count: int
    listener_limit_count_in_turn: int

    def __post_init__(self) -> None:
        if len(self.cards) != 4 or any(
            not isinstance(card, Plan2BlockDependCardVersion)
            for card in self.cards
        ):
            raise Plan2BlockDependConsumptionSumContractError(
                "program cards must be exact card-version records"
            )
        if tuple(card.upgrade for card in self.cards) != TARGET_UPGRADES:
            raise Plan2BlockDependConsumptionSumContractError(
                "program must contain exact upgrades 0..3"
            )
        for card, upgrade in zip(self.cards, TARGET_UPGRADES, strict=True):
            expected_effect_ids = tuple(
                str(item["produceExamEffectId"])
                for item in _expected_play_effects(upgrade)
            )
            if (
                not isinstance(card, Plan2BlockDependCardVersion)
                or type(card.upgrade) is not int
                or card.name != TARGET_CARD_NAME + "+" * upgrade
                or card.stamina != _CARD_STAMINA_BY_UPGRADE[upgrade]
                or card.play_effect_ids != expected_effect_ids
                or card.installer_index != 2
            ):
                raise Plan2BlockDependConsumptionSumContractError(
                    f"card upgrade {upgrade} installer shape changed"
                )
        if self.wrapper_effect_id != TARGET_WRAPPER_EFFECT_ID:
            raise Plan2BlockDependConsumptionSumContractError(
                "wrapper effect id is outside the target"
            )
        if self.status_enchant_id != TARGET_STATUS_ENCHANT_ID:
            raise Plan2BlockDependConsumptionSumContractError(
                "status enchant id is outside the target"
            )
        if self.trigger_id != TARGET_TRIGGER_ID:
            raise Plan2BlockDependConsumptionSumContractError(
                "trigger id is outside the target"
            )
        if self.phase_type != START_TURN_PHASE_TYPE:
            raise Plan2BlockDependConsumptionSumContractError(
                "phase must be ExamStartTurn"
            )
        if self.field_status_type != NO_BLOCK_FIELD_STATUS_TYPE:
            raise Plan2BlockDependConsumptionSumContractError(
                "field status must be NoBlock"
            )
        if self.wrapper_effect_groups != (
            "effect_group-visible-exam_block-000",
            "effect_group-visible-exam_status_enchant-000",
        ):
            raise Plan2BlockDependConsumptionSumContractError(
                "wrapper effect groups changed"
            )
        if len(self.effects) != 1 or not isinstance(
            self.effects[0], Plan2EndTurnEffect
        ):
            raise Plan2BlockDependConsumptionSumContractError(
                "program must contain exactly one child"
            )
        child = self.effects[0]
        if (
            child.effect_id != TARGET_CHILD_EFFECT_ID
            or child.effect_type != BLOCK_DEPEND_CONSUMPTION_SUM_EFFECT_TYPE
            or child.value1 != TARGET_VALUE_PERMILLE
            or child.value2 != 0
            or child.count != TARGET_EFFECT_COUNT
            or child.turn != TARGET_EFFECT_TURN
            or child.effect_group_ids
            != ("effect_group-visible-exam_block-000",)
        ):
            raise Plan2BlockDependConsumptionSumContractError(
                "child effect shape is outside the exact target"
            )
        if self.listener_turn != PERMANENT_TURN:
            raise Plan2BlockDependConsumptionSumContractError(
                "listener must be permanent"
            )
        if self.listener_limit_count != TARGET_LISTENER_LIMIT:
            raise Plan2BlockDependConsumptionSumContractError(
                "listener lifetime count must be 3"
            )
        if self.listener_limit_count_in_turn != UNLIMITED_COUNT:
            raise Plan2BlockDependConsumptionSumContractError(
                "listener must have no per-turn limit"
            )


def _validate_card(row: sqlite3.Row, upgrade: int) -> Plan2BlockDependCardVersion:
    row_id = f"{TARGET_CARD_ID}#{upgrade}"
    expected_name = TARGET_CARD_NAME + "+" * upgrade
    expected_effects = _expected_play_effects(upgrade)
    expected_columns = (
        ("id", TARGET_CARD_ID),
        ("upgrade_count", upgrade),
        ("name", expected_name),
        ("plan_type", PLAN2),
        ("category", MENTAL_SKILL),
        ("stamina", _CARD_STAMINA_BY_UPGRADE[upgrade]),
        ("cost_type", UNKNOWN_COST),
        ("cost_value", 0),
        ("play_trigger_id", ""),
        ("move_position_type", MOVE_LOST),
    )
    for key, expected in expected_columns:
        if row[key] != expected:
            raise Plan2BlockDependConsumptionSumContractError(
                f"{row_id}: column {key} must be {expected!r}"
            )
    if _json_array(row["play_effects_json"], f"{row_id}.play_effects_json") != expected_effects:
        raise Plan2BlockDependConsumptionSumContractError(
            f"{row_id}: ordered play effects changed"
        )
    raw = _json_object(row["raw_json"], row_id)
    _require_exact_keys(raw, _CARD_RAW_KEYS, row_id)
    exact = (
        ("id", TARGET_CARD_ID),
        ("upgradeCount", upgrade),
        ("name", expected_name),
        ("assetId", "img_general_skillcard_ido-3_176"),
        ("isCharacterAsset", False),
        ("voiceAssetId", ""),
        ("rarity", "ProduceCardRarity_Ssr"),
        ("planType", PLAN2),
        ("category", MENTAL_SKILL),
        ("stamina", _CARD_STAMINA_BY_UPGRADE[upgrade]),
        ("forceStamina", 0),
        ("costType", UNKNOWN_COST),
        ("costValue", 0),
        ("playProduceExamTriggerId", ""),
        ("playEffects", expected_effects),
        ("playMovePositionType", MOVE_LOST),
        ("moveEffectTriggerType", "ProduceCardMoveEffectTriggerType_Unknown"),
        ("moveProduceExamEffectIds", []),
        ("isEndTurnLost", False),
        ("isInitial", False),
        ("isRestrict", False),
        ("produceCardStatusEnchantId", ""),
        ("searchTag", "idol-unique"),
        ("libraryHidden", False),
        ("noDeckDuplication", True),
        ("isReward", False),
        ("unlockProducerLevel", 0),
        ("rentalUnlockProducerLevel", 0),
        ("evaluation", _CARD_EVALUATION_BY_UPGRADE[upgrade]),
        ("originIdolCardId", "i_card-hume-3-017"),
        ("originSupportCardId", ""),
        ("isInitialDeckProduceCard", False),
        ("effectGroupIds", _CARD_EFFECT_GROUPS),
        ("produceCardCustomizeIds", []),
        ("maxCustomizeCount", 0),
        ("isConversion", False),
        ("moveProduceExamTriggerIds", []),
        ("originCharacterId", ""),
        ("originPrimaStellaIdolCardId", ""),
        ("viewStartTime", "1775786400000"),
        ("isLimited", False),
        ("order", "48020010830176"),
    )
    for key, expected in exact:
        _require_equal(raw, key, expected, row_id)
    descriptions = _json_array(
        row["descriptions_json"], f"{row_id}.descriptions_json"
    )
    if raw["produceDescriptions"] != descriptions:
        raise Plan2BlockDependConsumptionSumContractError(
            f"{row_id}: description column/raw payload mismatch"
        )
    _validate_descriptions(descriptions, 34, f"{row_id}.produceDescriptions")
    return Plan2BlockDependCardVersion(
        upgrade=upgrade,
        name=expected_name,
        stamina=_CARD_STAMINA_BY_UPGRADE[upgrade],
        play_effect_ids=tuple(
            str(item["produceExamEffectId"]) for item in expected_effects
        ),
    )


def _validate_trigger(row: sqlite3.Row) -> None:
    row_id = TARGET_TRIGGER_ID
    expected_columns = (
        ("phase_types_json", [START_TURN_PHASE_TYPE]),
        ("phase_values_json", []),
        ("field_status_check_types_json", []),
        ("field_status_types_json", [NO_BLOCK_FIELD_STATUS_TYPE]),
        ("field_status_values_json", []),
        ("field_status_produce_card_search_ids_json", []),
        ("produce_card_search_id", ""),
        ("upper_search_count", 0),
        ("lower_search_count", 0),
        ("card_move_position_type", MOVE_UNKNOWN),
        ("effect_types_json", []),
        ("lesson_type", LESSON_UNKNOWN),
    )
    for key, expected in expected_columns:
        actual = row[key]
        if key.endswith("_json"):
            actual = _json_array(actual, f"{row_id}.{key}")
        if actual != expected:
            raise Plan2BlockDependConsumptionSumContractError(
                f"{row_id}: {key} must be {expected!r}"
            )
    raw = _json_object(row["raw_json"], row_id)
    keys = {
        "id",
        "phaseTypes",
        "phaseValues",
        "fieldStatusCheckTypes",
        "fieldStatusTypes",
        "fieldStatusValues",
        "fieldStatusProduceCardSearchIds",
        "produceCardSearchId",
        "upperSearchCount",
        "lowerSearchCount",
        "cardMovePositionType",
        "effectTypes",
        "lessonType",
        "produceDescriptions",
        "playProduceDescriptions",
        "playEffectProduceDescriptions",
    }
    _require_exact_keys(raw, keys, row_id)
    exact = (
        ("id", row_id),
        ("phaseTypes", [START_TURN_PHASE_TYPE]),
        ("phaseValues", []),
        ("fieldStatusCheckTypes", []),
        ("fieldStatusTypes", [NO_BLOCK_FIELD_STATUS_TYPE]),
        ("fieldStatusValues", []),
        ("fieldStatusProduceCardSearchIds", []),
        ("produceCardSearchId", ""),
        ("upperSearchCount", 0),
        ("lowerSearchCount", 0),
        ("cardMovePositionType", MOVE_UNKNOWN),
        ("effectTypes", []),
        ("lessonType", LESSON_UNKNOWN),
    )
    for key, expected in exact:
        _require_equal(raw, key, expected, row_id)
    _validate_descriptions(raw["produceDescriptions"], 3, f"{row_id}.produceDescriptions")
    _validate_descriptions(raw["playProduceDescriptions"], 2, f"{row_id}.playProduceDescriptions")
    _validate_descriptions(raw["playEffectProduceDescriptions"], 2, f"{row_id}.playEffectProduceDescriptions")


def load_plan2_block_depend_consumption_sum_program(
    database: Path = DEFAULT_DATABASE,
) -> Plan2BlockDependConsumptionSumProgram:
    """Load the exact four-card Master slice and fail closed on any drift."""

    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        cards = connection.execute(
            "SELECT * FROM card WHERE id = ? ORDER BY upgrade_count",
            (TARGET_CARD_ID,),
        ).fetchall()
        wrapper = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (TARGET_WRAPPER_EFFECT_ID,)
        ).fetchone()
        status = connection.execute(
            "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
            (TARGET_STATUS_ENCHANT_ID,),
        ).fetchone()
        trigger = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?",
            (TARGET_TRIGGER_ID,),
        ).fetchone()
        child = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (TARGET_CHILD_EFFECT_ID,)
        ).fetchone()
    if len(cards) != 4 or any(
        row is None for row in (wrapper, status, trigger, child)
    ):
        raise Plan2BlockDependConsumptionSumContractError(
            "target Master slice is incomplete"
        )
    card_versions = tuple(
        _validate_card(row, upgrade)
        for upgrade, row in zip(TARGET_UPGRADES, cards, strict=True)
    )
    _validate_effect_raw(
        wrapper,
        effect_type=STATUS_ENCHANT_EFFECT_TYPE,
        value1=0,
        value2=0,
        count=TARGET_LISTENER_LIMIT,
        turn=PERMANENT_TURN,
        status_id=TARGET_STATUS_ENCHANT_ID,
        effect_groups=[
            "effect_group-visible-exam_block-000",
            "effect_group-visible-exam_status_enchant-000",
        ],
        description_counts=(16, 15),
    )
    _validate_effect_raw(
        child,
        effect_type=BLOCK_DEPEND_CONSUMPTION_SUM_EFFECT_TYPE,
        value1=TARGET_VALUE_PERMILLE,
        value2=0,
        count=TARGET_EFFECT_COUNT,
        turn=TARGET_EFFECT_TURN,
        status_id="",
        effect_groups=["effect_group-visible-exam_block-000"],
        description_counts=(8, 9),
    )
    status_raw = _json_object(status["raw_json"], TARGET_STATUS_ENCHANT_ID)
    _require_exact_keys(
        status_raw,
        {
            "id",
            "assetId",
            "produceDescriptions",
            "produceExamTriggerId",
            "produceExamEffectIds",
        },
        TARGET_STATUS_ENCHANT_ID,
    )
    child_ids = [TARGET_CHILD_EFFECT_ID]
    status_expected = (
        ("id", TARGET_STATUS_ENCHANT_ID),
        ("assetId", ""),
        ("produceExamTriggerId", TARGET_TRIGGER_ID),
        ("produceExamEffectIds", child_ids),
    )
    for key, expected in status_expected:
        _require_equal(status_raw, key, expected, TARGET_STATUS_ENCHANT_ID)
    if status["asset_id"] != "" or status["produce_exam_trigger_id"] != TARGET_TRIGGER_ID:
        raise Plan2BlockDependConsumptionSumContractError(
            f"{TARGET_STATUS_ENCHANT_ID}: normalized status fields changed"
        )
    if _json_array(
        status["produce_exam_effect_ids_json"],
        f"{TARGET_STATUS_ENCHANT_ID}.produce_exam_effect_ids_json",
    ) != child_ids:
        raise Plan2BlockDependConsumptionSumContractError(
            f"{TARGET_STATUS_ENCHANT_ID}: ordered child list changed"
        )
    _validate_descriptions(
        status_raw["produceDescriptions"], 14,
        f"{TARGET_STATUS_ENCHANT_ID}.produceDescriptions",
    )
    _validate_trigger(trigger)
    child_effect = Plan2EndTurnEffect(
        effect_id=TARGET_CHILD_EFFECT_ID,
        effect_type=BLOCK_DEPEND_CONSUMPTION_SUM_EFFECT_TYPE,
        value1=TARGET_VALUE_PERMILLE,
        value2=0,
        count=TARGET_EFFECT_COUNT,
        turn=TARGET_EFFECT_TURN,
        effect_group_ids=("effect_group-visible-exam_block-000",),
    )
    return Plan2BlockDependConsumptionSumProgram(
        cards=card_versions,
        wrapper_effect_id=TARGET_WRAPPER_EFFECT_ID,
        status_enchant_id=TARGET_STATUS_ENCHANT_ID,
        trigger_id=TARGET_TRIGGER_ID,
        phase_type=START_TURN_PHASE_TYPE,
        field_status_type=NO_BLOCK_FIELD_STATUS_TYPE,
        effects=(child_effect,),
        wrapper_effect_groups=(
            "effect_group-visible-exam_block-000",
            "effect_group-visible-exam_status_enchant-000",
        ),
        listener_turn=PERMANENT_TURN,
        listener_limit_count=TARGET_LISTENER_LIMIT,
        listener_limit_count_in_turn=UNLIMITED_COUNT,
    )


@dataclass(frozen=True, slots=True)
class Plan2BlockDependConsumptionSumRuntime:
    """Smallest state read by SetBlock and the target child executor."""

    block: int = 0
    block_consumption_sum_count: int = 0
    add_block_status: AddBlockStatus = AddBlockStatus()
    add_block_settings: AddBlockSettings = AddBlockSettings()

    def __post_init__(self) -> None:
        _plain_i32(self.block, "block")
        _plain_i32(
            self.block_consumption_sum_count,
            "block_consumption_sum_count",
        )
        if not isinstance(self.add_block_status, AddBlockStatus):
            raise TypeError("add_block_status must be AddBlockStatus")
        if not isinstance(self.add_block_settings, AddBlockSettings):
            raise TypeError("add_block_settings must be AddBlockSettings")


@dataclass(frozen=True, slots=True)
class Plan2SetBlockTransition:
    before: Plan2BlockDependConsumptionSumRuntime
    after: Plan2BlockDependConsumptionSumRuntime
    requested_block: int
    is_consumption: bool
    consumption_delta: int
    sum_before: int
    sum_after: int


def apply_plan2_set_block(
    runtime: Plan2BlockDependConsumptionSumRuntime,
    block: int,
    *,
    is_consumption: bool = False,
) -> Plan2SetBlockTransition:
    """Project native ``ExamParameterModel.SetBlock`` including its counter."""

    if not isinstance(runtime, Plan2BlockDependConsumptionSumRuntime):
        raise TypeError("runtime must be Plan2BlockDependConsumptionSumRuntime")
    block = _plain_i32(block, "block")
    if type(is_consumption) is not bool:
        raise TypeError("is_consumption must be boolean")
    delta = 0
    next_sum = runtime.block_consumption_sum_count
    if is_consumption:
        delta = max(_i32(runtime.block - block), 0)
        next_sum = _i32(next_sum + delta)
    after = replace(
        runtime,
        block=block,
        block_consumption_sum_count=next_sum,
    )
    return Plan2SetBlockTransition(
        before=runtime,
        after=after,
        requested_block=block,
        is_consumption=is_consumption,
        consumption_delta=delta,
        sum_before=runtime.block_consumption_sum_count,
        sum_after=next_sum,
    )


@dataclass(frozen=True, slots=True)
class Plan2BlockDependDifference:
    kind: Literal["block", "effect"]
    preview: int
    current: int
    block_consumption_sum_count: int | None
    status_effect_type: int | None
    is_consumption: bool = False


@dataclass(frozen=True, slots=True)
class Plan2BlockDependChildTransition:
    before: Plan2BlockDependConsumptionSumRuntime
    after: Plan2BlockDependConsumptionSumRuntime
    effect: Plan2EndTurnEffect
    sum_at_execution: int
    ratio: float
    requested_value: int
    calculated_value: int
    block_fix_value: int
    differences: tuple[Plan2BlockDependDifference, ...]
    event_trace: tuple[str, ...]

    @property
    def applied_block(self) -> int:
        return _i32(self.after.block - self.before.block)


def execute_plan2_block_depend_consumption_sum_child(
    runtime: Plan2BlockDependConsumptionSumRuntime,
    effect: Plan2EndTurnEffect,
) -> Plan2BlockDependChildTransition:
    """Execute the target child against the runtime state at execution time."""

    if not isinstance(runtime, Plan2BlockDependConsumptionSumRuntime):
        raise TypeError("runtime must be Plan2BlockDependConsumptionSumRuntime")
    if not isinstance(effect, Plan2EndTurnEffect):
        raise TypeError("effect must be Plan2EndTurnEffect")
    if (
        effect.effect_id != TARGET_CHILD_EFFECT_ID
        or effect.effect_type != BLOCK_DEPEND_CONSUMPTION_SUM_EFFECT_TYPE
        or effect.value1 != TARGET_VALUE_PERMILLE
        or effect.value2 != 0
        or effect.count != TARGET_EFFECT_COUNT
        or effect.turn != TARGET_EFFECT_TURN
        or effect.effect_group_ids
        != ("effect_group-visible-exam_block-000",)
    ):
        raise Plan2BlockDependConsumptionSumContractError(
            "runtime child shape is outside the exact target"
        )
    sum_at_execution = runtime.block_consumption_sum_count
    ratio = permille_to_f32(effect.value1)
    requested = ceil_f32_to_i32(f32(ratio * f32(sum_at_execution)))
    calculated = calculate_add_block(
        requested,
        is_buff_active=True,
        status=runtime.add_block_status,
        settings=runtime.add_block_settings,
        additional_multiple_aggressive_rate=1.0,
    )
    # Native AddBlockFix: max(i32(-currentBlock), value), then wrapping add,
    # followed by SetBlock(..., isConsumption=false).
    block_fix_value = max(_i32(-runtime.block), calculated)
    block_after = _i32(runtime.block + block_fix_value)
    block_difference = Plan2BlockDependDifference(
        kind="block",
        preview=runtime.block,
        current=block_after,
        block_consumption_sum_count=sum_at_execution,
        status_effect_type=None,
    )
    after = replace(runtime, block=block_after)
    effect_difference = Plan2BlockDependDifference(
        kind="effect",
        preview=runtime.block,
        current=block_after,
        block_consumption_sum_count=None,
        status_effect_type=BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE,
    )
    return Plan2BlockDependChildTransition(
        before=runtime,
        after=after,
        effect=effect,
        sum_at_execution=sum_at_execution,
        ratio=ratio,
        requested_value=requested,
        calculated_value=calculated,
        block_fix_value=block_fix_value,
        differences=(block_difference, effect_difference),
        event_trace=(
            "executor:read:BlockConsumptionSumCount",
            "executor:FloatFromPermil(value1)",
            "executor:ceil-f32:ratio*sum",
            "executor:CalculateAddBlock(isBuffActive=true,rate=1.0)",
            "AddBlockFix:append:block-difference",
            "AddBlockFix:SetBlock(isConsumption=false)",
            "executor:append:effect-difference(statusEffectType=3)",
        ),
    )


@dataclass(frozen=True, slots=True)
class Plan2BlockDependListener:
    status_uid: int
    wrapper_effect_id: str
    status_enchant_id: str
    trigger_id: str
    effects: tuple[Plan2EndTurnEffect, ...]
    remaining_turns: int = PERMANENT_TURN
    remaining_count: int = TARGET_LISTENER_LIMIT
    remaining_count_in_turn: int = UNLIMITED_COUNT

    def __post_init__(self) -> None:
        if isinstance(self.status_uid, bool) or not isinstance(self.status_uid, int):
            raise TypeError("status_uid must be an integer")
        if self.status_uid < 1:
            raise ValueError("status_uid must be positive")
        if (
            self.wrapper_effect_id != TARGET_WRAPPER_EFFECT_ID
            or self.status_enchant_id != TARGET_STATUS_ENCHANT_ID
            or self.trigger_id != TARGET_TRIGGER_ID
            or len(self.effects) != 1
        ):
            raise Plan2BlockDependConsumptionSumContractError(
                "listener identity/child shape is outside the exact target"
            )
        child = self.effects[0]
        if (
            not isinstance(child, Plan2EndTurnEffect)
            or child.effect_id != TARGET_CHILD_EFFECT_ID
            or child.effect_type != BLOCK_DEPEND_CONSUMPTION_SUM_EFFECT_TYPE
            or child.value1 != TARGET_VALUE_PERMILLE
            or child.value2 != 0
            or child.count != TARGET_EFFECT_COUNT
            or child.turn != TARGET_EFFECT_TURN
            or child.effect_group_ids
            != ("effect_group-visible-exam_block-000",)
        ):
            raise Plan2BlockDependConsumptionSumContractError(
                "listener child shape is outside the exact target"
            )
        if self.remaining_turns != PERMANENT_TURN:
            raise Plan2BlockDependConsumptionSumContractError(
                "target listener must remain permanent"
            )
        if not 0 <= self.remaining_count <= TARGET_LISTENER_LIMIT:
            raise Plan2BlockDependConsumptionSumContractError(
                "remaining_count must be within 0..3"
            )
        if self.remaining_count_in_turn != UNLIMITED_COUNT:
            raise Plan2BlockDependConsumptionSumContractError(
                "target listener has no per-turn count"
            )

    @property
    def can_trigger(self) -> bool:
        return self.remaining_count > 0


@dataclass(frozen=True, slots=True)
class Plan2BlockDependScheduler:
    listeners: tuple[Plan2BlockDependListener, ...] = ()
    next_status_uid: int = 1

    def __post_init__(self) -> None:
        listeners = tuple(self.listeners)
        if any(not isinstance(item, Plan2BlockDependListener) for item in listeners):
            raise TypeError("listeners must contain Plan2BlockDependListener")
        object.__setattr__(self, "listeners", listeners)
        uids = [item.status_uid for item in listeners]
        if len(uids) != len(set(uids)):
            raise ValueError("listener status_uids must be unique")
        if (
            isinstance(self.next_status_uid, bool)
            or not isinstance(self.next_status_uid, int)
            or self.next_status_uid < 1
            or (uids and self.next_status_uid <= max(uids))
        ):
            raise ValueError("next_status_uid must exceed active listener uids")


@dataclass(frozen=True, slots=True)
class Plan2BlockDependInstallTransition:
    before: Plan2BlockDependScheduler
    after: Plan2BlockDependScheduler
    card_upgrade: int
    created_status_uid: int
    event_trace: tuple[str, ...]


def install_plan2_block_depend_consumption_sum_listener(
    scheduler: Plan2BlockDependScheduler,
    program: Plan2BlockDependConsumptionSumProgram,
    *,
    card_id: str,
    upgrade: int,
) -> Plan2BlockDependInstallTransition:
    """Install the third ordered play effect as one distinct native listener."""

    if not isinstance(scheduler, Plan2BlockDependScheduler):
        raise TypeError("scheduler must be Plan2BlockDependScheduler")
    if not isinstance(program, Plan2BlockDependConsumptionSumProgram):
        raise TypeError("program must be Plan2BlockDependConsumptionSumProgram")
    if card_id != TARGET_CARD_ID or upgrade not in TARGET_UPGRADES:
        raise Plan2BlockDependConsumptionSumContractError(
            "card identity/upgrade is outside the exact target"
        )
    card = program.cards[upgrade]
    if (
        card.upgrade != upgrade
        or card.installer_index != 2
        or card.play_effect_ids[2] != program.wrapper_effect_id
    ):
        raise Plan2BlockDependConsumptionSumContractError(
            "card installer binding is not exact"
        )
    uid = scheduler.next_status_uid
    listener = Plan2BlockDependListener(
        status_uid=uid,
        wrapper_effect_id=program.wrapper_effect_id,
        status_enchant_id=program.status_enchant_id,
        trigger_id=program.trigger_id,
        effects=program.effects,
    )
    after = replace(
        scheduler,
        listeners=scheduler.listeners + (listener,),
        next_status_uid=uid + 1,
    )
    return Plan2BlockDependInstallTransition(
        before=scheduler,
        after=after,
        card_upgrade=upgrade,
        created_status_uid=uid,
        event_trace=(
            "card-play:effect:0:ExamBlock",
            "card-play:effect:1:ExamCardPlayAggressive",
            "card-play:effect:2:ExamStatusEnchant",
            f"listener:install:{uid}:limit=3:per-turn=-1:turn=-1",
        ),
    )


@dataclass(frozen=True, slots=True)
class Plan2NoBlockAdmission:
    """Exact hand-off expected from the separately owned NoBlock trigger."""

    admitted: bool
    trigger_id: str = TARGET_TRIGGER_ID
    phase_type: str = START_TURN_PHASE_TYPE
    snapshot: str = "settled-start-turn"

    def __post_init__(self) -> None:
        if type(self.admitted) is not bool:
            raise TypeError("admitted must be boolean")
        if (
            self.trigger_id != TARGET_TRIGGER_ID
            or self.phase_type != START_TURN_PHASE_TYPE
            or self.snapshot != "settled-start-turn"
        ):
            raise Plan2BlockDependConsumptionSumContractError(
                "NoBlock admission shape is outside the exact hand-off"
            )


@dataclass(frozen=True, slots=True)
class Plan2CapturedBlockDependCommand:
    listener_status_uid: int
    effect_snapshot: tuple[Plan2EndTurnEffect, ...]
    runtime_values_snapshotted: bool = False


@dataclass(frozen=True, slots=True)
class Plan2BlockDependCaptureTransition:
    before: Plan2BlockDependScheduler
    after_spend: Plan2BlockDependScheduler
    commands: tuple[Plan2CapturedBlockDependCommand, ...]
    admitted: bool
    fail_closed_reason: str | None
    event_trace: tuple[str, ...]


def capture_plan2_block_depend_start_turn_commands(
    scheduler: Plan2BlockDependScheduler,
    admission: Plan2NoBlockAdmission | None,
) -> Plan2BlockDependCaptureTransition:
    """Snapshot child commands, then spend count exactly as native does."""

    if not isinstance(scheduler, Plan2BlockDependScheduler):
        raise TypeError("scheduler must be Plan2BlockDependScheduler")
    if admission is None or not isinstance(admission, Plan2NoBlockAdmission):
        return Plan2BlockDependCaptureTransition(
            before=scheduler,
            after_spend=scheduler,
            commands=(),
            admitted=False,
            fail_closed_reason="NoBlock-admission-unavailable-or-unknown",
            event_trace=("phase:ExamStartTurn:fail-closed:NoBlock",),
        )
    if not admission.admitted:
        return Plan2BlockDependCaptureTransition(
            before=scheduler,
            after_spend=scheduler,
            commands=(),
            admitted=False,
            fail_closed_reason=None,
            event_trace=("phase:ExamStartTurn:NoBlock:not-admitted",),
        )
    listeners = list(scheduler.listeners)
    commands: list[Plan2CapturedBlockDependCommand] = []
    trace: list[str] = ["phase:ExamStartTurn:NoBlock:admitted"]
    # The native source enumerable is consumed in stable active-list order.
    for index, snapshot_listener in enumerate(scheduler.listeners):
        if not snapshot_listener.can_trigger:
            continue
        commands.append(
            Plan2CapturedBlockDependCommand(
                listener_status_uid=snapshot_listener.status_uid,
                effect_snapshot=snapshot_listener.effects,
            )
        )
        trace.extend(
            (
                f"listener:{snapshot_listener.status_uid}:build:open-trigger",
                f"listener:{snapshot_listener.status_uid}:snapshot:child:0:{TARGET_CHILD_EFFECT_ID}",
                f"listener:{snapshot_listener.status_uid}:SpendCount:{snapshot_listener.remaining_count}->{snapshot_listener.remaining_count-1}",
                f"listener:{snapshot_listener.status_uid}:build:close-trigger",
            )
        )
        listeners[index] = replace(
            snapshot_listener,
            remaining_count=snapshot_listener.remaining_count - 1,
        )
    after = replace(scheduler, listeners=tuple(listeners))
    return Plan2BlockDependCaptureTransition(
        before=scheduler,
        after_spend=after,
        commands=tuple(commands),
        admitted=True,
        fail_closed_reason=None,
        event_trace=tuple(trace),
    )


@dataclass(frozen=True, slots=True)
class Plan2BlockDependCommandOccurrence:
    listener_status_uid: int
    child_transitions: tuple[Plan2BlockDependChildTransition, ...]


@dataclass(frozen=True, slots=True)
class Plan2BlockDependExecutionTransition:
    before_runtime: Plan2BlockDependConsumptionSumRuntime
    after_runtime: Plan2BlockDependConsumptionSumRuntime
    before_scheduler: Plan2BlockDependScheduler
    after_scheduler: Plan2BlockDependScheduler
    occurrences: tuple[Plan2BlockDependCommandOccurrence, ...]
    removed_status_uids: tuple[int, ...]
    event_trace: tuple[str, ...]


def execute_captured_plan2_block_depend_commands(
    runtime: Plan2BlockDependConsumptionSumRuntime,
    capture: Plan2BlockDependCaptureTransition,
) -> Plan2BlockDependExecutionTransition:
    """Execute captured children; accumulator values are read only now."""

    if not isinstance(runtime, Plan2BlockDependConsumptionSumRuntime):
        raise TypeError("runtime must be Plan2BlockDependConsumptionSumRuntime")
    if not isinstance(capture, Plan2BlockDependCaptureTransition):
        raise TypeError("capture must be Plan2BlockDependCaptureTransition")
    scheduler = capture.after_spend
    current = runtime
    removed: list[int] = []
    occurrences: list[Plan2BlockDependCommandOccurrence] = []
    trace: list[str] = []
    for command in capture.commands:
        trace.append(f"listener:{command.listener_status_uid}:execute:open-trigger")
        exhausted = tuple(
            listener.status_uid
            for listener in scheduler.listeners
            if listener.remaining_count == 0
        )
        if exhausted:
            scheduler = replace(
                scheduler,
                listeners=tuple(
                    listener
                    for listener in scheduler.listeners
                    if listener.status_uid not in exhausted
                ),
            )
            for uid in exhausted:
                if uid not in removed:
                    removed.append(uid)
                    trace.append(f"listener:{uid}:remove-count-limited")
        child_rows: list[Plan2BlockDependChildTransition] = []
        for child_index, effect in enumerate(command.effect_snapshot):
            child = execute_plan2_block_depend_consumption_sum_child(
                current, effect
            )
            child_rows.append(child)
            current = child.after
            trace.append(
                f"listener:{command.listener_status_uid}:execute:child:{child_index}:sum={child.sum_at_execution}"
            )
        trace.append(f"listener:{command.listener_status_uid}:execute:close-trigger")
        occurrences.append(
            Plan2BlockDependCommandOccurrence(
                listener_status_uid=command.listener_status_uid,
                child_transitions=tuple(child_rows),
            )
        )
    return Plan2BlockDependExecutionTransition(
        before_runtime=runtime,
        after_runtime=current,
        before_scheduler=capture.after_spend,
        after_scheduler=scheduler,
        occurrences=tuple(occurrences),
        removed_status_uids=tuple(removed),
        event_trace=tuple(trace),
    )


__all__ = [
    "ANDROID_BLOCK_DEPEND_CONSUMPTION_SUM_EVIDENCE",
    "BLOCK_DEPEND_CONSUMPTION_SUM_EFFECT_TYPE",
    "FORMAL_AFFECTED_VERSION_REFS",
    "FORMAL_CO_BLOCKED_VERSION_REFS",
    "FORMAL_DIRECT_VERSION_REFS",
    "PC_BLOCK_DEPEND_CONSUMPTION_SUM_METADATA",
    "Plan2BlockDependCaptureTransition",
    "Plan2BlockDependChildTransition",
    "Plan2BlockDependCommandOccurrence",
    "Plan2BlockDependConsumptionSumContractError",
    "Plan2BlockDependConsumptionSumProgram",
    "Plan2BlockDependConsumptionSumRuntime",
    "Plan2BlockDependExecutionTransition",
    "Plan2BlockDependInstallTransition",
    "Plan2BlockDependListener",
    "Plan2BlockDependScheduler",
    "Plan2CapturedBlockDependCommand",
    "Plan2NoBlockAdmission",
    "Plan2SetBlockTransition",
    "TARGET_CARD_ID",
    "TARGET_CHILD_EFFECT_ID",
    "TARGET_STATUS_ENCHANT_ID",
    "TARGET_TRIGGER_ID",
    "TARGET_UPGRADES",
    "TARGET_VALUE_PERMILLE",
    "TARGET_WRAPPER_EFFECT_ID",
    "apply_plan2_set_block",
    "capture_plan2_block_depend_start_turn_commands",
    "execute_captured_plan2_block_depend_commands",
    "execute_plan2_block_depend_consumption_sum_child",
    "install_plan2_block_depend_consumption_sum_listener",
    "load_plan2_block_depend_consumption_sum_program",
]
