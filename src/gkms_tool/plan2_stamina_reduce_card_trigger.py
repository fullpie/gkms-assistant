"""Bounded Plan2 implementation of ``e_trigger-exam_stamina_reduce_card``.

This module is intentionally standalone.  It does not register anything in
the shared Plan2 core and it does not execute the child scalar effect.  The
only Master slice admitted here is ``p_card-02-men-100_011#0`` (最強パフォーマー).

Android v3.2.3 places the target phase in ``ExamSequence.ConsumeCardCost``.
The event is therefore a post-payment, post-block-split observation of an
actual stamina loss, before the card's direct effects run.  A phase-20 direct
stamina effect, a failed/bypassed payment, a zero/no-delta payment, and an
unknown wrapper or source are not silently treated as this phase.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Mapping

from .master_db import DEFAULT_DATABASE
from .plan2_state import Plan2CardPlayEffect


TARGET_CARD_ID = "p_card-02-men-100_011"
TARGET_CARD_UPGRADE = 0
TARGET_CARD_UPGRADES = (TARGET_CARD_UPGRADE,)
TARGET_CARD_VERSION_REF = f"{TARGET_CARD_ID}#{TARGET_CARD_UPGRADE}"
TARGET_CARD_NAME = "最強パフォーマー"
TARGET_CARD_GATE_ID = "e_trigger-none-card_play_aggressive_up-9"

TARGET_TRIGGER_ID = "e_trigger-exam_stamina_reduce_card"
TARGET_PHASE_TYPE = "ProduceExamPhaseType_ExamStaminaReduceCard"
TARGET_PHASE_VALUE = 24

TARGET_WRAPPER_EFFECT_ID = (
    "e_effect-exam_status_enchant-inf-"
    "enchant-p_card-02-men-100_011-enc01"
)
TARGET_STATUS_ENCHANT_ID = "enchant-p_card-02-men-100_011-enc01"
TARGET_CHILD_EFFECT_ID = "e_effect-exam_block_add_multiple_aggressive-0004-1500-01"

STATUS_ENCHANT_EFFECT_TYPE = "ProduceExamEffectType_ExamStatusEnchant"
CHILD_EFFECT_TYPE = "ProduceExamEffectType_ExamBlockAddMultipleAggressive"
TARGET_CHILD_EFFECT_TYPE = CHILD_EFFECT_TYPE
PLAN2 = "ProducePlanType_Plan2"
MENTAL_SKILL = "ProduceCardCategory_MentalSkill"
UNKNOWN_COST = "ExamCostType_Unknown"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
MOVE_LOST = "ProduceCardMovePositionType_Lost"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
PERMANENT_TURN = -1
UNLIMITED_COUNT = -1

TARGET_CHILD_VALUE1 = 4
TARGET_CHILD_VALUE2 = 1500
TARGET_CHILD_COUNT = 1
TARGET_CHILD_TURN = 0
TARGET_WRAPPER_COUNT = 0
TARGET_WRAPPER_TURN = -1
TARGET_WRAPPER_GROUPS = (
    "effect_group-visible-exam_block-000",
    "effect_group-visible-exam_status_enchant-000",
)
TARGET_CHILD_GROUPS = ("effect_group-visible-exam_block-000",)
TARGET_CHILD_EFFECT_GROUP_IDS = TARGET_CHILD_GROUPS
TARGET_CARD_EFFECT_GROUPS = (
    "effect_group-visible-exam_block-000",
    "effect_group-visible-exam_status_enchant-000",
    "effect_group-visible-exam_playable_value_add-000",
    "effect_group-visible-exam_card_play_aggressive-000",
)
TARGET_WRAPPER_EFFECT_GROUP_IDS = TARGET_WRAPPER_GROUPS
TARGET_PLAY_EFFECTS = [
    {
        "produceExamEffectId": "e_effect-exam_playable_value_add-01",
        "produceExamTriggerId": "",
        "hideIcon": False,
        "isOncePlayEffect": False,
    },
    {
        "produceExamEffectId": "e_effect-exam_card_play_aggressive-0005",
        "produceExamTriggerId": "",
        "hideIcon": False,
        "isOncePlayEffect": False,
    },
    {
        "produceExamEffectId": TARGET_WRAPPER_EFFECT_ID,
        "produceExamTriggerId": "",
        "hideIcon": False,
        "isOncePlayEffect": False,
    },
]

FORMAL_AFFECTED_VERSION_REFS = (TARGET_CARD_VERSION_REF,)
FORMAL_DIRECT_VERSION_REFS: tuple[str, ...] = ()
FORMAL_CO_BLOCKED_VERSION_REFS = (TARGET_CARD_VERSION_REF,)


ANDROID_V323_NATIVE_EVIDENCE = {
    "enum": {
        "type": "ProduceExamPhaseType",
        "member": "ExamStaminaReduceCard",
        "value": TARGET_PHASE_VALUE,
    },
    "consume_card_cost": {
        "method": "ExamSequence.ConsumeCardCost",
        "rva": "0x7ED040C",
        "fact": (
            "cost payment asks ExamStatusEffectCollection.GetPhaseEffectList "
            "for phase 24 and builds the inserted trigger command"
        ),
    },
    "damage_stamina": {
        "method": "ExamEffectUtility.DamageStamina",
        "rva": "0x7E5F288",
        "fact": (
            "ordinary cost is split against Block; only the post-split "
            "stamina decrease is an accepted target event"
        ),
    },
    "phase_list": {
        "method": "ExamStatusEffectCollection.GetPhaseEffectList",
        "metadata_va": "0x7EA2F18",
        "raw_file_offset": "0x7E9EF18",
    },
    "command_order": {
        "get_trigger_commands": "0x7ECF230",
        "get_insert_trigger_commands": "0x7ED0690",
        "fact": (
            "active listeners are enumerated in stable order; child plans are "
            "snapshotted and SpendCount is called while commands are built"
        ),
    },
    "playing_card": {
        "method": "ExamSequence.ExecuteCardCommandImpl",
        "rva": "0x7ECE628",
        "fact": (
            "SetPlayingCard precedes the normal card-play listener snapshot; "
            "the phase-24 cost event is later than payment and earlier than "
            "the current card's direct effects"
        ),
    },
}

# Naming aliases follow the adjacent Plan2 standalone artifacts.
ANDROID_STAMINA_REDUCE_CARD_EVIDENCE = ANDROID_V323_NATIVE_EVIDENCE

PC_METADATA_EVIDENCE = {
    "source": "var/coverage/android_pc_exam_metadata_compatibility.json",
    "kind": "structural metadata cross-check only",
    "types": [
        "Campus.InGame.Exam.ExamEffectUtility",
        "Campus.InGame.Exam.IExamSequenceHandler",
        "Campus.InGame.Exam.ExamSequence",
    ],
    "native_semantics_source": "Android v3.2.3 native/ISIL evidence",
}
PC_STAMINA_REDUCE_CARD_METADATA = PC_METADATA_EVIDENCE


class Plan2StaminaReduceCardContractError(ValueError):
    """The supplied Master/runtime shape is outside this exact slice."""


Plan2StaminaReduceCardTriggerContractError = Plan2StaminaReduceCardContractError


class StaminaReduceCardSource(str, Enum):
    """Sources that are deliberately distinguished at the event boundary."""

    CARD_COST = "card-cost"
    FORCED_CARD_COST = "forced-card-cost"
    EXTRA_CARD_COST = "extra-card-cost"
    DIRECT_STAMINA_REDUCE = "direct-stamina-reduce"
    BLOCK_ONLY = "block-only"
    UNKNOWN = "unknown"


class StaminaReduceCardCostKind(str, Enum):
    ORDINARY = "ordinary"
    FORCE = "force"
    EXTRA = "extra"
    UNKNOWN = "unknown"


class StaminaReduceCardPaymentPath(str, Enum):
    CONSUME_CARD_COST = "ConsumeCardCost"
    BYPASSED = "bypassed"
    UNKNOWN = "unknown"


def _enum_value(value: object) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return value if isinstance(value, str) else "<unknown>"


def _plain_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2StaminaReduceCardContractError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise Plan2StaminaReduceCardContractError(
            f"{label} must be >= {minimum}, got {value}"
        )
    return value


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise Plan2StaminaReduceCardContractError(
            f"{label}: invalid JSON object"
        ) from error
    if not isinstance(decoded, dict):
        raise Plan2StaminaReduceCardContractError(f"{label}: expected object")
    return decoded


def _json_array(value: object, label: str) -> list[object]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise Plan2StaminaReduceCardContractError(
            f"{label}: invalid JSON array"
        ) from error
    if not isinstance(decoded, list):
        raise Plan2StaminaReduceCardContractError(f"{label}: expected array")
    return decoded


def _equal(raw: Mapping[str, object], key: str, expected: object, label: str) -> None:
    if raw.get(key) != expected:
        raise Plan2StaminaReduceCardContractError(
            f"{label}: {key} must be {expected!r}, got {raw.get(key)!r}"
        )


def _exact_keys(raw: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(raw)
    if actual != expected:
        raise Plan2StaminaReduceCardContractError(
            f"{label}: raw key set changed; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}"
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

_STATUS_RAW_KEYS = {
    "id",
    "assetId",
    "produceExamTriggerId",
    "produceExamEffectIds",
    "produceDescriptions",
}


def _validate_descriptions(value: object, expected_count: int, label: str) -> None:
    if not isinstance(value, list) or len(value) != expected_count:
        raise Plan2StaminaReduceCardContractError(
            f"{label}: expected {expected_count} descriptions"
        )
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != _DESCRIPTION_KEYS:
            raise Plan2StaminaReduceCardContractError(
                f"{label}[{index}]: description shape changed"
            )


def _validate_effect(
    row: sqlite3.Row,
    *,
    effect_type: str,
    value1: int,
    value2: int,
    count: int,
    turn: int,
    status_id: str,
    groups: tuple[str, ...],
    description_counts: tuple[int, int],
) -> None:
    label = str(row["id"])
    for key, expected in (
        ("effect_type", effect_type),
        ("value1", value1),
        ("value2", value2),
        ("effect_count", count),
        ("effect_turn", turn),
        ("status_enchant_id", status_id),
        ("chain_effect_id", ""),
    ):
        if row[key] != expected:
            raise Plan2StaminaReduceCardContractError(
                f"{label}: column {key} must be {expected!r}"
            )
    raw = _json_object(row["raw_json"], label)
    _exact_keys(raw, _EFFECT_RAW_KEYS, label)
    for key, expected in (
        ("id", label),
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
        ("effectGroupIds", list(groups)),
    ):
        _equal(raw, key, expected, label)
    _validate_descriptions(
        raw["produceDescriptions"], description_counts[0],
        f"{label}.produceDescriptions",
    )
    _validate_descriptions(
        raw["customizeProduceDescriptions"], description_counts[1],
        f"{label}.customizeProduceDescriptions",
    )


@dataclass(frozen=True, slots=True)
class Plan2StaminaReduceCardVersion:
    upgrade: int
    name: str
    stamina: int
    force_stamina: int
    play_trigger_id: str
    play_effect_ids: tuple[str, ...]
    installer_index: int = 2

    def __post_init__(self) -> None:
        if self.upgrade != TARGET_CARD_UPGRADE:
            raise Plan2StaminaReduceCardContractError(
                "only card upgrade 0 is in scope"
            )
        if (
            self.name != TARGET_CARD_NAME
            or self.stamina != 8
            or self.force_stamina != 0
            or self.play_trigger_id != TARGET_CARD_GATE_ID
            or self.play_effect_ids
            != tuple(item["produceExamEffectId"] for item in TARGET_PLAY_EFFECTS)
            or self.installer_index != 2
        ):
            raise Plan2StaminaReduceCardContractError(
                "target card version shape changed"
            )


@dataclass(frozen=True, slots=True)
class Plan2StaminaReduceCardProgram:
    card: Plan2StaminaReduceCardVersion
    wrapper_effect_id: str
    status_enchant_id: str
    trigger_id: str
    phase_type: str
    effects: tuple[Plan2CardPlayEffect, ...]
    wrapper_effect_group_ids: tuple[str, ...]
    listener_turn: int
    listener_limit_count: int
    listener_limit_count_in_turn: int
    card_gate_id: str = TARGET_CARD_GATE_ID

    def __post_init__(self) -> None:
        if not isinstance(self.card, Plan2StaminaReduceCardVersion):
            raise TypeError("card must be the exact target card version")
        if (
            self.wrapper_effect_id != TARGET_WRAPPER_EFFECT_ID
            or self.status_enchant_id != TARGET_STATUS_ENCHANT_ID
            or self.trigger_id != TARGET_TRIGGER_ID
            or self.phase_type != TARGET_PHASE_TYPE
            or self.wrapper_effect_group_ids != TARGET_WRAPPER_GROUPS
            or self.listener_turn != PERMANENT_TURN
            or self.listener_limit_count != UNLIMITED_COUNT
            or self.listener_limit_count_in_turn != UNLIMITED_COUNT
            or self.card_gate_id != TARGET_CARD_GATE_ID
        ):
            raise Plan2StaminaReduceCardContractError(
                "program identity/lifetime shape is outside the target"
            )
        if len(self.effects) != 1 or not isinstance(
            self.effects[0], Plan2CardPlayEffect
        ):
            raise Plan2StaminaReduceCardContractError(
                "program must contain exactly one ordered child"
            )
        child = self.effects[0]
        if (
            child.effect_id != TARGET_CHILD_EFFECT_ID
            or child.effect_type != CHILD_EFFECT_TYPE
            or child.value1 != TARGET_CHILD_VALUE1
            or child.value2 != TARGET_CHILD_VALUE2
            or child.count != TARGET_CHILD_COUNT
            or child.turn != TARGET_CHILD_TURN
            or child.effect_group_ids != TARGET_CHILD_GROUPS
        ):
            raise Plan2StaminaReduceCardContractError(
                "child effect shape is outside the exact target"
            )

    @property
    def cards(self) -> tuple[Plan2StaminaReduceCardVersion, ...]:
        return (self.card,)

    def to_dict(self) -> dict[str, object]:
        return {
            "card_version_refs": [TARGET_CARD_VERSION_REF],
            "card_gate_id": self.card_gate_id,
            "wrapper_effect_id": self.wrapper_effect_id,
            "status_enchant_id": self.status_enchant_id,
            "trigger_id": self.trigger_id,
            "phase_type": self.phase_type,
            "listener": {
                "turn": self.listener_turn,
                "limit_count": self.listener_limit_count,
                "limit_count_in_turn": self.listener_limit_count_in_turn,
            },
            "ordered_child_effect_ids": [effect.effect_id for effect in self.effects],
        }


def _expected_card_raw() -> dict[str, object]:
    return {
        "id": TARGET_CARD_ID,
        "upgradeCount": 0,
        "name": TARGET_CARD_NAME,
        "assetId": "img_general_skillcard_men-100_011",
        "isCharacterAsset": True,
        "voiceAssetId": "",
        "rarity": "ProduceCardRarity_Legend",
        "planType": PLAN2,
        "category": MENTAL_SKILL,
        "stamina": 8,
        "forceStamina": 0,
        "costType": UNKNOWN_COST,
        "costValue": 0,
        "playProduceExamTriggerId": TARGET_CARD_GATE_ID,
        "playEffects": TARGET_PLAY_EFFECTS,
        "playMovePositionType": MOVE_LOST,
        "moveEffectTriggerType": "ProduceCardMoveEffectTriggerType_Unknown",
        "moveProduceExamEffectIds": [],
        "isEndTurnLost": False,
        "isInitial": False,
        "isRestrict": False,
        "produceCardStatusEnchantId": "",
        "searchTag": "",
        "libraryHidden": False,
        "noDeckDuplication": True,
        "isReward": False,
        "unlockProducerLevel": 0,
        "rentalUnlockProducerLevel": 0,
        "evaluation": 0,
        "originIdolCardId": "",
        "originSupportCardId": "",
        "isInitialDeckProduceCard": False,
        "effectGroupIds": list(TARGET_CARD_EFFECT_GROUPS),
        "produceCardCustomizeIds": [],
        "maxCustomizeCount": 0,
        "isConversion": False,
        "moveProduceExamTriggerIds": [],
        "originCharacterId": "",
        "originPrimaStellaIdolCardId": "",
        "viewStartTime": "0",
        "isLimited": False,
        "order": "92020000680011",
    }


def _validate_card(row: sqlite3.Row) -> Plan2StaminaReduceCardVersion:
    label = TARGET_CARD_VERSION_REF
    expected_columns = (
        ("id", TARGET_CARD_ID),
        ("upgrade_count", 0),
        ("name", TARGET_CARD_NAME),
        ("plan_type", PLAN2),
        ("category", MENTAL_SKILL),
        ("stamina", 8),
        ("cost_type", UNKNOWN_COST),
        ("cost_value", 0),
        ("play_trigger_id", TARGET_CARD_GATE_ID),
        ("move_position_type", MOVE_LOST),
    )
    for key, expected in expected_columns:
        if row[key] != expected:
            raise Plan2StaminaReduceCardContractError(
                f"{label}: column {key} must be {expected!r}"
            )
    play_effects = _json_array(row["play_effects_json"], f"{label}.play_effects_json")
    if play_effects != TARGET_PLAY_EFFECTS:
        raise Plan2StaminaReduceCardContractError(
            f"{label}: ordered play effects changed"
        )
    raw = _json_object(row["raw_json"], label)
    _exact_keys(raw, _CARD_RAW_KEYS, label)
    for key, expected in _expected_card_raw().items():
        _equal(raw, key, expected, label)
    descriptions = _json_array(row["descriptions_json"], f"{label}.descriptions_json")
    if raw["produceDescriptions"] != descriptions:
        raise Plan2StaminaReduceCardContractError(
            f"{label}: description column/raw payload mismatch"
        )
    _validate_descriptions(descriptions, 33, f"{label}.produceDescriptions")
    return Plan2StaminaReduceCardVersion(
        upgrade=0,
        name=TARGET_CARD_NAME,
        stamina=8,
        force_stamina=0,
        play_trigger_id=TARGET_CARD_GATE_ID,
        play_effect_ids=tuple(
            str(item["produceExamEffectId"]) for item in TARGET_PLAY_EFFECTS
        ),
    )


def _validate_trigger(row: sqlite3.Row) -> None:
    label = TARGET_TRIGGER_ID
    expected_columns = (
        ("phase_types_json", [TARGET_PHASE_TYPE]),
        ("phase_values_json", []),
        ("field_status_check_types_json", []),
        ("field_status_types_json", []),
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
        actual = (
            _json_array(row[key], f"{label}.{key}")
            if key.endswith("_json")
            else row[key]
        )
        if actual != expected:
            raise Plan2StaminaReduceCardContractError(
                f"{label}: {key} must be {expected!r}"
            )
    raw = _json_object(row["raw_json"], label)
    _exact_keys(raw, _TRIGGER_RAW_KEYS, label)
    for key, expected in (
        ("id", label),
        ("phaseTypes", [TARGET_PHASE_TYPE]),
        ("phaseValues", []),
        ("fieldStatusCheckTypes", []),
        ("fieldStatusTypes", []),
        ("fieldStatusValues", []),
        ("fieldStatusProduceCardSearchIds", []),
        ("produceCardSearchId", ""),
        ("upperSearchCount", 0),
        ("lowerSearchCount", 0),
        ("cardMovePositionType", MOVE_UNKNOWN),
        ("effectTypes", []),
        ("lessonType", LESSON_UNKNOWN),
    ):
        _equal(raw, key, expected, label)
    _validate_descriptions(raw["produceDescriptions"], 1, f"{label}.produceDescriptions")
    _validate_descriptions(
        raw["playProduceDescriptions"], 1, f"{label}.playProduceDescriptions"
    )
    _validate_descriptions(
        raw["playEffectProduceDescriptions"],
        1,
        f"{label}.playEffectProduceDescriptions",
    )


def _validate_status(row: sqlite3.Row) -> None:
    label = TARGET_STATUS_ENCHANT_ID
    children = [TARGET_CHILD_EFFECT_ID]
    if row["asset_id"] != "" or row["produce_exam_trigger_id"] != TARGET_TRIGGER_ID:
        raise Plan2StaminaReduceCardContractError(
            f"{label}: normalized status fields changed"
        )
    if _json_array(row["produce_exam_effect_ids_json"], f"{label}.children") != children:
        raise Plan2StaminaReduceCardContractError(f"{label}: ordered child list changed")
    raw = _json_object(row["raw_json"], label)
    _exact_keys(raw, _STATUS_RAW_KEYS, label)
    for key, expected in (
        ("id", label),
        ("assetId", ""),
        ("produceExamTriggerId", TARGET_TRIGGER_ID),
        ("produceExamEffectIds", children),
    ):
        _equal(raw, key, expected, label)
    _validate_descriptions(raw["produceDescriptions"], 10, f"{label}.produceDescriptions")


def load_plan2_stamina_reduce_card_trigger_program(
    database: Path = DEFAULT_DATABASE,
) -> Plan2StaminaReduceCardProgram:
    """Load exactly one target card version and its Master dependency chain."""

    database = Path(database).resolve()
    if not database.is_file():
        raise Plan2StaminaReduceCardContractError(
            f"Master database is unavailable: {database}"
        )
    try:
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
    except (OSError, sqlite3.Error) as error:
        raise Plan2StaminaReduceCardContractError(
            f"could not read exact Master slice: {database}"
        ) from error
    if len(cards) != 1 or any(item is None for item in (wrapper, status, trigger, child)):
        raise Plan2StaminaReduceCardContractError(
            "target Master slice is incomplete or has an unexpected card upgrade"
        )
    card = _validate_card(cards[0])
    _validate_effect(
        wrapper,
        effect_type=STATUS_ENCHANT_EFFECT_TYPE,
        value1=0,
        value2=0,
        count=TARGET_WRAPPER_COUNT,
        turn=TARGET_WRAPPER_TURN,
        status_id=TARGET_STATUS_ENCHANT_ID,
        groups=TARGET_WRAPPER_GROUPS,
        description_counts=(9, 9),
    )
    _validate_effect(
        child,
        effect_type=CHILD_EFFECT_TYPE,
        value1=TARGET_CHILD_VALUE1,
        value2=TARGET_CHILD_VALUE2,
        count=TARGET_CHILD_COUNT,
        turn=TARGET_CHILD_TURN,
        status_id="",
        groups=TARGET_CHILD_GROUPS,
        description_counts=(6, 7),
    )
    _validate_status(status)
    _validate_trigger(trigger)
    child_plan = Plan2CardPlayEffect(
        effect_id=TARGET_CHILD_EFFECT_ID,
        effect_type=CHILD_EFFECT_TYPE,
        value1=TARGET_CHILD_VALUE1,
        value2=TARGET_CHILD_VALUE2,
        count=TARGET_CHILD_COUNT,
        turn=TARGET_CHILD_TURN,
        effect_group_ids=TARGET_CHILD_GROUPS,
    )
    return Plan2StaminaReduceCardProgram(
        card=card,
        wrapper_effect_id=TARGET_WRAPPER_EFFECT_ID,
        status_enchant_id=TARGET_STATUS_ENCHANT_ID,
        trigger_id=TARGET_TRIGGER_ID,
        phase_type=TARGET_PHASE_TYPE,
        effects=(child_plan,),
        wrapper_effect_group_ids=TARGET_WRAPPER_GROUPS,
        listener_turn=PERMANENT_TURN,
        listener_limit_count=UNLIMITED_COUNT,
        listener_limit_count_in_turn=UNLIMITED_COUNT,
    )


@dataclass(frozen=True, slots=True)
class Plan2StaminaReduceCardListener:
    """A local phase-24 listener with the existing Plan2 child primitive."""

    status_uid: int
    wrapper_effect_id: str
    status_enchant_id: str
    trigger_id: str
    effects: tuple[Plan2CardPlayEffect, ...]
    wrapper_effect_group_ids: tuple[str, ...]
    turn: int = PERMANENT_TURN
    limit_count: int = UNLIMITED_COUNT
    limit_count_in_turn: int = UNLIMITED_COUNT
    limit_count_in_turn_remaining: int = UNLIMITED_COUNT
    turn_count: int = 0
    is_passing_turn_start: bool = False

    def __post_init__(self) -> None:
        _plain_int(self.status_uid, "status_uid", minimum=1)
        if (
            self.wrapper_effect_id != TARGET_WRAPPER_EFFECT_ID
            or self.status_enchant_id != TARGET_STATUS_ENCHANT_ID
            or self.trigger_id != TARGET_TRIGGER_ID
            or tuple(self.wrapper_effect_group_ids) != TARGET_WRAPPER_GROUPS
            or self.turn != PERMANENT_TURN
            or self.limit_count != UNLIMITED_COUNT
            or self.limit_count_in_turn != UNLIMITED_COUNT
            or self.limit_count_in_turn_remaining != UNLIMITED_COUNT
        ):
            raise Plan2StaminaReduceCardContractError(
                "listener identity/lifetime shape is outside the target"
            )
        if type(self.is_passing_turn_start) is not bool:
            raise TypeError("is_passing_turn_start must be boolean")
        _plain_int(self.turn_count, "turn_count", minimum=0)
        effects = tuple(self.effects)
        if effects != (
            Plan2CardPlayEffect(
                TARGET_CHILD_EFFECT_ID,
                CHILD_EFFECT_TYPE,
                TARGET_CHILD_VALUE1,
                TARGET_CHILD_VALUE2,
                TARGET_CHILD_COUNT,
                TARGET_CHILD_TURN,
                TARGET_CHILD_GROUPS,
            ),
        ):
            raise Plan2StaminaReduceCardContractError(
                "listener child snapshot is outside the target"
            )
        object.__setattr__(self, "effects", effects)
        object.__setattr__(self, "wrapper_effect_group_ids", TARGET_WRAPPER_GROUPS)

    @property
    def can_trigger(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Plan2StaminaReduceCardScheduler:
    listeners: tuple[Plan2StaminaReduceCardListener, ...] = ()
    next_status_uid: int = 1

    def __post_init__(self) -> None:
        listeners = tuple(self.listeners)
        if any(not isinstance(item, Plan2StaminaReduceCardListener) for item in listeners):
            raise TypeError("listeners must contain target phase-24 listeners")
        uids = [item.status_uid for item in listeners]
        if len(uids) != len(set(uids)):
            raise ValueError("listener status_uids must be unique")
        _plain_int(self.next_status_uid, "next_status_uid", minimum=1)
        if uids and self.next_status_uid <= max(uids):
            raise ValueError("next_status_uid must exceed active listener uids")
        object.__setattr__(self, "listeners", listeners)


@dataclass(frozen=True, slots=True)
class Plan2StaminaReduceCardInstallTransition:
    before: Plan2StaminaReduceCardScheduler
    after: Plan2StaminaReduceCardScheduler
    card_upgrade: int
    created_status_uid: int
    event_trace: tuple[str, ...]


def install_plan2_stamina_reduce_card_listener(
    scheduler: Plan2StaminaReduceCardScheduler,
    program: Plan2StaminaReduceCardProgram,
    *,
    card_id: str = TARGET_CARD_ID,
    upgrade: int = TARGET_CARD_UPGRADE,
) -> Plan2StaminaReduceCardInstallTransition:
    """Install only the target card's third, ordered direct-play effect."""

    if not isinstance(scheduler, Plan2StaminaReduceCardScheduler):
        raise TypeError("scheduler must be Plan2StaminaReduceCardScheduler")
    if not isinstance(program, Plan2StaminaReduceCardProgram):
        raise TypeError("program must be Plan2StaminaReduceCardProgram")
    if card_id != TARGET_CARD_ID or upgrade != TARGET_CARD_UPGRADE:
        raise Plan2StaminaReduceCardContractError(
            "card identity/upgrade is outside the exact target"
        )
    card = program.card
    if card.play_effect_ids[card.installer_index] != program.wrapper_effect_id:
        raise Plan2StaminaReduceCardContractError(
            "card installer binding is not the Master-ordered target slot"
        )
    uid = scheduler.next_status_uid
    listener = Plan2StaminaReduceCardListener(
        status_uid=uid,
        wrapper_effect_id=program.wrapper_effect_id,
        status_enchant_id=program.status_enchant_id,
        trigger_id=program.trigger_id,
        effects=program.effects,
        wrapper_effect_group_ids=program.wrapper_effect_group_ids,
    )
    after = replace(
        scheduler,
        listeners=scheduler.listeners + (listener,),
        next_status_uid=uid + 1,
    )
    return Plan2StaminaReduceCardInstallTransition(
        before=scheduler,
        after=after,
        card_upgrade=upgrade,
        created_status_uid=uid,
        event_trace=(
            "card-play:direct-effect:slot-0:e_effect-exam_playable_value_add-01",
            "card-play:direct-effect:slot-1:e_effect-exam_card_play_aggressive-0005",
            f"card-play:direct-effect:slot-2:{TARGET_WRAPPER_EFFECT_ID}",
            f"listener:install:{uid}:turn=-1:limit=-1:per-turn=-1",
        ),
    )


