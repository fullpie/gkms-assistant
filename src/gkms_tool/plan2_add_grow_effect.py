"""Exact standalone Plan2 ``ExamAddGrowEffect`` slice.

Only ``p_card-02-men-100_008#0`` (``究極スマイル``) is admitted.  The
native executor is deliberately separated from the per-card GUID mutation:

* ``AddGrowEffectExecutor`` searches cards, filters grow rows against each
  card's direct play effects, and captures ordered GUID affect groups.
* ``CardPhaseStatusAffect`` rematches those GUIDs against the current
  ``DeckAll`` positions and calls ``ExamCardData.AddGrowEffect``.

The target grow row is ``ReviewAdd +6``.  It applies only to cards containing
a direct ``ExamReview`` play effect.  It is not Plan3 lesson growth and it is
not made applicable by a live ReviewMultiple status.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import yaml

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_review_multiple import (
    Plan2ReviewMultipleContractError,
    Plan2ReviewMultipleEffect,
    Plan2ReviewMultipleTransition,
    load_plan2_review_multiple_effect,
    simulate_review_multiple,
)
from .plan2_state import Plan2EndTurnEffect, Plan2State


TARGET_CARD_ID = "p_card-02-men-100_008"
TARGET_UPGRADE = 0
TARGET_CARD_NAME = "究極スマイル"
TARGET_TRIGGER_ID = "e_trigger-none-review_up-6"
TARGET_REVIEW_MULTIPLE_EFFECT_ID = "e_effect-exam_review_multiple-1000-inf"
TARGET_ADD_GROW_EFFECT_ID = (
    "e_effect-exam_add_grow_effect-p_card_search-deck_all-all-0_0-"
    "g_effect-review_add-6"
)
TARGET_PLAYABLE_VALUE_ADD_EFFECT_ID = "e_effect-exam_playable_value_add-01"
TARGET_SEARCH_ID = "p_card_search-deck_all"
TARGET_GROW_EFFECT_ID = "g_effect-review_add-6"

PLAN2 = "ProducePlanType_Plan2"
MENTAL_SKILL = "ProduceCardCategory_MentalSkill"
UNKNOWN_COST = "ExamCostType_Unknown"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
MOVE_LOST = "ProduceCardMovePositionType_Lost"
PICK_ALL = "ProducePickRangeType_All"
PICK_UNKNOWN = "ProducePickRangeType_Unknown"
PICK_COUNT_UNKNOWN = "ProducePickCountType_Unknown"
EXAM_EFFECT_UNKNOWN = "ProduceExamEffectType_Unknown"
PHASE_NONE = "ProduceExamPhaseType_None"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
REVIEW_UP_FIELD = "ProduceExamFieldStatusType_ReviewUp"
CARD_STATUS_UNKNOWN = "ProduceCardSearchStatusType_Unknown"
CARD_ORDER_UNKNOWN = "ProduceCardOrderType_Unknown"
CARD_POSITION_DECK_ALL = "ProduceCardPositionType_DeckAll"
MIN_MAX_UNKNOWN = "ConditionMinMaxType_Unknown"

REVIEW_MULTIPLE_EFFECT_TYPE = "ProduceExamEffectType_ExamReviewMultiple"
ADD_GROW_EFFECT_TYPE = "ProduceExamEffectType_ExamAddGrowEffect"
PLAYABLE_VALUE_ADD_EFFECT_TYPE = "ProduceExamEffectType_ExamPlayableValueAdd"
REVIEW_EFFECT_TYPE = "ProduceExamEffectType_ExamReview"
REVIEW_ADD_GROW_TYPE = "ProduceCardGrowEffectType_ReviewAdd"
REVIEW_REDUCE_GROW_TYPE = "ProduceCardGrowEffectType_ReviewReduce"

TARGET_REVIEW_THRESHOLD = 6
TARGET_REVIEW_MULTIPLE_PERMILLE = 1000
TARGET_GROW_VALUE = 6

FORMAL_AFFECTED_VERSION_REFS = (f"{TARGET_CARD_ID}#0",)
FORMAL_DIRECT_VERSION_REFS = FORMAL_AFFECTED_VERSION_REFS
FORMAL_CO_BLOCKED_VERSION_REFS: tuple[str, ...] = ()

DEFAULT_MASTER_DIR = Path(__file__).resolve().parents[2] / "_research/gakumasu-diff"

ANDROID_PLAN2_ADD_GROW_EVIDENCE = {
    "executor": {
        "ctor_va": "0x7E6EED0",
        "execute_va": "0x7E6F1E8",
        "fact": (
            "ctor copies CardSearch, pick fields, reference search, and the "
            "ordered grow list; effectValue1/value2/count/turn are not copied"
        ),
    },
    "selection": {
        "get_search_card_list_va": "0x7E57B94",
        "is_valid_card_va": "0x7E61DDC",
        "is_valid_effect_va": "0x7E66EA4",
        "review_pair_branch_va": "0x7E67400",
        "review_effect_compare_va": "0x7E67474",
        "fact": (
            "ReviewAdd/ReviewReduce rows are valid only when a card has a "
            "direct play effect whose ProduceExamEffectType is ExamReview (31)"
        ),
    },
    "guid_transaction": {
        "card_phase_status_affect_va": "0x7E602AC",
        "guid_match_va": "0x7E6CD9C",
        "grow_apply_va": "0x7E6CDDC",
        "card_add_grow_va": "0x808FFCC",
        "card_guid_getter_va": "0x808F030",
        "fact": (
            "searched cards become ordered GUID affect groups; execution "
            "rematches current DeckAll positions by GUID"
        ),
    },
    "aggregate": {
        "utility_add_grow_va": "0x7E62948",
        "reset_cache_va": "0x808FF7C",
        "grow_list_offset": "0x50",
        "grow_id_list_offset": "0x58",
        "fact": (
            "same-sign rows add with wrapping Int32; opposite rows cancel, "
            "remove at zero, or replace with the positive opposite type; the "
            "source grow ID is appended after aggregate mutation"
        ),
    },
    "review_formula": {
        "materializer_va": "0x7E62E8C",
        "review_predicate_va": "0x7E6C594",
        "review_action_va": "0x7E6D7E4",
        "math_max_va": "0xDC1DDC8",
        "expression": (
            "max(1, i32(base ExamReview.effectValue1 + signed ReviewGrow))"
        ),
        "number_kind": "signed Int32 only; no float or permille conversion",
    },
    "deck_all_order": (
        "Hand",
        "Deck",
        "Grave",
        "Lost",
        "Hold",
        "Playing",
        "FutureDecks",
        "PastDecks",
    ),
}

PC_PLAN2_ADD_GROW_METADATA = {
    "AddGrowEffectExecutor": {
        "type_index": 3501,
        "ctor": {"method_index": 17754, "token": 100681051},
        "ExecuteEffect": {"method_index": 17755, "token": 100681052},
    },
    "ExamEffectUtility": {
        "GetSearchCardList": {"method_index": 17569, "token": 100680866},
        "CardPhaseStatusAffect": {"method_index": 17600, "token": 100680897},
        "AddGrowEffect": {"method_index": 17609, "token": 100680906},
        "IsValidCardGrowEffect.card": {
            "method_index": 17619,
            "token": 100680916,
        },
        "IsValidCardGrowEffect.effect": {
            "method_index": 17621,
            "token": 100680918,
        },
    },
    "ReviewMultipleEffectExecutor": {
        "type_index": 3640,
        "ExecuteEffect": {"method_index": 18049, "token": 100681346},
    },
    "PlayableValueAddEffectExecutor": {
        "type_index": 3631,
        "ExecuteEffect": {"method_index": 18031, "token": 100681328},
    },
}


class Plan2AddGrowContractError(ValueError):
    """A Master/runtime value is outside this one proven Plan2 slice."""


def _plain_i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2AddGrowContractError(f"{label} must be a plain Int32")
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2AddGrowContractError(f"{label} is outside Int32")
    return value


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _json_object(raw_json: object, row_id: str) -> dict[str, object]:
    try:
        value = json.loads(str(raw_json))
    except json.JSONDecodeError as error:
        raise Plan2AddGrowContractError(f"{row_id}: invalid raw_json") from error
    if not isinstance(value, dict):
        raise Plan2AddGrowContractError(f"{row_id}: raw_json must be an object")
    return value


def _json_array(raw_json: object, label: str) -> list[object]:
    try:
        value = json.loads(str(raw_json))
    except json.JSONDecodeError as error:
        raise Plan2AddGrowContractError(f"{label}: invalid JSON") from error
    if not isinstance(value, list):
        raise Plan2AddGrowContractError(f"{label} must be an array")
    return value


def _require_exact_keys(
    raw: dict[str, object], expected: set[str], row_id: str
) -> None:
    if set(raw) != expected:
        raise Plan2AddGrowContractError(
            f"{row_id}: raw key set changed; "
            f"missing={sorted(expected-set(raw))!r}, "
            f"extra={sorted(set(raw)-expected)!r}"
        )


def _require_equal(
    raw: dict[str, object], key: str, expected: object, row_id: str
) -> None:
    if raw.get(key) != expected:
        raise Plan2AddGrowContractError(
            f"{row_id}: {key} must be {expected!r}, got {raw.get(key)!r}"
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


def _validate_descriptions(value: object, count: int, label: str) -> None:
    if not isinstance(value, list) or len(value) != count:
        raise Plan2AddGrowContractError(
            f"{label} must contain exactly {count} descriptions"
        )
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != _DESCRIPTION_KEYS:
            raise Plan2AddGrowContractError(
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


@dataclass(frozen=True, slots=True)
class Plan2AddGrowMasterEffect:
    effect_id: str = TARGET_ADD_GROW_EFFECT_ID
    effect_type: str = ADD_GROW_EFFECT_TYPE
    value1: int = 0
    value2: int = 0
    count: int = 0
    turn: int = 0
    search_id: str = TARGET_SEARCH_ID
    pick_range_type: str = PICK_ALL
    pick_count_min: int = 0
    pick_count_max: int = 0
    pick_count_type: str = PICK_COUNT_UNKNOWN
    reference_search_id: str = ""
    grow_effect_ids: tuple[str, ...] = (TARGET_GROW_EFFECT_ID,)

    def __post_init__(self) -> None:
        exact = (
            self.effect_id,
            self.effect_type,
            self.value1,
            self.value2,
            self.count,
            self.turn,
            self.search_id,
            self.pick_range_type,
            self.pick_count_min,
            self.pick_count_max,
            self.pick_count_type,
            self.reference_search_id,
            tuple(self.grow_effect_ids),
        )
        expected = (
            TARGET_ADD_GROW_EFFECT_ID,
            ADD_GROW_EFFECT_TYPE,
            0,
            0,
            0,
            0,
            TARGET_SEARCH_ID,
            PICK_ALL,
            0,
            0,
            PICK_COUNT_UNKNOWN,
            "",
            (TARGET_GROW_EFFECT_ID,),
        )
        if exact != expected:
            raise Plan2AddGrowContractError(
                "AddGrow executor shape is outside the exact target"
            )


@dataclass(frozen=True, slots=True)
class Plan2ReviewUp6Trigger:
    trigger_id: str = TARGET_TRIGGER_ID
    phase_types: tuple[str, ...] = (PHASE_NONE,)
    field_status_types: tuple[str, ...] = (REVIEW_UP_FIELD,)
    field_status_values: tuple[int, ...] = (TARGET_REVIEW_THRESHOLD,)

    def __post_init__(self) -> None:
        if (
            self.trigger_id != TARGET_TRIGGER_ID
            or self.phase_types != (PHASE_NONE,)
            or self.field_status_types != (REVIEW_UP_FIELD,)
            or self.field_status_values != (TARGET_REVIEW_THRESHOLD,)
        ):
            raise Plan2AddGrowContractError("ReviewUp6 trigger shape changed")


@dataclass(frozen=True, slots=True)
class Plan2DeckAllSearch:
    search_id: str = TARGET_SEARCH_ID
    card_position_type: str = CARD_POSITION_DECK_ALL
    pick_range_type: str = PICK_ALL

    def __post_init__(self) -> None:
        if (
            self.search_id != TARGET_SEARCH_ID
            or self.card_position_type != CARD_POSITION_DECK_ALL
            or self.pick_range_type != PICK_ALL
        ):
            raise Plan2AddGrowContractError("DeckAll search shape changed")


@dataclass(frozen=True, slots=True)
class Plan2ReviewGrowRow:
    grow_id: str = TARGET_GROW_EFFECT_ID
    effect_type: str = REVIEW_ADD_GROW_TYPE
    value: int = TARGET_GROW_VALUE

    def __post_init__(self) -> None:
        if (
            self.grow_id != TARGET_GROW_EFFECT_ID
            or self.effect_type != REVIEW_ADD_GROW_TYPE
            or self.value != TARGET_GROW_VALUE
        ):
            raise Plan2AddGrowContractError("ReviewAdd grow row shape changed")


@dataclass(frozen=True, slots=True)
class Plan2AddGrowTargetCard:
    card_id: str = TARGET_CARD_ID
    upgrade: int = TARGET_UPGRADE
    name: str = TARGET_CARD_NAME
    stamina: int = 6
    trigger_id: str = TARGET_TRIGGER_ID
    play_effect_ids: tuple[str, ...] = (
        TARGET_REVIEW_MULTIPLE_EFFECT_ID,
        TARGET_ADD_GROW_EFFECT_ID,
        TARGET_PLAYABLE_VALUE_ADD_EFFECT_ID,
    )

    def __post_init__(self) -> None:
        if (
            self.card_id != TARGET_CARD_ID
            or type(self.upgrade) is not int
            or self.upgrade != 0
            or self.name != TARGET_CARD_NAME
            or self.stamina != 6
            or self.trigger_id != TARGET_TRIGGER_ID
            or self.play_effect_ids
            != (
                TARGET_REVIEW_MULTIPLE_EFFECT_ID,
                TARGET_ADD_GROW_EFFECT_ID,
                TARGET_PLAYABLE_VALUE_ADD_EFFECT_ID,
            )
        ):
            raise Plan2AddGrowContractError("target card shape changed")


@dataclass(frozen=True, slots=True)
class Plan2AddGrowProgram:
    card: Plan2AddGrowTargetCard
    trigger: Plan2ReviewUp6Trigger
    search: Plan2DeckAllSearch
    grow: Plan2ReviewGrowRow
    add_grow_effect: Plan2AddGrowMasterEffect
    review_multiple_effect: Plan2ReviewMultipleEffect
    play_effects: tuple[Plan2EndTurnEffect, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.card, Plan2AddGrowTargetCard):
            raise Plan2AddGrowContractError("unknown target card record")
        if not isinstance(self.trigger, Plan2ReviewUp6Trigger):
            raise Plan2AddGrowContractError("unknown ReviewUp6 trigger record")
        if not isinstance(self.search, Plan2DeckAllSearch):
            raise Plan2AddGrowContractError("unknown DeckAll search record")
        if not isinstance(self.grow, Plan2ReviewGrowRow):
            raise Plan2AddGrowContractError("unknown ReviewAdd grow record")
        if not isinstance(self.add_grow_effect, Plan2AddGrowMasterEffect):
            raise Plan2AddGrowContractError("unknown AddGrow effect record")
        if not isinstance(self.review_multiple_effect, Plan2ReviewMultipleEffect):
            raise Plan2AddGrowContractError("unknown ReviewMultiple record")
        effects = tuple(self.play_effects)
        if len(effects) != 3 or any(
            not isinstance(effect, Plan2EndTurnEffect) for effect in effects
        ):
            raise Plan2AddGrowContractError("target play effect list changed")
        expected = (
            (
                TARGET_REVIEW_MULTIPLE_EFFECT_ID,
                REVIEW_MULTIPLE_EFFECT_TYPE,
                1000,
                0,
                0,
                -1,
                ("effect_group-visible-exam_review-000",),
            ),
            (
                TARGET_ADD_GROW_EFFECT_ID,
                ADD_GROW_EFFECT_TYPE,
                0,
                0,
                0,
                0,
                ("effect_group-visible-exam_add_grow_effect-000",),
            ),
            (
                TARGET_PLAYABLE_VALUE_ADD_EFFECT_ID,
                PLAYABLE_VALUE_ADD_EFFECT_TYPE,
                0,
                0,
                1,
                0,
                ("effect_group-visible-exam_playable_value_add-000",),
            ),
        )
        actual = tuple(
            (
                effect.effect_id,
                effect.effect_type,
                effect.value1,
                effect.value2,
                effect.count,
                effect.turn,
                effect.effect_group_ids,
            )
            for effect in effects
        )
        if actual != expected:
            raise Plan2AddGrowContractError(
                "target play effect values/order changed"
            )
        if (
            self.review_multiple_effect.effect_id
            != TARGET_REVIEW_MULTIPLE_EFFECT_ID
            or self.review_multiple_effect.permil != 1000
            or self.review_multiple_effect.turn != -1
        ):
            raise Plan2AddGrowContractError(
                "ReviewMultiple neighbor shape changed"
            )
        object.__setattr__(self, "play_effects", effects)


def _validate_effect_row(
    row: sqlite3.Row,
    *,
    effect_type: str,
    value1: int,
    value2: int,
    count: int,
    turn: int,
    search_id: str,
    pick_range: str,
    grow_ids: list[str],
    groups: list[str],
    description_counts: tuple[int, int],
) -> Plan2EndTurnEffect:
    row_id = str(row["id"])
    for column, expected in (
        ("effect_type", effect_type),
        ("value1", value1),
        ("value2", value2),
        ("effect_count", count),
        ("effect_turn", turn),
        ("status_enchant_id", ""),
        ("chain_effect_id", ""),
    ):
        if row[column] != expected:
            raise Plan2AddGrowContractError(
                f"{row_id}: column {column} must be {expected!r}"
            )
    raw = _json_object(row["raw_json"], row_id)
    _require_exact_keys(raw, _EFFECT_RAW_KEYS, row_id)
    exact = (
        ("id", row_id),
        ("effectType", effect_type),
        ("effectValue1", value1),
        ("effectValue2", value2),
        ("effectCount", count),
        ("effectTurn", turn),
        ("targetProduceCardId", ""),
        ("targetUpgradeCount", 0),
        ("targetExamEffectType", EXAM_EFFECT_UNKNOWN),
        ("produceCardSearchId", search_id),
        ("movePositionType", MOVE_UNKNOWN),
        ("pickRangeType", pick_range),
        ("pickCountReferenceProduceCardSearchId", ""),
        ("pickCountType", PICK_COUNT_UNKNOWN),
        ("pickCountMin", 0),
        ("pickCountMax", 0),
        ("produceCardSearchId2", ""),
        ("pickRangeType2", PICK_UNKNOWN),
        ("pickCountReferenceProduceCardSearchId2", ""),
        ("pickCountType2", PICK_COUNT_UNKNOWN),
        ("pickCountMin2", 0),
        ("pickCountMax2", 0),
        ("chainProduceExamEffectId", ""),
        ("chainProduceExamEffectIds", []),
        ("produceExamStatusEnchantId", ""),
        ("produceCardStatusEnchantId", ""),
        ("produceCardGrowEffectIds", grow_ids),
        ("effectGroupIds", groups),
    )
    for key, expected in exact:
        _require_equal(raw, key, expected, row_id)
    _validate_descriptions(
        raw["produceDescriptions"],
        description_counts[0],
        f"{row_id}.produceDescriptions",
    )
    _validate_descriptions(
        raw["customizeProduceDescriptions"],
        description_counts[1],
        f"{row_id}.customizeProduceDescriptions",
    )
    return Plan2EndTurnEffect(
        effect_id=row_id,
        effect_type=effect_type,
        value1=value1,
        value2=value2,
        count=count,
        turn=turn,
        effect_group_ids=tuple(groups),
    )


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


def _play_entries() -> list[dict[str, object]]:
    return [
        {
            "produceExamTriggerId": "",
            "produceExamEffectId": effect_id,
            "hideIcon": False,
            "isOncePlayEffect": False,
        }
        for effect_id in (
            TARGET_REVIEW_MULTIPLE_EFFECT_ID,
            TARGET_ADD_GROW_EFFECT_ID,
            TARGET_PLAYABLE_VALUE_ADD_EFFECT_ID,
        )
    ]


def _validate_card(row: sqlite3.Row) -> Plan2AddGrowTargetCard:
    row_id = f"{TARGET_CARD_ID}#0"
    entries = _play_entries()
    for column, expected in (
        ("id", TARGET_CARD_ID),
        ("upgrade_count", 0),
        ("name", TARGET_CARD_NAME),
        ("plan_type", PLAN2),
        ("category", MENTAL_SKILL),
        ("stamina", 6),
        ("cost_type", UNKNOWN_COST),
        ("cost_value", 0),
        ("play_trigger_id", TARGET_TRIGGER_ID),
        ("move_position_type", MOVE_LOST),
    ):
        if row[column] != expected:
            raise Plan2AddGrowContractError(
                f"{row_id}: column {column} must be {expected!r}"
            )
    if _json_array(row["play_effects_json"], f"{row_id}.play_effects") != entries:
        raise Plan2AddGrowContractError(f"{row_id}: play effect order changed")
    raw = _json_object(row["raw_json"], row_id)
    _require_exact_keys(raw, _CARD_RAW_KEYS, row_id)
    exact = (
        ("id", TARGET_CARD_ID),
        ("upgradeCount", 0),
        ("name", TARGET_CARD_NAME),
        ("assetId", "img_general_skillcard_men-100_008"),
        ("isCharacterAsset", True),
        ("voiceAssetId", ""),
        ("rarity", "ProduceCardRarity_Legend"),
        ("planType", PLAN2),
        ("category", MENTAL_SKILL),
        ("stamina", 6),
        ("forceStamina", 0),
        ("costType", UNKNOWN_COST),
        ("costValue", 0),
        ("playProduceExamTriggerId", TARGET_TRIGGER_ID),
        ("playEffects", entries),
        ("playMovePositionType", MOVE_LOST),
        ("moveEffectTriggerType", "ProduceCardMoveEffectTriggerType_Unknown"),
        ("moveProduceExamEffectIds", []),
        ("isEndTurnLost", False),
        ("isInitial", False),
        ("isRestrict", False),
        ("produceCardStatusEnchantId", ""),
        ("searchTag", ""),
        ("libraryHidden", False),
        ("noDeckDuplication", True),
        ("isReward", False),
        ("unlockProducerLevel", 0),
        ("rentalUnlockProducerLevel", 0),
        ("evaluation", 0),
        ("originIdolCardId", ""),
        ("originSupportCardId", ""),
        ("isInitialDeckProduceCard", False),
        (
            "effectGroupIds",
            [
                "effect_group-visible-exam_playable_value_add-000",
                "effect_group-visible-exam_review-000",
                "effect_group-visible-exam_add_grow_effect-000",
            ],
        ),
        ("produceCardCustomizeIds", []),
        ("maxCustomizeCount", 0),
        ("isConversion", False),
        ("moveProduceExamTriggerIds", []),
        ("originCharacterId", ""),
        ("originPrimaStellaIdolCardId", ""),
        ("viewStartTime", "0"),
        ("isLimited", False),
        ("order", "92020000680008"),
    )
    for key, expected in exact:
        _require_equal(raw, key, expected, row_id)
    descriptions = _json_array(row["descriptions_json"], f"{row_id}.descriptions")
    if raw["produceDescriptions"] != descriptions:
        raise Plan2AddGrowContractError(
            f"{row_id}: description column/raw mismatch"
        )
    _validate_descriptions(descriptions, 28, f"{row_id}.produceDescriptions")
    return Plan2AddGrowTargetCard()


_TRIGGER_RAW_KEYS = {
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


def _validate_trigger(row: sqlite3.Row) -> Plan2ReviewUp6Trigger:
    row_id = TARGET_TRIGGER_ID
    normalized = (
        ("phase_types_json", [PHASE_NONE]),
        ("phase_values_json", []),
        ("field_status_check_types_json", []),
        ("field_status_types_json", [REVIEW_UP_FIELD]),
        ("field_status_values_json", [TARGET_REVIEW_THRESHOLD]),
        ("field_status_produce_card_search_ids_json", []),
        ("produce_card_search_id", ""),
        ("upper_search_count", 0),
        ("lower_search_count", 0),
        ("card_move_position_type", MOVE_UNKNOWN),
        ("effect_types_json", []),
        ("lesson_type", LESSON_UNKNOWN),
    )
    for column, expected in normalized:
        actual = row[column]
        if column.endswith("_json"):
            actual = _json_array(actual, f"{row_id}.{column}")
        if actual != expected:
            raise Plan2AddGrowContractError(
                f"{row_id}: column {column} must be {expected!r}"
            )
    raw = _json_object(row["raw_json"], row_id)
    _require_exact_keys(raw, _TRIGGER_RAW_KEYS, row_id)
    exact = (
        ("id", row_id),
        ("phaseTypes", [PHASE_NONE]),
        ("phaseValues", []),
        ("fieldStatusCheckTypes", []),
        ("fieldStatusTypes", [REVIEW_UP_FIELD]),
        ("fieldStatusValues", [TARGET_REVIEW_THRESHOLD]),
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
    for key in (
        "produceDescriptions",
        "playProduceDescriptions",
        "playEffectProduceDescriptions",
    ):
        _validate_descriptions(raw[key], 2, f"{row_id}.{key}")
    return Plan2ReviewUp6Trigger()


_SEARCH_RAW_KEYS = {
    "id",
    "cardRarities",
    "produceCardIds",
    "upgradeCounts",
    "planType",
    "cardCategories",
    "cardStatusType",
    "orderType",
    "cardPositionType",
    "cardSearchTag",
    "produceCardRandomPoolId",
    "limitCount",
    "staminaMinMaxType",
    "staminaMin",
    "staminaMax",
    "examEffectType",
    "effectGroupIds",
    "isSelf",
    "produceDescriptions",
    "produceCardPoolId",
    "costType",
    "isCustomized",
}


def _validate_search(row: sqlite3.Row) -> Plan2DeckAllSearch:
    row_id = TARGET_SEARCH_ID
    normalized = (
        ("card_rarities_json", []),
        ("produce_card_ids_json", []),
        ("upgrade_counts_json", []),
        ("plan_type", "ProducePlanType_Unknown"),
        ("card_categories_json", []),
        ("card_status_type", CARD_STATUS_UNKNOWN),
        ("order_type", CARD_ORDER_UNKNOWN),
        ("card_position_type", CARD_POSITION_DECK_ALL),
        ("card_search_tag", ""),
        ("produce_card_random_pool_id", ""),
        ("limit_count", 0),
        ("stamina_min_max_type", MIN_MAX_UNKNOWN),
        ("stamina_min", 0),
        ("stamina_max", 0),
        ("exam_effect_type", EXAM_EFFECT_UNKNOWN),
        ("effect_group_ids_json", []),
        ("is_self", 0),
        ("produce_card_pool_id", ""),
        ("cost_type", UNKNOWN_COST),
        ("is_customized", 0),
    )
    for column, expected in normalized:
        actual = row[column]
        if column.endswith("_json"):
            actual = _json_array(actual, f"{row_id}.{column}")
        if actual != expected:
            raise Plan2AddGrowContractError(
                f"{row_id}: column {column} must be {expected!r}"
            )
    raw = _json_object(row["raw_json"], row_id)
    _require_exact_keys(raw, _SEARCH_RAW_KEYS, row_id)
    exact = (
        ("id", row_id),
        ("cardRarities", []),
        ("produceCardIds", []),
        ("upgradeCounts", []),
        ("planType", "ProducePlanType_Unknown"),
        ("cardCategories", []),
        ("cardStatusType", CARD_STATUS_UNKNOWN),
        ("orderType", CARD_ORDER_UNKNOWN),
        ("cardPositionType", CARD_POSITION_DECK_ALL),
        ("cardSearchTag", ""),
        ("produceCardRandomPoolId", ""),
        ("limitCount", 0),
        ("staminaMinMaxType", MIN_MAX_UNKNOWN),
        ("staminaMin", 0),
        ("staminaMax", 0),
        ("examEffectType", EXAM_EFFECT_UNKNOWN),
        ("effectGroupIds", []),
        ("isSelf", False),
        ("produceCardPoolId", ""),
        ("costType", UNKNOWN_COST),
        ("isCustomized", False),
    )
    for key, expected in exact:
        _require_equal(raw, key, expected, row_id)
    _validate_descriptions(
        raw["produceDescriptions"], 1, f"{row_id}.produceDescriptions"
    )
    return Plan2DeckAllSearch()


_GROW_RAW_KEYS = {
    "id",
    "effectType",
    "costType",
    "value",
    "playProduceExamTriggerId",
    "playEffectProduceExamTriggerId",
    "targetPlayEffectProduceExamTriggerIds",
    "playProduceExamEffectId",
    "targetPlayProduceExamEffectIds",
    "produceCardStatusEnchantId",
    "playMovePositionType",
    "effectGroupIds",
}


def _load_grow_row(master_dir: Path) -> Plan2ReviewGrowRow:
    path = Path(master_dir) / "ProduceCardGrowEffect.yaml"
    try:
        raw_rows = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise Plan2AddGrowContractError("cannot read grow Master") from error
    if not isinstance(raw_rows, list):
        raise Plan2AddGrowContractError("grow Master must be a list")
    rows = [
        row
        for row in raw_rows
        if isinstance(row, dict) and row.get("id") == TARGET_GROW_EFFECT_ID
    ]
    if len(rows) != 1:
        raise Plan2AddGrowContractError("target grow row is missing/duplicated")
    raw = rows[0]
    _require_exact_keys(raw, _GROW_RAW_KEYS, TARGET_GROW_EFFECT_ID)
    exact = (
        ("id", TARGET_GROW_EFFECT_ID),
        ("effectType", REVIEW_ADD_GROW_TYPE),
        ("costType", UNKNOWN_COST),
        ("value", TARGET_GROW_VALUE),
        ("playProduceExamTriggerId", ""),
        ("playEffectProduceExamTriggerId", ""),
        ("targetPlayEffectProduceExamTriggerIds", []),
        ("playProduceExamEffectId", ""),
        ("targetPlayProduceExamEffectIds", []),
        ("produceCardStatusEnchantId", ""),
        ("playMovePositionType", MOVE_UNKNOWN),
        ("effectGroupIds", []),
    )
    for key, expected in exact:
        _require_equal(raw, key, expected, TARGET_GROW_EFFECT_ID)
    return Plan2ReviewGrowRow()


def load_plan2_add_grow_program(
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan2AddGrowProgram:
    """Load the exact one-card program and fail closed on any Master drift."""

    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        cards = connection.execute(
            "SELECT * FROM card WHERE id = ? ORDER BY upgrade_count",
            (TARGET_CARD_ID,),
        ).fetchall()
        effect_rows = connection.execute(
            "SELECT * FROM effect WHERE id IN (?, ?, ?)",
            (
                TARGET_REVIEW_MULTIPLE_EFFECT_ID,
                TARGET_ADD_GROW_EFFECT_ID,
                TARGET_PLAYABLE_VALUE_ADD_EFFECT_ID,
            ),
        ).fetchall()
        trigger = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?",
            (TARGET_TRIGGER_ID,),
        ).fetchone()
        search = connection.execute(
            "SELECT * FROM produce_card_search WHERE id = ?",
            (TARGET_SEARCH_ID,),
        ).fetchone()
    if len(cards) != 1 or trigger is None or search is None:
        raise Plan2AddGrowContractError("target card/trigger/search slice is incomplete")
    rows = {str(row["id"]): row for row in effect_rows}
    if set(rows) != {
        TARGET_REVIEW_MULTIPLE_EFFECT_ID,
        TARGET_ADD_GROW_EFFECT_ID,
        TARGET_PLAYABLE_VALUE_ADD_EFFECT_ID,
    }:
        raise Plan2AddGrowContractError("target play effect slice is incomplete")
    card = _validate_card(cards[0])
    review = _validate_effect_row(
        rows[TARGET_REVIEW_MULTIPLE_EFFECT_ID],
        effect_type=REVIEW_MULTIPLE_EFFECT_TYPE,
        value1=1000,
        value2=0,
        count=0,
        turn=-1,
        search_id="",
        pick_range=PICK_UNKNOWN,
        grow_ids=[],
        groups=["effect_group-visible-exam_review-000"],
        description_counts=(4, 5),
    )
    add_grow = _validate_effect_row(
        rows[TARGET_ADD_GROW_EFFECT_ID],
        effect_type=ADD_GROW_EFFECT_TYPE,
        value1=0,
        value2=0,
        count=0,
        turn=0,
        search_id=TARGET_SEARCH_ID,
        pick_range=PICK_ALL,
        grow_ids=[TARGET_GROW_EFFECT_ID],
        groups=["effect_group-visible-exam_add_grow_effect-000"],
        description_counts=(3, 4),
    )
    playable = _validate_effect_row(
        rows[TARGET_PLAYABLE_VALUE_ADD_EFFECT_ID],
        effect_type=PLAYABLE_VALUE_ADD_EFFECT_TYPE,
        value1=0,
        value2=0,
        count=1,
        turn=0,
        search_id="",
        pick_range=PICK_UNKNOWN,
        grow_ids=[],
        groups=["effect_group-visible-exam_playable_value_add-000"],
        description_counts=(4, 5),
    )
    try:
        review_primitive = load_plan2_review_multiple_effect(
            TARGET_REVIEW_MULTIPLE_EFFECT_ID, database
        )
    except (Plan2ReviewMultipleContractError, KeyError) as error:
        raise Plan2AddGrowContractError(
            "ReviewMultiple neighbor no longer matches its proven primitive"
        ) from error
    return Plan2AddGrowProgram(
        card=card,
        trigger=_validate_trigger(trigger),
        search=_validate_search(search),
        grow=_load_grow_row(Path(master_dir)),
        add_grow_effect=Plan2AddGrowMasterEffect(),
        review_multiple_effect=review_primitive,
        play_effects=(review, add_grow, playable),
    )


@dataclass(frozen=True, slots=True)
class Plan2ReviewGrowAggregate:
    effect_type: Literal[
        "ProduceCardGrowEffectType_ReviewAdd",
        "ProduceCardGrowEffectType_ReviewReduce",
    ]
    value: int

    def __post_init__(self) -> None:
        if self.effect_type not in {REVIEW_ADD_GROW_TYPE, REVIEW_REDUCE_GROW_TYPE}:
            raise Plan2AddGrowContractError("unknown Review grow aggregate type")
        _plain_i32(self.value, "review grow aggregate value")

    @property
    def signed_value(self) -> int:
        if self.effect_type == REVIEW_ADD_GROW_TYPE:
            return self.value
        return _i32(-self.value)


@dataclass(frozen=True, slots=True)
class Plan2AddGrowCardInstance:
    guid: str
    card_id: str
    play_effects: tuple[Plan2EndTurnEffect, ...]
    review_grow: Plan2ReviewGrowAggregate | None = None
    applied_grow_effect_ids: tuple[str, ...] = ()
    cache_reset_count: int = 0
    on_change_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.guid, str) or not self.guid:
            raise TypeError("guid must be non-empty text")
        if not isinstance(self.card_id, str) or not self.card_id:
            raise TypeError("card_id must be non-empty text")
        effects = tuple(self.play_effects)
        if any(not isinstance(effect, Plan2EndTurnEffect) for effect in effects):
            raise TypeError("play_effects must contain Plan2EndTurnEffect")
        object.__setattr__(self, "play_effects", effects)
        if self.review_grow is not None and not isinstance(
            self.review_grow, Plan2ReviewGrowAggregate
        ):
            raise TypeError("review_grow must be a Review aggregate or None")
        ids = tuple(self.applied_grow_effect_ids)
        if any(not isinstance(value, str) or not value for value in ids):
            raise TypeError("applied_grow_effect_ids must contain text")
        object.__setattr__(self, "applied_grow_effect_ids", ids)
        for label in ("cache_reset_count", "on_change_count"):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError(f"{label} must be a non-negative integer")

    @property
    def has_direct_review_effect(self) -> bool:
        return any(
            effect.effect_type == REVIEW_EFFECT_TYPE for effect in self.play_effects
        )

    @property
    def effective_review_values(self) -> tuple[int, ...]:
        values: list[int] = []
        for effect in self.play_effects:
            if effect.effect_type != REVIEW_EFFECT_TYPE:
                continue
            if self.review_grow is None:
                values.append(effect.value1)
            else:
                values.append(
                    max(1, _i32(effect.value1 + self.review_grow.signed_value))
                )
        return tuple(values)


@dataclass(frozen=True, slots=True)
class Plan2DeckAllCardLocation:
    zone: str
    card_index: int
    deck_list_index: int | None
    card: Plan2AddGrowCardInstance


@dataclass(frozen=True, slots=True)
class Plan2AddGrowDeckAllState:
    hand: tuple[Plan2AddGrowCardInstance, ...] = ()
    deck: tuple[Plan2AddGrowCardInstance, ...] = ()
    grave: tuple[Plan2AddGrowCardInstance, ...] = ()
    lost: tuple[Plan2AddGrowCardInstance, ...] = ()
    hold: tuple[Plan2AddGrowCardInstance, ...] = ()
    playing: Plan2AddGrowCardInstance | None = None
    future_decks: tuple[tuple[Plan2AddGrowCardInstance, ...], ...] = ()
    past_decks: tuple[tuple[Plan2AddGrowCardInstance, ...], ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("hand", "deck", "grave", "lost", "hold"):
            cards = tuple(getattr(self, field_name))
            if any(not isinstance(card, Plan2AddGrowCardInstance) for card in cards):
                raise TypeError(f"{field_name} must contain card instances")
            object.__setattr__(self, field_name, cards)
        if self.playing is not None and not isinstance(
            self.playing, Plan2AddGrowCardInstance
        ):
            raise TypeError("playing must be a card instance or None")
        for field_name in ("future_decks", "past_decks"):
            decks = tuple(tuple(deck) for deck in getattr(self, field_name))
            if any(
                not isinstance(card, Plan2AddGrowCardInstance)
                for deck in decks
                for card in deck
            ):
                raise TypeError(f"{field_name} must contain card-instance decks")
            object.__setattr__(self, field_name, decks)
        guids = [location.card.guid for location in self.native_locations]
        if len(guids) != len(set(guids)):
            raise Plan2AddGrowContractError("DeckAll card GUIDs must be unique")

    @property
    def native_locations(self) -> tuple[Plan2DeckAllCardLocation, ...]:
        rows: list[Plan2DeckAllCardLocation] = []
        for zone, cards in (
            ("Hand", self.hand),
            ("Deck", self.deck),
            ("Grave", self.grave),
            ("Lost", self.lost),
            ("Hold", self.hold),
        ):
            rows.extend(
                Plan2DeckAllCardLocation(zone, index, None, card)
                for index, card in enumerate(cards)
            )
        if self.playing is not None:
            rows.append(Plan2DeckAllCardLocation("Playing", 0, None, self.playing))
        for zone, decks in (("FutureDecks", self.future_decks), ("PastDecks", self.past_decks)):
            for deck_index, cards in enumerate(decks):
                rows.extend(
                    Plan2DeckAllCardLocation(zone, index, deck_index, card)
                    for index, card in enumerate(cards)
                )
        return tuple(rows)


def _replace_card_by_guid(
    state: Plan2AddGrowDeckAllState,
    guid: str,
    card: Plan2AddGrowCardInstance,
) -> Plan2AddGrowDeckAllState:
    def replace_cards(
        values: tuple[Plan2AddGrowCardInstance, ...]
    ) -> tuple[Plan2AddGrowCardInstance, ...]:
        return tuple(card if value.guid == guid else value for value in values)

    return replace(
        state,
        hand=replace_cards(state.hand),
        deck=replace_cards(state.deck),
        grave=replace_cards(state.grave),
        lost=replace_cards(state.lost),
        hold=replace_cards(state.hold),
        playing=(
            card if state.playing is not None and state.playing.guid == guid
            else state.playing
        ),
        future_decks=tuple(replace_cards(deck) for deck in state.future_decks),
        past_decks=tuple(replace_cards(deck) for deck in state.past_decks),
    )


@dataclass(frozen=True, slots=True)
class Plan2CapturedReviewGrowGroup:
    guid: str
    grow_rows: tuple[Plan2ReviewGrowRow, ...]
    direct_effect_types_snapshot: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.guid, str) or not self.guid:
            raise TypeError("captured guid must be non-empty text")
        grow_rows = tuple(self.grow_rows)
        effect_types = tuple(self.direct_effect_types_snapshot)
        if grow_rows != (Plan2ReviewGrowRow(),):
            raise Plan2AddGrowContractError("captured grow row/order changed")
        if any(not isinstance(value, str) or not value for value in effect_types):
            raise TypeError("captured effect types must contain non-empty text")
        if REVIEW_EFFECT_TYPE not in effect_types:
            raise Plan2AddGrowContractError(
                "captured ReviewAdd target lacked a direct ExamReview effect"
            )
        object.__setattr__(self, "grow_rows", grow_rows)
        object.__setattr__(self, "direct_effect_types_snapshot", effect_types)


@dataclass(frozen=True, slots=True)
class Plan2AddGrowCapture:
    state_at_search: Plan2AddGrowDeckAllState
    groups: tuple[Plan2CapturedReviewGrowGroup, ...]
    event_trace: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.state_at_search, Plan2AddGrowDeckAllState):
            raise TypeError("state_at_search must be a DeckAll state")
        groups = tuple(self.groups)
        if any(not isinstance(group, Plan2CapturedReviewGrowGroup) for group in groups):
            raise TypeError("groups must contain captured Review grow groups")
        if len({group.guid for group in groups}) != len(groups):
            raise Plan2AddGrowContractError("captured GUID groups must be unique")
        trace = tuple(self.event_trace)
        if any(not isinstance(value, str) or not value for value in trace):
            raise TypeError("event_trace must contain non-empty text")
        object.__setattr__(self, "groups", groups)
        object.__setattr__(self, "event_trace", trace)


def capture_plan2_add_grow_groups(
    state: Plan2AddGrowDeckAllState,
    program: Plan2AddGrowProgram,
) -> Plan2AddGrowCapture:
    """Run DeckAll search/validation and capture ordered GUID affect groups."""

    if not isinstance(state, Plan2AddGrowDeckAllState):
        raise TypeError("state must be Plan2AddGrowDeckAllState")
    if not isinstance(program, Plan2AddGrowProgram):
        raise TypeError("program must be Plan2AddGrowProgram")
    groups: list[Plan2CapturedReviewGrowGroup] = []
    trace: list[str] = ["AddGrow:GetSearchCardList:DeckAll:All"]
    for location in state.native_locations:
        card = location.card
        valid = card.has_direct_review_effect
        trace.append(
            f"AddGrow:IsValidCardGrowEffect:{card.guid}:ReviewAdd:{str(valid).lower()}"
        )
        if not valid:
            continue
        groups.append(
            Plan2CapturedReviewGrowGroup(
                guid=card.guid,
                grow_rows=(program.grow,),
                direct_effect_types_snapshot=tuple(
                    effect.effect_type for effect in card.play_effects
                ),
            )
        )
        trace.append(f"AddGrow:capture-guid:{card.guid}:ReviewAdd+6")
    return Plan2AddGrowCapture(
        state_at_search=state,
        groups=tuple(groups),
        event_trace=tuple(trace),
    )


def _add_review_grow(
    aggregate: Plan2ReviewGrowAggregate | None,
    value: int,
) -> Plan2ReviewGrowAggregate | None:
    value = _plain_i32(value, "ReviewAdd value")
    if aggregate is None:
        return Plan2ReviewGrowAggregate(REVIEW_ADD_GROW_TYPE, value)
    if aggregate.effect_type == REVIEW_ADD_GROW_TYPE:
        return replace(aggregate, value=_i32(aggregate.value + value))
    residual = _i32(aggregate.value - value)
    if residual == 0:
        return None
    if residual > 0:
        return replace(aggregate, value=residual)
    return Plan2ReviewGrowAggregate(REVIEW_ADD_GROW_TYPE, _i32(-residual))


@dataclass(frozen=True, slots=True)
class Plan2AddGrowMutation:
    sequence: int
    guid: str
    zone: str
    card_index: int
    deck_list_index: int | None
    aggregate_before: Plan2ReviewGrowAggregate | None
    aggregate_after: Plan2ReviewGrowAggregate | None
    review_values_before: tuple[int, ...]
    review_values_after: tuple[int, ...]
    grow_ids_before: tuple[str, ...]
    grow_ids_after: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2AddGrowExecution:
    before: Plan2AddGrowDeckAllState
    after: Plan2AddGrowDeckAllState
    capture: Plan2AddGrowCapture
    mutations: tuple[Plan2AddGrowMutation, ...]
    unmatched_guids: tuple[str, ...]
    event_trace: tuple[str, ...]


def execute_captured_plan2_add_grow_groups(
    state: Plan2AddGrowDeckAllState,
    capture: Plan2AddGrowCapture,
) -> Plan2AddGrowExecution:
    """Rematch captured GUIDs and apply the exact per-card ReviewAdd mutation."""

    if not isinstance(state, Plan2AddGrowDeckAllState):
        raise TypeError("state must be Plan2AddGrowDeckAllState")
    if not isinstance(capture, Plan2AddGrowCapture):
        raise TypeError("capture must be Plan2AddGrowCapture")
    current = state
    mutations: list[Plan2AddGrowMutation] = []
    unmatched: list[str] = []
    trace: list[str] = ["CardPhaseStatusAffect:CreateCardPositionDataList:DeckAll"]
    for group in capture.groups:
        locations = {
            location.card.guid: location for location in current.native_locations
        }
        location = locations.get(group.guid)
        if location is None:
            unmatched.append(group.guid)
            trace.append(f"CardPhaseStatusAffect:guid-unmatched:{group.guid}")
            continue
        before_card = location.card
        after_aggregate = before_card.review_grow
        for grow in group.grow_rows:
            after_aggregate = _add_review_grow(after_aggregate, grow.value)
        after_card = replace(
            before_card,
            review_grow=after_aggregate,
            applied_grow_effect_ids=(
                *before_card.applied_grow_effect_ids,
                *(grow.grow_id for grow in group.grow_rows),
            ),
            cache_reset_count=before_card.cache_reset_count + len(group.grow_rows),
            on_change_count=before_card.on_change_count + len(group.grow_rows),
        )
        current = _replace_card_by_guid(current, group.guid, after_card)
        mutations.append(
            Plan2AddGrowMutation(
                sequence=len(mutations),
                guid=group.guid,
                zone=location.zone,
                card_index=location.card_index,
                deck_list_index=location.deck_list_index,
                aggregate_before=before_card.review_grow,
                aggregate_after=after_card.review_grow,
                review_values_before=before_card.effective_review_values,
                review_values_after=after_card.effective_review_values,
                grow_ids_before=before_card.applied_grow_effect_ids,
                grow_ids_after=after_card.applied_grow_effect_ids,
            )
        )
        trace.extend(
            (
                f"CardPhaseStatusAffect:guid-match:{group.guid}:{location.zone}",
                f"ExamCardData:AddGrowEffect:{group.guid}:ReviewAdd+6",
                f"ExamCardData:append-grow-id:{group.guid}:{TARGET_GROW_EFFECT_ID}",
                f"ExamCardData:ResetGrowEffectCacheValue:{group.guid}",
                f"ExamCardData:OnChangeEffectData:{group.guid}",
            )
        )
    return Plan2AddGrowExecution(
        before=state,
        after=current,
        capture=capture,
        mutations=tuple(mutations),
        unmatched_guids=tuple(unmatched),
        event_trace=tuple(trace),
    )


@dataclass(frozen=True, slots=True)
class Plan2ReviewUp6Snapshot:
    current_review: int
    threshold: int
    admitted: bool
    stage: str = "pre-play-field-gate"

    def __post_init__(self) -> None:
        _plain_i32(self.current_review, "current_review")
        if self.threshold != TARGET_REVIEW_THRESHOLD:
            raise Plan2AddGrowContractError("ReviewUp threshold changed")
        if type(self.admitted) is not bool:
            raise TypeError("admitted must be boolean")
        if self.admitted != (self.current_review >= self.threshold):
            raise Plan2AddGrowContractError("ReviewUp6 admission is inconsistent")
        if self.stage != "pre-play-field-gate":
            raise Plan2AddGrowContractError("ReviewUp6 snapshot stage changed")


def snapshot_plan2_review_up6(current_review: int) -> Plan2ReviewUp6Snapshot:
    current_review = _plain_i32(current_review, "current_review")
    return Plan2ReviewUp6Snapshot(
        current_review=current_review,
        threshold=TARGET_REVIEW_THRESHOLD,
        admitted=current_review >= TARGET_REVIEW_THRESHOLD,
    )


@dataclass(frozen=True, slots=True)
class Plan2TargetAddGrowStageTransition:
    scalar_before: Plan2State
    scalar_after_review_multiple: Plan2State
    deck_before: Plan2AddGrowDeckAllState
    deck_after_add_grow: Plan2AddGrowDeckAllState
    trigger_snapshot: Plan2ReviewUp6Snapshot
    review_multiple_transition: Plan2ReviewMultipleTransition | None
    add_grow_capture: Plan2AddGrowCapture | None
    add_grow_execution: Plan2AddGrowExecution | None
    downstream_playable_value_add: Plan2EndTurnEffect | None
    event_trace: tuple[str, ...]


def simulate_plan2_target_add_grow_stage(
    scalar_state: Plan2State,
    deck_state: Plan2AddGrowDeckAllState,
    program: Plan2AddGrowProgram,
) -> Plan2TargetAddGrowStageTransition:
    """Run the target card's gate/neighbors while owning only AddGrowEffect.

    ReviewMultiple is delegated to its existing proven primitive.  The final
    PlayableValueAdd remains an explicit downstream hand-off to its existing
    Plan2 owner.
    """

    if not isinstance(scalar_state, Plan2State):
        raise TypeError("scalar_state must be Plan2State")
    if not isinstance(deck_state, Plan2AddGrowDeckAllState):
        raise TypeError("deck_state must be Plan2AddGrowDeckAllState")
    if not isinstance(program, Plan2AddGrowProgram):
        raise TypeError("program must be Plan2AddGrowProgram")
    playing = deck_state.playing
    if (
        playing is None
        or playing.card_id != TARGET_CARD_ID
        or playing.play_effects != program.play_effects
    ):
        raise Plan2AddGrowContractError(
            "the exact target card must be the current Playing card"
        )
    gate = snapshot_plan2_review_up6(scalar_state.review)
    trace = [
        f"card-trigger:ReviewUp6:snapshot={gate.current_review}:admitted={str(gate.admitted).lower()}"
    ]
    if not gate.admitted:
        return Plan2TargetAddGrowStageTransition(
            scalar_before=scalar_state,
            scalar_after_review_multiple=scalar_state,
            deck_before=deck_state,
            deck_after_add_grow=deck_state,
            trigger_snapshot=gate,
            review_multiple_transition=None,
            add_grow_capture=None,
            add_grow_execution=None,
            downstream_playable_value_add=None,
            event_trace=tuple(trace + ["card-play:rejected-before-effect-0"]),
        )
    review_transition = simulate_review_multiple(
        scalar_state, program.review_multiple_effect
    )
    trace.append("card-play:effect:0:ReviewMultiple1000-inf:executed")
    capture = capture_plan2_add_grow_groups(deck_state, program)
    execution = execute_captured_plan2_add_grow_groups(deck_state, capture)
    trace.extend(capture.event_trace)
    trace.extend(execution.event_trace)
    trace.append("card-play:effect:1:ExamAddGrowEffect:executed")
    playable = program.play_effects[2]
    trace.append("card-play:effect:2:PlayableValueAdd1:downstream-handoff")
    return Plan2TargetAddGrowStageTransition(
        scalar_before=scalar_state,
        scalar_after_review_multiple=review_transition.after,
        deck_before=deck_state,
        deck_after_add_grow=execution.after,
        trigger_snapshot=gate,
        review_multiple_transition=review_transition,
        add_grow_capture=capture,
        add_grow_execution=execution,
        downstream_playable_value_add=playable,
        event_trace=tuple(trace),
    )


__all__ = [
    "ADD_GROW_EFFECT_TYPE",
    "ANDROID_PLAN2_ADD_GROW_EVIDENCE",
    "FORMAL_AFFECTED_VERSION_REFS",
    "FORMAL_CO_BLOCKED_VERSION_REFS",
    "FORMAL_DIRECT_VERSION_REFS",
    "PC_PLAN2_ADD_GROW_METADATA",
    "Plan2AddGrowCapture",
    "Plan2AddGrowCardInstance",
    "Plan2AddGrowContractError",
    "Plan2AddGrowDeckAllState",
    "Plan2AddGrowExecution",
    "Plan2AddGrowMasterEffect",
    "Plan2AddGrowMutation",
    "Plan2AddGrowProgram",
    "Plan2AddGrowTargetCard",
    "Plan2CapturedReviewGrowGroup",
    "Plan2DeckAllCardLocation",
    "Plan2DeckAllSearch",
    "Plan2ReviewGrowAggregate",
    "Plan2ReviewGrowRow",
    "Plan2ReviewUp6Snapshot",
    "Plan2ReviewUp6Trigger",
    "Plan2TargetAddGrowStageTransition",
    "REVIEW_ADD_GROW_TYPE",
    "REVIEW_EFFECT_TYPE",
    "REVIEW_REDUCE_GROW_TYPE",
    "TARGET_ADD_GROW_EFFECT_ID",
    "TARGET_CARD_ID",
    "TARGET_GROW_EFFECT_ID",
    "TARGET_PLAYABLE_VALUE_ADD_EFFECT_ID",
    "TARGET_REVIEW_MULTIPLE_EFFECT_ID",
    "capture_plan2_add_grow_groups",
    "execute_captured_plan2_add_grow_groups",
    "load_plan2_add_grow_program",
    "simulate_plan2_target_add_grow_stage",
    "snapshot_plan2_review_up6",
]