@dataclass(frozen=True, slots=True)
class Plan2StaminaReduceCardEvent:
    """Explicit native observations required to admit phase 24."""

    source: str | StaminaReduceCardSource = StaminaReduceCardSource.UNKNOWN
    cost_kind: str | StaminaReduceCardCostKind = StaminaReduceCardCostKind.UNKNOWN
    payment_path: str | StaminaReduceCardPaymentPath = (
        StaminaReduceCardPaymentPath.CONSUME_CARD_COST
    )
    phase_type: str = TARGET_PHASE_TYPE
    card_id: str = TARGET_CARD_ID
    upgrade: int = TARGET_CARD_UPGRADE
    payment_succeeded: bool = True
    accepted_by_cost_validator: bool = True
    is_consume_cost: bool = True
    requested_cost: int | None = None
    stamina_before: int | None = None
    stamina_after: int | None = None
    actual_stamina_delta: int | None = None
    block_before: int | None = None
    block_after: int | None = None
    set_playing_card: bool = True
    card_play_listeners_captured: bool = True
    direct_effects_started: bool = False
    current_card_installer_active: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("payment_succeeded", self.payment_succeeded),
            ("accepted_by_cost_validator", self.accepted_by_cost_validator),
            ("is_consume_cost", self.is_consume_cost),
            ("set_playing_card", self.set_playing_card),
            ("card_play_listeners_captured", self.card_play_listeners_captured),
            ("direct_effects_started", self.direct_effects_started),
            ("current_card_installer_active", self.current_card_installer_active),
        ):
            if type(value) is not bool:
                raise TypeError(f"{label} must be boolean")
        if self.requested_cost is not None:
            _plain_int(self.requested_cost, "requested_cost", minimum=0)
        for label, value in (
            ("stamina_before", self.stamina_before),
            ("stamina_after", self.stamina_after),
            ("actual_stamina_delta", self.actual_stamina_delta),
            ("block_before", self.block_before),
            ("block_after", self.block_after),
        ):
            if value is not None:
                _plain_int(value, label, minimum=0)
        _plain_int(self.upgrade, "upgrade", minimum=0)


@dataclass(frozen=True, slots=True)
class Plan2StaminaReduceCardEvaluation:
    trigger_id: str
    phase_type: str
    supported: bool
    fires: bool | None
    reason: str | None
    source: str
    cost_kind: str
    actual_stamina_delta: int | None
    snapshot_boundary: str
    event_trace: tuple[str, ...]

    @property
    def should_trigger(self) -> bool:
        return self.fires is True


def _evaluation_fail(event: Plan2StaminaReduceCardEvent, reason: str) -> Plan2StaminaReduceCardEvaluation:
    return Plan2StaminaReduceCardEvaluation(
        trigger_id=TARGET_TRIGGER_ID,
        phase_type=event.phase_type,
        supported=False,
        fires=None,
        reason=reason,
        source=_enum_value(event.source),
        cost_kind=_enum_value(event.cost_kind),
        actual_stamina_delta=None,
        snapshot_boundary="unknown",
        event_trace=(f"phase:{event.phase_type}:fail-closed:{reason}",),
    )


def evaluate_plan2_stamina_reduce_card_event(
    event: Plan2StaminaReduceCardEvent,
) -> Plan2StaminaReduceCardEvaluation:
    """Admit exactly a post-payment, actual-loss phase-24 event."""

    if not isinstance(event, Plan2StaminaReduceCardEvent):
        raise TypeError("event must be Plan2StaminaReduceCardEvent")
    source = _enum_value(event.source)
    cost_kind = _enum_value(event.cost_kind)
    payment_path = _enum_value(event.payment_path)
    if (
        event.phase_type != TARGET_PHASE_TYPE
        or event.card_id != TARGET_CARD_ID
        or event.upgrade != TARGET_CARD_UPGRADE
    ):
        return _evaluation_fail(event, "phase/card/version-unknown")
    if source in {
        StaminaReduceCardSource.DIRECT_STAMINA_REDUCE.value,
        StaminaReduceCardSource.BLOCK_ONLY.value,
        StaminaReduceCardSource.UNKNOWN.value,
    }:
        return _evaluation_fail(event, "source-not-card-cost")
    if source not in {
        StaminaReduceCardSource.CARD_COST.value,
        StaminaReduceCardSource.FORCED_CARD_COST.value,
        StaminaReduceCardSource.EXTRA_CARD_COST.value,
    }:
        return _evaluation_fail(event, "source-unknown")
    if cost_kind == StaminaReduceCardCostKind.UNKNOWN.value:
        return _evaluation_fail(event, "cost-kind-unknown")
    if payment_path != StaminaReduceCardPaymentPath.CONSUME_CARD_COST.value:
        return _evaluation_fail(event, "payment-path-not-ConsumeCardCost")
    if not event.is_consume_cost:
        return _evaluation_fail(event, "cost-consumption-bypassed")
    if not event.set_playing_card or not event.card_play_listeners_captured:
        return _evaluation_fail(event, "pre-SetPlaying-or-listener-snapshot-unknown")
    if event.direct_effects_started or event.current_card_installer_active:
        return _evaluation_fail(event, "direct-effects-already-started")
    if source == StaminaReduceCardSource.CARD_COST.value and cost_kind != StaminaReduceCardCostKind.ORDINARY.value:
        return _evaluation_fail(event, "ordinary-source-cost-kind-mismatch")
    if source == StaminaReduceCardSource.FORCED_CARD_COST.value and cost_kind not in {
        StaminaReduceCardCostKind.ORDINARY.value,
        StaminaReduceCardCostKind.FORCE.value,
    }:
        return _evaluation_fail(event, "forced-source-cost-kind-mismatch")
    if source == StaminaReduceCardSource.EXTRA_CARD_COST.value and cost_kind not in {
        StaminaReduceCardCostKind.ORDINARY.value,
        StaminaReduceCardCostKind.EXTRA.value,
    }:
        return _evaluation_fail(event, "extra-source-cost-kind-mismatch")

    # The target Master card has forceStamina=0 and no extra-cost field.  A
    # positive standalone force/extra component is therefore not guessed into
    # the ordinary card-cost event.
    if cost_kind in {
        StaminaReduceCardCostKind.FORCE.value,
        StaminaReduceCardCostKind.EXTRA.value,
    }:
        if event.requested_cost not in (None, 0):
            return _evaluation_fail(event, "positive-force-or-extra-cost-not-in-target-card")
        if cost_kind == StaminaReduceCardCostKind.FORCE.value and (
            event.block_before is not None
            and event.block_after is not None
            and event.block_before != event.block_after
        ):
            return _evaluation_fail(event, "force-cost-cannot-be-block-absorbed")

    if not event.payment_succeeded or not event.accepted_by_cost_validator:
        return Plan2StaminaReduceCardEvaluation(
            trigger_id=TARGET_TRIGGER_ID,
            phase_type=TARGET_PHASE_TYPE,
            supported=True,
            fires=False,
            reason="payment-failed-or-rejected-no-phase-event",
            source=source,
            cost_kind=cost_kind,
            actual_stamina_delta=None,
            snapshot_boundary="post-payment-before-direct-effects",
            event_trace=(
                "ConsumeCardCost:payment:failed-or-rejected",
                "phase:ExamStaminaReduceCard:not-emitted",
            ),
        )
    if event.requested_cost is None:
        return _evaluation_fail(event, "requested-cost-unknown")
    if event.stamina_before is None or event.stamina_after is None:
        return _evaluation_fail(event, "post-payment-stamina-snapshot-unknown")
    actual_delta = event.stamina_before - event.stamina_after
    if event.actual_stamina_delta is not None and event.actual_stamina_delta != actual_delta:
        return _evaluation_fail(event, "stamina-delta-mismatch")
    if actual_delta < 0:
        return _evaluation_fail(event, "stamina-increased-or-invalid")
    if event.block_before is not None or event.block_after is not None:
        if event.block_before is None or event.block_after is None:
            return _evaluation_fail(event, "partial-block-snapshot")
        if event.block_after > event.block_before:
            return _evaluation_fail(event, "block-increased-during-cost-payment")
    if cost_kind == StaminaReduceCardCostKind.FORCE.value and (
        event.block_before is None
        or event.block_after is None
        or event.block_before != event.block_after
    ):
        return _evaluation_fail(event, "force-cost-block-boundary-unknown")
    if event.requested_cost == 0 or actual_delta == 0:
        return Plan2StaminaReduceCardEvaluation(
            trigger_id=TARGET_TRIGGER_ID,
            phase_type=TARGET_PHASE_TYPE,
            supported=True,
            fires=False,
            reason="zero-or-no-post-block-stamina-delta",
            source=source,
            cost_kind=cost_kind,
            actual_stamina_delta=actual_delta,
            snapshot_boundary="post-payment-before-direct-effects",
            event_trace=(
                "ConsumeCardCost:payment:accepted",
                f"DamageStamina:post-block:actual-delta={actual_delta}",
                "phase:ExamStaminaReduceCard:not-emitted",
            ),
        )
    return Plan2StaminaReduceCardEvaluation(
        trigger_id=TARGET_TRIGGER_ID,
        phase_type=TARGET_PHASE_TYPE,
        supported=True,
        fires=True,
        reason=None,
        source=source,
        cost_kind=cost_kind,
        actual_stamina_delta=actual_delta,
        snapshot_boundary="post-payment-before-direct-effects",
        event_trace=(
            "SetPlayingCard",
            "capture:ExamCardPlay:pre-payment",
            "ConsumeCardCost:payment:accepted",
            f"DamageStamina:post-block:actual-delta={actual_delta}",
            "capture:ExamStaminaReduceCard:post-payment-before-direct-effects",
        ),
    )


def evaluate_plan2_stamina_reduce_card_trigger(
    event: Plan2StaminaReduceCardEvent,
) -> Plan2StaminaReduceCardEvaluation:
    """Alias kept explicit for callers naming the trigger rather than phase."""

    return evaluate_plan2_stamina_reduce_card_event(event)


@dataclass(frozen=True, slots=True)
class Plan2CapturedStaminaReduceCardCommand:
    listener_status_uid: int
    effect_snapshot: tuple[Plan2CardPlayEffect, ...]
    spend_count_called: bool = True
    spend_count_delta: int = 0


@dataclass(frozen=True, slots=True)
class Plan2StaminaReduceCardCaptureTransition:
    before: Plan2StaminaReduceCardScheduler
    after_spend: Plan2StaminaReduceCardScheduler
    commands: tuple[Plan2CapturedStaminaReduceCardCommand, ...]
    admitted: bool
    evaluation: Plan2StaminaReduceCardEvaluation
    fail_closed_reason: str | None
    event_trace: tuple[str, ...]


def capture_plan2_stamina_reduce_card_commands(
    scheduler: Plan2StaminaReduceCardScheduler,
    event: Plan2StaminaReduceCardEvent,
    *,
    program: Plan2StaminaReduceCardProgram | None = None,
) -> Plan2StaminaReduceCardCaptureTransition:
    """Snapshot children in active order, then call native-style SpendCount."""

    if not isinstance(scheduler, Plan2StaminaReduceCardScheduler):
        raise TypeError("scheduler must be Plan2StaminaReduceCardScheduler")
    if not isinstance(event, Plan2StaminaReduceCardEvent):
        raise TypeError("event must be Plan2StaminaReduceCardEvent")
    if program is not None and not isinstance(program, Plan2StaminaReduceCardProgram):
        raise TypeError("program must be Plan2StaminaReduceCardProgram or None")
    evaluation = evaluate_plan2_stamina_reduce_card_event(event)
    if not evaluation.should_trigger:
        trace = evaluation.event_trace + (
            "phase:ExamStaminaReduceCard:listener-capture:skipped",
        )
        return Plan2StaminaReduceCardCaptureTransition(
            before=scheduler,
            after_spend=scheduler,
            commands=(),
            admitted=False,
            evaluation=evaluation,
            fail_closed_reason=evaluation.reason if not evaluation.supported else None,
            event_trace=trace,
        )
    listeners = list(scheduler.listeners)
    commands: list[Plan2CapturedStaminaReduceCardCommand] = []
    trace = list(evaluation.event_trace)
    trace.append("phase:ExamStaminaReduceCard:listener-capture:active-list-order")
    for index, listener in enumerate(scheduler.listeners):
        if not listener.can_trigger:
            continue
        if program is not None and tuple(listener.effects) != program.effects:
            raise Plan2StaminaReduceCardContractError(
                "active listener child differs from the loaded program"
            )
        commands.append(
            Plan2CapturedStaminaReduceCardCommand(
                listener_status_uid=listener.status_uid,
                effect_snapshot=listener.effects,
                spend_count_called=True,
                spend_count_delta=0,
            )
        )
        trace.extend(
            (
                f"listener:{listener.status_uid}:build:open-trigger",
                f"listener:{listener.status_uid}:snapshot:child:0:{TARGET_CHILD_EFFECT_ID}",
                f"listener:{listener.status_uid}:SpendCount:unlimited:no-delta",
                f"listener:{listener.status_uid}:build:close-trigger",
            )
        )
        # The exact wrapper has effectCount=0, so SpendCount does not consume
        # a lifetime or per-turn budget.  Keep the immutable state identical.
        listeners[index] = listener
    after = replace(scheduler, listeners=tuple(listeners))
    trace.append("phase:ExamStaminaReduceCard:children:capture-complete")
    return Plan2StaminaReduceCardCaptureTransition(
        before=scheduler,
        after_spend=after,
        commands=tuple(commands),
        admitted=True,
        evaluation=evaluation,
        fail_closed_reason=None,
        event_trace=tuple(trace),
    )


@dataclass(frozen=True, slots=True)
class Plan2StaminaReduceCardCommandOccurrence:
    listener_status_uid: int
    child_commands: tuple[Plan2CardPlayEffect, ...]


@dataclass(frozen=True, slots=True)
class Plan2StaminaReduceCardExecutionTransition:
    before_scheduler: Plan2StaminaReduceCardScheduler
    after_scheduler: Plan2StaminaReduceCardScheduler
    capture: Plan2StaminaReduceCardCaptureTransition
    occurrences: tuple[Plan2StaminaReduceCardCommandOccurrence, ...]
    removed_status_uids: tuple[int, ...]
    event_trace: tuple[str, ...]


def execute_captured_plan2_stamina_reduce_card_commands(
    capture: Plan2StaminaReduceCardCaptureTransition,
) -> Plan2StaminaReduceCardExecutionTransition:
    """Return the ordered child command plan; do not execute its scalar."""

    if not isinstance(capture, Plan2StaminaReduceCardCaptureTransition):
        raise TypeError("capture must be Plan2StaminaReduceCardCaptureTransition")
    occurrences = tuple(
        Plan2StaminaReduceCardCommandOccurrence(
            listener_status_uid=command.listener_status_uid,
            child_commands=command.effect_snapshot,
        )
        for command in capture.commands
    )
    trace = list(capture.event_trace)
    if occurrences:
        trace.append("phase:ExamStaminaReduceCard:children:execute:active-list-order")
        for occurrence in occurrences:
            trace.append(
                f"listener:{occurrence.listener_status_uid}:child-order:0:{TARGET_CHILD_EFFECT_ID}"
            )
        trace.append("phase:ExamStaminaReduceCard:children:plan-only-no-scalar-execution")
    return Plan2StaminaReduceCardExecutionTransition(
        before_scheduler=capture.before,
        after_scheduler=capture.after_spend,
        capture=capture,
        occurrences=occurrences,
        removed_status_uids=(),
        event_trace=tuple(trace),
    )


def simulate_plan2_stamina_reduce_card_turn_start(
    scheduler: Plan2StaminaReduceCardScheduler,
) -> Plan2StaminaReduceCardScheduler:
    """Advance the existing Plan2 turn boundary without expiring this listener."""

    if not isinstance(scheduler, Plan2StaminaReduceCardScheduler):
        raise TypeError("scheduler must be Plan2StaminaReduceCardScheduler")
    return replace(
        scheduler,
        listeners=tuple(
            replace(
                listener,
                turn_count=listener.turn_count + 1,
                limit_count_in_turn_remaining=UNLIMITED_COUNT,
                is_passing_turn_start=True,
            )
            for listener in scheduler.listeners
        ),
    )


def make_plan2_stamina_reduce_card_audit() -> dict[str, object]:
    """Return the non-central native audit contract used by the new test."""

    return {
        "schema_version": 1,
        "scope": {
            "kind": "standalone-status-listener-child",
            "trigger_id": TARGET_TRIGGER_ID,
            "card_version_refs": list(FORMAL_AFFECTED_VERSION_REFS),
            "central_core_changed": False,
            "central_dispatcher_changed": False,
            "formal_coverage_artifact_changed": False,
            "plan3_changed": False,
            "gui_changed": False,
        },
        "formal_594_accounting": {
            "denominator": 594,
            "affected": 1,
            "direct": 0,
            "co_blocked": 1,
            "affected_version_refs": list(FORMAL_AFFECTED_VERSION_REFS),
            "direct_version_refs": list(FORMAL_DIRECT_VERSION_REFS),
            "co_blocked_version_refs": list(FORMAL_CO_BLOCKED_VERSION_REFS),
            "co_block_reason": (
                "The separate card gate "
                f"{TARGET_CARD_GATE_ID} remains unresolved; this status trigger "
                "alone unlocks no formal version."
            ),
        },
        "master_contract": {
            "card": {
                "version_ref": TARGET_CARD_VERSION_REF,
                "name": TARGET_CARD_NAME,
                "play_trigger_id": TARGET_CARD_GATE_ID,
                "play_effect_order": [item["produceExamEffectId"] for item in TARGET_PLAY_EFFECTS],
                "installer_slot": 2,
                "force_stamina": 0,
            },
            "installer": {
                "effect_id": TARGET_WRAPPER_EFFECT_ID,
                "effect_count": TARGET_WRAPPER_COUNT,
                "effect_turn": TARGET_WRAPPER_TURN,
                "native_limit_count": UNLIMITED_COUNT,
                "native_limit_count_in_turn": UNLIMITED_COUNT,
                "native_turn": PERMANENT_TURN,
            },
            "status": {
                "id": TARGET_STATUS_ENCHANT_ID,
                "trigger_id": TARGET_TRIGGER_ID,
                "ordered_child_effect_ids": [TARGET_CHILD_EFFECT_ID],
            },
            "trigger": {
                "id": TARGET_TRIGGER_ID,
                "phase_type": TARGET_PHASE_TYPE,
                "phase_value": TARGET_PHASE_VALUE,
                "other_predicates": "empty",
            },
            "child": {
                "id": TARGET_CHILD_EFFECT_ID,
                "effect_type": CHILD_EFFECT_TYPE,
                "value1": TARGET_CHILD_VALUE1,
                "value2": TARGET_CHILD_VALUE2,
                "effect_count": TARGET_CHILD_COUNT,
                "effect_turn": TARGET_CHILD_TURN,
                "execution": "Plan2 scalar family owns execution; this module only orders it",
            },
        },
        "event_contract": {
            "accepted": [
                "ordinary card cost through ConsumeCardCost with actual post-block stamina loss",
                "forced/extra card path only when it explicitly consumes the card cost through ConsumeCardCost",
            ],
            "rejected": [
                "generic direct stamina reduction (phase 20), recovery, or unknown source",
                "failed/rejected payment, bypassed cost, zero cost, and no-delta payment",
                "block-only absorption and standalone positive force/extra component absent from this card shape",
                "unknown event/wrapper/child/card shape",
            ],
            "snapshot": "SetPlayingCard -> normal card listener capture -> payment/block split -> phase 24 capture -> direct card effects",
        },
        "native_android_v323": ANDROID_V323_NATIVE_EVIDENCE,
        "pc_metadata": PC_METADATA_EVIDENCE,
        "lifecycle": {
            "trigger_count": "unlimited (Master effectCount=0)",
            "lifetime_turn": "permanent (Master effectTurn=-1)",
            "per_turn_count": "unlimited; SpendCount has no numeric delta",
            "child_command_order": "active listener order, then Master child order",
        },
        "central_integration": "none",
    }


# Short aliases make the bounded primitive discoverable without adding any
# registration side effect or touching central Plan2 modules.
load_stamina_reduce_card_trigger_program = load_plan2_stamina_reduce_card_trigger_program
load_plan2_stamina_reduce_card_program = load_plan2_stamina_reduce_card_trigger_program
install_stamina_reduce_card_listener = install_plan2_stamina_reduce_card_listener
capture_stamina_reduce_card_event = capture_plan2_stamina_reduce_card_commands
execute_captured_stamina_reduce_card_commands = (
    execute_captured_plan2_stamina_reduce_card_commands
)


__all__ = [
    "ANDROID_V323_NATIVE_EVIDENCE",
    "ANDROID_STAMINA_REDUCE_CARD_EVIDENCE",
    "CHILD_EFFECT_TYPE",
    "FORMAL_AFFECTED_VERSION_REFS",
    "FORMAL_CO_BLOCKED_VERSION_REFS",
    "FORMAL_DIRECT_VERSION_REFS",
    "PC_METADATA_EVIDENCE",
    "PC_STAMINA_REDUCE_CARD_METADATA",
    "PERMANENT_TURN",
    "Plan2CapturedStaminaReduceCardCommand",
    "Plan2StaminaReduceCardCaptureTransition",
    "Plan2StaminaReduceCardCommandOccurrence",
    "Plan2StaminaReduceCardContractError",
    "Plan2StaminaReduceCardTriggerContractError",
    "Plan2StaminaReduceCardEvent",
    "Plan2StaminaReduceCardEvaluation",
    "Plan2StaminaReduceCardExecutionTransition",
    "Plan2StaminaReduceCardInstallTransition",
    "Plan2StaminaReduceCardListener",
    "Plan2StaminaReduceCardProgram",
    "Plan2StaminaReduceCardScheduler",
    "Plan2StaminaReduceCardVersion",
    "StaminaReduceCardCostKind",
    "StaminaReduceCardPaymentPath",
    "StaminaReduceCardSource",
    "TARGET_CARD_GATE_ID",
    "TARGET_CARD_ID",
    "TARGET_CARD_NAME",
    "TARGET_CARD_UPGRADES",
    "TARGET_CARD_VERSION_REF",
    "TARGET_CHILD_EFFECT_ID",
    "TARGET_CHILD_EFFECT_TYPE",
    "TARGET_CHILD_EFFECT_GROUP_IDS",
    "TARGET_PHASE_TYPE",
    "TARGET_PHASE_VALUE",
    "TARGET_STATUS_ENCHANT_ID",
    "TARGET_TRIGGER_ID",
    "TARGET_WRAPPER_EFFECT_ID",
    "TARGET_WRAPPER_EFFECT_GROUP_IDS",
    "capture_plan2_stamina_reduce_card_commands",
    "capture_stamina_reduce_card_event",
    "evaluate_plan2_stamina_reduce_card_event",
    "evaluate_plan2_stamina_reduce_card_trigger",
    "execute_captured_plan2_stamina_reduce_card_commands",
    "execute_captured_stamina_reduce_card_commands",
    "install_plan2_stamina_reduce_card_listener",
    "install_stamina_reduce_card_listener",
    "load_plan2_stamina_reduce_card_trigger_program",
    "load_plan2_stamina_reduce_card_program",
    "load_stamina_reduce_card_trigger_program",
    "make_plan2_stamina_reduce_card_audit",
    "simulate_plan2_stamina_reduce_card_turn_start",
]
