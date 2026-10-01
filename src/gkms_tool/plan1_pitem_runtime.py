"""Generic Plan 1 equipped P-item runtime from a captured LocalSave graph.

The original Plan 1 card kernel intentionally exposes one FKTN-only item
contract.  That contract remains useful for the historical fixed deck, but it
is not a suitable boundary for a captured ``ExamSaveData``: the save contains
an ordered ``itemList`` and a graph of ``TriggerEffectStatusEffect`` objects
owned by those items.  This module is the generic boundary for that graph.

Only facts present in the LocalSave and Master rows are accepted.  Item IDs
and card IDs are never used as behavioural switches; they are data supplied by
the Master trigger/search rows.  A trigger or effect shape that cannot be
represented by the typed contract produces a blocker and never becomes a
partial listener.  The module is deliberately independent from the Plan 1
bridge so a caller can integrate it into a replay adapter without changing
the FKTN-only ``Plan1EquippedItemRuntime``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import sqlite3
from typing import Final, Literal

from .audition_local_save_state import LocalSaveExamState
from .card_search import (
    ProduceCardSearchRule,
    load_produce_card_search,
    match_exact_target_card_context_search,
)
from .master_db import DEFAULT_DATABASE
from .plan2_pitem_composite_trigger import (
    CARD_MOVE_UNKNOWN,
    CHECK_DEFAULT,
    CHECK_NOT,
    CompositeFieldPredicate,
    CompositeTriggerError,
    CompositeTriggerSpec,
    KNOWN_LESSON_TYPES,
    KNOWN_PHASES,
    LESSON_UNKNOWN,
    parse_composite_trigger,
)
from .plan2_pitem_event_dispatch import (
    KNOWN_PITEM_EFFECT_TYPES,
    KNOWN_PITEM_FIELD_TYPES,
    PItemEventPhase,
)


PLAN1: Final = "ProducePlanType_Plan1"
PLAN_COMMON: Final = "ProducePlanType_Common"
PLAN1_PITEM_PLAN_TYPES: Final = frozenset((PLAN_COMMON, PLAN1))
ITEM_EFFECT_STATUS_ENCHANT: Final = "ProduceItemEffectType_ExamStatusEnchant"

# The LocalSave serializer stores enum values, while Master stores names.
# These values are the stable Android v3.2.3 enum values needed by the graph
# boundary.  The map intentionally includes all current phase names and the
# Plan 1 effect families commonly found in item listeners.  An unknown Master
# name or save value remains a typed blocker.
SERIALIZED_PHASE_ENUM: Final[dict[str, int]] = {
    "ProduceExamPhaseType_ExamCardDraw": 1,
    "ProduceExamPhaseType_ExamCardPlay": 2,
    "ProduceExamPhaseType_ExamCardPlayAfter": 3,
    "ProduceExamPhaseType_ExamStartTurn": 4,
    "ProduceExamPhaseType_ExamEndTurn": 5,
    "ProduceExamPhaseType_ExamStartExam": 6,
    "ProduceExamPhaseType_ExamCardMoveGrave": 7,
    "ProduceExamPhaseType_ExamCardMoveHand": 29,
    "ProduceExamPhaseType_ExamCardMoveLost": 31,
    "ProduceExamPhaseType_ExamCardMove": 7,
    "ProduceExamPhaseType_ExamCardAdd": 8,
    "ProduceExamPhaseType_ExamLesson": 9,
    "ProduceExamPhaseType_ExamForecast": 10,
    "ProduceExamPhaseType_ExamSearchCardPlay": 11,
    "ProduceExamPhaseType_ExamStanceChange": 12,
    "ProduceExamPhaseType_ExamStatusChange": 13,
    "ProduceExamPhaseType_ExamTurnCheck": 16,
    "ProduceExamPhaseType_ExamUseDrink": 17,
    "ProduceExamPhaseType_ExamGetPoint": 18,
    "ProduceExamPhaseType_ExamShuffle": 19,
    "ProduceExamPhaseType_ExamStaminaReduce": 20,
    "ProduceExamPhaseType_ExamTurnTimer": 21,
    "ProduceExamPhaseType_ExamTurnInterval": 22,
    "ProduceExamPhaseType_ExamPlayCountInterval": 23,
    "ProduceExamPhaseType_ExamStaminaReduceCard": 24,
    "ProduceExamPhaseType_ExamPlayTurnCountInterval": 25,
    "ProduceExamPhaseType_StartExamPlay": 28,
    "ProduceExamPhaseType_ExamCardMoveHand": 29,
    "ProduceExamPhaseType_ExamCardMoveGrave": 30,
    "ProduceExamPhaseType_ExamCardMoveLost": 31,
    "ProduceExamPhaseType_ExamLessonParameterUp": 33,
    "ProduceExamPhaseType_ExamStanceChangeCountInterval": 35,
    "ProduceExamPhaseType_ExamStanceChangeCount": 36,
    "ProduceExamPhaseType_ExamStanceChangeConcentration": 37,
    "ProduceExamPhaseType_ExamStanceChangePreservation": 38,
    "ProduceExamPhaseType_ExamStanceChangeFullPower": 39,
    "ProduceExamPhaseType_ExamStanceReset": 40,
    "ProduceExamPhaseType_ExamTurnSkip": 41,
    "ProduceExamPhaseType_ExamEndTurnTimer": 42,
    "ProduceExamPhaseType_ExamEndTurnInterval": 43,
    "ProduceExamPhaseType_ExamPlayCountIntervalAfter": 44,
    "ProduceExamPhaseType_ExamStanceChangeFromConcentration": 45,
    "ProduceExamPhaseType_ExamStanceChangeFromPreservation": 46,
    "ProduceExamPhaseType_ExamStanceChangeFromFullPower": 47,
    "ProduceExamPhaseType_ExamCardUpgrade": 48,
    "ProduceExamPhaseType_ExamPlayCardMoveGrave": 49,
    "ProduceExamPhaseType_ExamParameterBuffUpInterval": 50,
    "ProduceExamPhaseType_ExamLessonBuffUpInterval": 51,
    "ProduceExamPhaseType_ExamReviewUpInterval": 52,
    "ProduceExamPhaseType_ExamAggressiveUpInterval": 53,
    "ProduceExamPhaseType_ExamFullPowerPointUpInterval": 54,
    "ProduceExamPhaseType_ExamStanceChangePreservationInterval": 55,
    "ProduceExamPhaseType_ExamStanceChangeConcentrationInterval": 56,
    "ProduceExamPhaseType_ExamStanceChangeFullPowerInterval": 57,
    "ProduceExamPhaseType_ExamCardUpgradeInterval": 58,
    "ProduceExamPhaseType_ExamCardDrawInterval": 59,
    "ProduceExamPhaseType_ExamBuffConsume": 34,
    "ProduceExamPhaseType_StartPlay": 27,
    "ProduceExamPhaseType_None": 999,
}

SERIALIZED_EFFECT_ENUM: Final[dict[str, int]] = {
    "ProduceExamEffectType_Unknown": 0,
    "ProduceExamEffectType_ExamLesson": 1,
    "ProduceExamEffectType_ExamParameterBuff": 2,
    "ProduceExamEffectType_ExamBlock": 3,
    "ProduceExamEffectType_ExamCardDraw": 4,
    "ProduceExamEffectType_ExamStaminaConsumptionDown": 5,
    "ProduceExamEffectType_ExamCardCreateId": 6,
    "ProduceExamEffectType_ExamCardMove": 9,
    "ProduceExamEffectType_ExamLessonBuff": 10,
    "ProduceExamEffectType_ExamCardUpgrade": 11,
    "ProduceExamEffectType_ExamBlockValueMultiple": 13,
    "ProduceExamEffectType_ExamPlayableValueAdd": 14,
    "ProduceExamEffectType_ExamLessonBuffMultiple": 15,
    "ProduceExamEffectType_ExamCardStaminaConsumptionChange": 17,
    "ProduceExamEffectType_ExamBlockRestriction": 18,
    "ProduceExamEffectType_ExamLessonDependBlock": 19,
    "ProduceExamEffectType_ExamCardCreateSearch": 21,
    "ProduceExamEffectType_ExamStatusEnchant": 22,
    "ProduceExamEffectType_ExamMultipleLessonBuffLesson": 23,
    "ProduceExamEffectType_ExamForcePlayCardSearch": 24,
    "ProduceExamEffectType_ExamCardStaminaConsumptionDownSpecify": 26,
    "ProduceExamEffectType_ExamStaminaDamage": 27,
    "ProduceExamEffectType_ExamLessonFix": 29,
    "ProduceExamEffectType_ExamCardDuplicate": 30,
    "ProduceExamEffectType_ExamLessonValueChangePerPlay": 32,
    "ProduceExamEffectType_ExamCardStaminaConsumptionReduce": 33,
    "ProduceExamEffectType_ExamReviewValueMultiple": 36,
    "ProduceExamEffectType_ExamCardSearchEffectPlayCountBuff": 38,
    "ProduceExamEffectType_ExamLessonValueMultiple": 39,
    "ProduceExamEffectType_ExamConcentration": 45,
    "ProduceExamEffectType_ExamPreservation": 46,
    "ProduceExamEffectType_ExamFullPower": 47,
    "ProduceExamEffectType_ExamStanceReset": 48,
    "ProduceExamEffectType_ExamFullPowerPoint": 49,
    "ProduceExamEffectType_ExamForecast": 50,
    "ProduceExamEffectType_ExamExtraTurn": 63,
    "ProduceExamEffectType_ExamAntiDebuff": 66,
    "ProduceExamEffectType_ExamStaminaConsumptionAdd": 69,
    "ProduceExamEffectType_ExamStaminaReduce": 60,
    "ProduceExamEffectType_ExamHandGraveCountCardDraw": 98,
    "ProduceExamEffectType_ExamEffectTimer": 103,
    "ProduceExamEffectType_ExamStaminaRecoverMultiple": 117,
    "ProduceExamEffectType_ExamStaminaConsumptionDownFix": 93,
    "ProduceExamEffectType_ExamParameterBuffMultiplePerTurn": 129,
    "ProduceExamEffectType_ExamLessonBuffDependParameterBuff": 130,
    "ProduceExamEffectType_ExamLessonDependParameterBuff": 131,
    "ProduceExamEffectType_ExamLessonAddMultipleParameterBuff": 132,
    "ProduceExamEffectType_ExamLessonDependStaminaConsumptionSum": 144,
    "ProduceExamEffectType_ExamParameterBuffAdditive": 175,
    "ProduceExamEffectType_ExamLessonBuffAdditive": 174,
    "ProduceExamEffectType_ExamReviewAdditive": 177,
    "ProduceExamEffectType_ExamAggressiveAdditive": 176,
    "ProduceExamEffectType_ExamBlockFix": 119,
    "ProduceExamEffectType_ExamReview": 31,
    "ProduceExamEffectType_ExamCardPlayAggressive": 42,
    "ProduceExamEffectType_ExamLessonDependExamReview": 124,
    "ProduceExamEffectType_ExamReviewMultiple": 182,
    "ProduceExamEffectType_ExamReviewReduce": 151,
    "ProduceExamEffectType_ExamBlockDependExamReview": 127,
    "ProduceExamEffectType_ExamBlockAddMultipleAggressive": 143,
    "ProduceExamEffectType_ExamLessonDependExamCardPlayAggressive": 125,
}

SERIALIZED_MOVE_POSITION_ENUM: Final[dict[str, int]] = {
    "ProduceCardMovePositionType_Unknown": 0,
    "ProduceCardMovePositionType_Hand": 1,
    "ProduceCardMovePositionType_DeckFirst": 2,
    "ProduceCardMovePositionType_DeckLast": 3,
    "ProduceCardMovePositionType_DeckRandom": 4,
    "ProduceCardMovePositionType_Grave": 5,
    "ProduceCardMovePositionType_Lost": 6,
    "ProduceCardMovePositionType_Hold": 7,
}
SERIALIZED_PICK_RANGE_ENUM: Final[dict[str, int]] = {
    "ProducePickRangeType_Unknown": 0,
    "ProducePickRangeType_Select": 1,
    "ProducePickRangeType_Random": 2,
    "ProducePickRangeType_All": 3,
}
SERIALIZED_PICK_COUNT_ENUM: Final[dict[str, int]] = {
    "ProducePickCountType_Unknown": 0,
    "ProducePickCountType_Normal": 1,
    "ProducePickCountType_Shortage": 2,
    "ProducePickCountType_Over": 3,
}

_ITEM_FIELDS: Final = frozenset(
    {"_fireCount", "_id", "_itemType", "_parentCustomItemIds", "_reactionCount"}
)
_STATUS_FIELDS: Final = frozenset(
    {"_effectList", "_removedEffectList", "_effectCreateCount", "_idolStatusEffect"}
)
_REFERENCE_FIELDS: Final = frozenset({"version", "RefIds"})
_REFERENCE_ENTRY_FIELDS: Final = frozenset({"rid", "type", "data"})
_REFERENCE_TYPE_FIELDS: Final = frozenset({"asm", "class", "ns"})
_TRIGGER_FIELDS: Final = frozenset({"_id", "_phaseTypeList", "_phaseValueList"})
_TRIGGER_STATUS_FIELDS: Final = frozenset(
    {
        "_descriptionReactiveDataTextList",
        "_descriptionReactiveDataTypeList",
        "_effectList",
        "_fromExamEffectIdList",
        "_isEncoreEnchant",
        "_isFromEnchantEffect",
        "_isItemDirectEnchant",
        "_isPassingTurnStart",
        "_isTurnLimited",
        "_limitCount",
        "_limitCountInTurn",
        "_limitCountInTurnRemain",
        "_originType",
        "_overrideType",
        "_phaseCountDictionary",
        "_startEnchantOriginId",
        "_startEnchantOriginLevel",
        "_startEnchantOriginType",
        "_startEnchantOwnerId",
        "_statusEnchantId",
        "_trigger",
        "_triggerCard",
        "_triggerDrink",
        "_triggerGimmickGroup",
        "_triggerItem",
        "_turn",
        "_turnCount",
        "_uid",
    }
)
_SERIALIZED_EFFECT_FIELDS: Final = frozenset(
    {
        "_cardGrowEffectIdList",
        "_cardSearchId",
        "_cardSearchId2",
        "_chainEffectId",
        "_chainEffectIdList",
        "_effectCount",
        "_effectGroupIdList",
        "_effectTurn",
        "_effectType",
        "_effectValue1",
        "_effectValue2",
        "_id",
        "_judgeTargetIndex",
        "_movePositionType",
        "_pickCount",
        "_pickCountMax",
        "_pickCountMax2",
        "_pickCountMin",
        "_pickCountMin2",
        "_pickCountReferenceProduceCardSearchId",
        "_pickCountReferenceProduceCardSearchId2",
        "_pickCountType",
        "_pickCountType2",
        "_pickRangeType",
        "_pickRangeType2",
        "_statusEnchantId",
        "_targetExamEffectType",
        "_targetProduceCardId",
        "_targetUpgradeCount",
    }
)

_EMPTY_TRIGGER_CARD: Final[dict[str, object]] = {
    "_affectGrowEffectIdList": [],
    "_baseUpgradeCount": 0,
    "_cardData": {
        "_customizeCountList": [],
        "_id": "",
        "_produceCardSkinAssetId": "",
        "_produceCardSkinId": "",
        "_upgradeCount": 0,
    },
    "_fixedDeckOrder": 0,
    "_growEffectExamStartAfterList": [],
    "_guid": "",
    "_isMoveProduceExamEffectUseInTurn": False,
    "_playCount": 0,
    "_staminaConsumptionSpecifyEffectList": [],
    "_statusEffect": {
        "_effectGroupIdList": [],
        "_id": "",
        "_phaseCountDictionary": {"_list": []},
        "_produceCardGrowEffectIdList": [],
        "_produceExamTriggerId": "",
        "_spendCount": 0,
        "_spendTurn": 0,
        "_triggerCount": 0,
    },
    "_supportUpgradeIdList": [],
    "_tmpUpgradeCount": 0,
}
_EMPTY_TRIGGER_GIMMICK: Final[dict[str, object]] = {
    "_gimmickEffect": {"_effectId": ""},
    "_gimmickTrigger": {
        "_id": "",
        "_phaseTypeList": [],
        "_phaseValueList": [],
    },
    "_id": "",
    "_isPositive": False,
    "_priority": 0,
    "_remainingTurn": 0,
    "_remainingTurnPermil": 0,
    "_startTurn": 0,
}
_CONFLICTING_SEARCH: Final = object()


class Plan1PItemRuntimeError(ValueError):
    """Typed parse, graph, or runtime error with a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        if type(code) is not str or not code:
            raise ValueError("runtime error code must be non-empty text")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


# Graph callers commonly import the error under the more specific name used
# by the Plan 2 graph module.  Keep one error type so parse/restore paths
# remain interchangeable without weakening validation.
Plan1PItemEffectGraphError = Plan1PItemRuntimeError


@dataclass(frozen=True, slots=True)
class Plan1PItemRuntimeBlocker:
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if type(self.code) is not str or not self.code:
            raise ValueError("blocker code must be non-empty text")
        if type(self.detail) is not str:
            raise TypeError("blocker detail must be text")

    def __str__(self) -> str:
        return f"{self.code}:{self.detail}" if self.detail else self.code


def _block(code: str, detail: str = "") -> Plan1PItemRuntimeBlocker:
    return Plan1PItemRuntimeBlocker(code, detail)


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise Plan1PItemRuntimeError("invalid-text", label)
    return value


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise Plan1PItemRuntimeError("invalid-integer", label)
    if minimum is not None and value < minimum:
        raise Plan1PItemRuntimeError("integer-below-minimum", label)
    return value


def _array(value: object, label: str) -> tuple[object, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise Plan1PItemRuntimeError("invalid-json", label) from error
    if not isinstance(value, (list, tuple)):
        raise Plan1PItemRuntimeError("invalid-array", label)
    return tuple(value)


def _text_tuple(value: object, label: str, *, empty: bool = False) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{label}[{index}]", empty=empty)
        for index, item in enumerate(_array(value, label))
    )


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    return tuple(
        _integer(item, f"{label}[{index}]")
        for index, item in enumerate(_array(value, label))
    )


def _same_json(actual: object, expected: object) -> bool:
    """Compare JSON values without Python's bool/int equivalence."""

    if type(actual) is not type(expected):
        return False
    if isinstance(actual, Mapping):
        return set(actual) == set(expected) and all(
            _same_json(actual[key], expected[key])  # type: ignore[index]
            for key in actual
        )
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(  # type: ignore[arg-type]
            _same_json(left, right)
            for left, right in zip(actual, expected, strict=True)  # type: ignore[arg-type]
        )
    return actual == expected


@dataclass(frozen=True, slots=True)
class Plan1PItemEffectNode:
    """One Master effect row, including ordered nested references."""

    id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str = ""
    chain_effect_id: str = ""
    children: tuple["Plan1PItemEffectNode", ...] = ()
    status_enchant: "Plan1PItemStatusGraph | None" = None
    raw_payload: tuple[tuple[str, object], ...] = ()
    blockers: tuple[Plan1PItemRuntimeBlocker, ...] = ()

    def __post_init__(self) -> None:
        _text(self.id, "effect.id")
        _text(self.effect_type, f"{self.id}.effect_type")
        for name in ("value1", "value2", "effect_count", "effect_turn"):
            _integer(getattr(self, name), f"{self.id}.{name}")
        _text(self.status_enchant_id, f"{self.id}.status_enchant_id", empty=True)
        _text(self.chain_effect_id, f"{self.id}.chain_effect_id", empty=True)
        children = tuple(self.children)
        if any(not isinstance(value, Plan1PItemEffectNode) for value in children):
            raise TypeError("children must contain Plan1PItemEffectNode values")
        if self.chain_effect_id:
            if len(children) != 1 or children[0].id != self.chain_effect_id:
                raise Plan1PItemRuntimeError("effect-child-order", self.id)
        elif children:
            raise Plan1PItemRuntimeError("effect-child-reference-missing", self.id)
        if self.status_enchant is not None:
            if not isinstance(self.status_enchant, Plan1PItemStatusGraph):
                raise TypeError("status_enchant must be Plan1PItemStatusGraph")
            if self.status_enchant.id != self.status_enchant_id:
                raise Plan1PItemRuntimeError("effect-status-id-drift", self.id)
        if self.status_enchant_id and self.status_enchant is None:
            raise Plan1PItemRuntimeError("effect-status-unresolved", self.id)
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan1PItemRuntimeBlocker) for value in blockers):
            raise TypeError("blockers must contain Plan1PItemRuntimeBlocker values")
        object.__setattr__(self, "children", children)
        object.__setattr__(self, "blockers", blockers)
        object.__setattr__(self, "raw_payload", tuple(self.raw_payload))

    @property
    def executable(self) -> bool:
        return not self.blockers

    @property
    def nested(self) -> tuple["Plan1PItemEffectNode", ...]:
        return self.children


@dataclass(frozen=True, slots=True)
class Plan1PItemStatusGraph:
    id: str
    trigger_id: str
    trigger: CompositeTriggerSpec
    effect_ids: tuple[str, ...]
    effects: tuple[Plan1PItemEffectNode, ...]

    def __post_init__(self) -> None:
        _text(self.id, "status.id")
        _text(self.trigger_id, f"{self.id}.trigger_id")
        if not isinstance(self.trigger, CompositeTriggerSpec):
            raise TypeError("trigger must be CompositeTriggerSpec")
        ids = tuple(self.effect_ids)
        if tuple(effect.id for effect in self.effects) != ids:
            raise Plan1PItemRuntimeError("status-effect-order", self.id)
        if len(ids) != len(set(ids)):
            raise Plan1PItemRuntimeError("status-effect-duplicate", self.id)
        if any(not isinstance(value, Plan1PItemEffectNode) for value in self.effects):
            raise TypeError("effects must contain Plan1PItemEffectNode values")
        object.__setattr__(self, "effect_ids", ids)
        object.__setattr__(self, "effects", tuple(self.effects))

    @property
    def supported(self) -> bool:
        return all(effect.executable for effect in self.effects)


@dataclass(frozen=True, slots=True)
class Plan1PItemEnchantmentGraph:
    id: str
    item_effect_id: str
    trigger: CompositeTriggerSpec
    status: Plan1PItemStatusGraph
    max_uses: int
    effect_turn: int

    def __post_init__(self) -> None:
        _text(self.id, "enchantment.id")
        _text(self.item_effect_id, f"{self.id}.item_effect_id")
        if not isinstance(self.trigger, CompositeTriggerSpec):
            raise TypeError("trigger must be CompositeTriggerSpec")
        if not isinstance(self.status, Plan1PItemStatusGraph):
            raise TypeError("status must be Plan1PItemStatusGraph")
        _integer(self.max_uses, f"{self.id}.max_uses", minimum=-1)
        _integer(self.effect_turn, f"{self.id}.effect_turn")

    @property
    def unlimited(self) -> bool:
        return self.max_uses == -1


@dataclass(frozen=True, slots=True)
class Plan1PItemGraph:
    item_id: str
    item_name: str
    plan_type: str
    item_effect_ids: tuple[str, ...]
    enchantments: tuple[Plan1PItemEnchantmentGraph, ...]
    blockers: tuple[Plan1PItemRuntimeBlocker, ...] = ()

    def __post_init__(self) -> None:
        _text(self.item_id, "item_id")
        _text(self.item_name, "item_name", empty=True)
        _text(self.plan_type, "plan_type")
        if self.plan_type not in PLAN1_PITEM_PLAN_TYPES:
            raise Plan1PItemRuntimeError("item-plan-mismatch", self.item_id)
        ids = tuple(self.item_effect_ids)
        if tuple(value.item_effect_id for value in self.enchantments) != ids:
            raise Plan1PItemRuntimeError("item-effect-order", self.item_id)
        if any(not isinstance(value, Plan1PItemEnchantmentGraph) for value in self.enchantments):
            raise TypeError("enchantments must contain Plan1PItemEnchantmentGraph values")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan1PItemRuntimeBlocker) for value in blockers):
            raise TypeError("blockers must contain Plan1PItemRuntimeBlocker values")
        object.__setattr__(self, "item_effect_ids", ids)
        object.__setattr__(self, "enchantments", tuple(self.enchantments))
        object.__setattr__(self, "blockers", blockers)

    @property
    def supported(self) -> bool:
        return not self.blockers and bool(self.enchantments) and all(
            enchantment.status.supported for enchantment in self.enchantments
        )


def _raw_effect_fields(raw: Mapping[str, object], effect_id: str) -> dict[str, object]:
    """Project the exact Master effect payload needed by LocalSave."""

    defaults: dict[str, object] = {
        "id": effect_id,
        "effectType": "",
        "effectValue1": 0,
        "effectValue2": 0,
        "effectCount": 0,
        "effectTurn": 0,
        "targetExamEffectType": "ProduceExamEffectType_Unknown",
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "produceCardSearchId": "",
        "produceCardSearchId2": "",
        "movePositionType": CARD_MOVE_UNKNOWN,
        "pickRangeType": "ProducePickRangeType_Unknown",
        "pickRangeType2": "ProducePickRangeType_Unknown",
        "pickCountType": "ProducePickCountType_Unknown",
        "pickCountType2": "ProducePickCountType_Unknown",
        "pickCountMin": 0,
        "pickCountMax": 0,
        "pickCountMin2": 0,
        "pickCountMax2": 0,
        "pickCountReferenceProduceCardSearchId": "",
        "pickCountReferenceProduceCardSearchId2": "",
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": "",
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
        "effectGroupIds": [],
    }
    aliases = {
        "id": ("id",),
        "effectType": ("effectType", "effect_type"),
        "effectValue1": ("effectValue1", "value1"),
        "effectValue2": ("effectValue2", "value2"),
        "effectCount": ("effectCount", "effect_count"),
        "effectTurn": ("effectTurn", "effect_turn"),
        "targetExamEffectType": ("targetExamEffectType", "target_exam_effect_type"),
        "targetProduceCardId": ("targetProduceCardId", "target_produce_card_id"),
        "targetUpgradeCount": ("targetUpgradeCount", "target_upgrade_count"),
        "produceCardSearchId": ("produceCardSearchId", "produce_card_search_id"),
        "produceCardSearchId2": ("produceCardSearchId2", "produce_card_search_id2"),
        "movePositionType": ("movePositionType", "move_position_type"),
        "pickRangeType": ("pickRangeType", "pick_range_type"),
        "pickRangeType2": ("pickRangeType2", "pick_range_type2"),
        "pickCountType": ("pickCountType", "pick_count_type"),
        "pickCountType2": ("pickCountType2", "pick_count_type2"),
        "pickCountMin": ("pickCountMin", "pick_count_min"),
        "pickCountMax": ("pickCountMax", "pick_count_max"),
        "pickCountMin2": ("pickCountMin2", "pick_count_min2"),
        "pickCountMax2": ("pickCountMax2", "pick_count_max2"),
        "pickCountReferenceProduceCardSearchId": (
            "pickCountReferenceProduceCardSearchId",
            "pick_count_reference_search_id",
        ),
        "pickCountReferenceProduceCardSearchId2": (
            "pickCountReferenceProduceCardSearchId2",
            "pick_count_reference_search_id2",
        ),
        "chainProduceExamEffectId": ("chainProduceExamEffectId", "chain_effect_id"),
        "chainProduceExamEffectIds": ("chainProduceExamEffectIds", "chain_effect_ids"),
        "produceExamStatusEnchantId": (
            "produceExamStatusEnchantId",
            "status_enchant_id",
        ),
        "produceCardStatusEnchantId": (
            "produceCardStatusEnchantId",
            "produce_card_status_enchant_id",
        ),
        "produceCardGrowEffectIds": (
            "produceCardGrowEffectIds",
            "produce_card_grow_effect_ids",
        ),
        "effectGroupIds": ("effectGroupIds", "effect_group_ids"),
    }
    result = dict(defaults)
    for target, names in aliases.items():
        for name in names:
            if name in raw:
                result[target] = raw[name]
                break
    return result


def _master_effect_raw(connection: sqlite3.Connection, effect_id: str) -> Mapping[str, object]:
    row = connection.execute(
        "SELECT id, raw_json FROM effect WHERE id = ?", (effect_id,)
    ).fetchone()
    if row is None:
        raise Plan1PItemRuntimeError("effect-not-found", effect_id)
    try:
        raw = json.loads(str(row["raw_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan1PItemRuntimeError("effect-json-invalid", effect_id) from error
    if not isinstance(raw, Mapping):
        raise Plan1PItemRuntimeError("effect-json-object-invalid", effect_id)
    if raw.get("id") != effect_id:
        raise Plan1PItemRuntimeError("effect-id-drift", effect_id)
    return raw


class _GraphLoader:
    def __init__(self, connection: sqlite3.Connection, database: Path) -> None:
        self.connection = connection
        self.database = database
        self.effects: dict[str, Plan1PItemEffectNode] = {}
        self.statuses: dict[str, Plan1PItemStatusGraph] = {}
        self.effect_stack: list[str] = []
        self.status_stack: list[str] = []

    def _trigger(self, trigger_id: str) -> CompositeTriggerSpec:
        row = self.connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
        ).fetchone()
        if row is None:
            raise Plan1PItemRuntimeError("trigger-not-found", trigger_id)
        raw: dict[str, object] = {
            "id": str(row["id"]),
            "phase_types": row["phase_types_json"],
            "phase_values": row["phase_values_json"],
            "field_check_types": row["field_status_check_types_json"],
            "field_types": row["field_status_types_json"],
            "field_values": row["field_status_values_json"],
            "field_card_search_ids": row["field_status_produce_card_search_ids_json"],
            "produce_card_search_id": str(row["produce_card_search_id"]),
            "upper_search_count": int(row["upper_search_count"]),
            "lower_search_count": int(row["lower_search_count"]),
            "card_move_position_type": str(row["card_move_position_type"]),
            "effect_types": row["effect_types_json"],
            "lesson_type": str(row["lesson_type"]),
        }
        search_id = str(raw["produce_card_search_id"])
        rule = (
            None
            if not search_id
            else load_produce_card_search(search_id, self.database)
        )
        try:
            return parse_composite_trigger(raw, card_search_rule=rule)
        except (CompositeTriggerError, TypeError, ValueError) as error:
            raise Plan1PItemRuntimeError(
                getattr(error, "code", "trigger-parse-error"), str(error)
            ) from error

    def status(self, status_id: str) -> Plan1PItemStatusGraph:
        status_id = _text(status_id, "status_id")
        cached = self.statuses.get(status_id)
        if cached is not None:
            return cached
        if status_id in self.status_stack:
            raise Plan1PItemRuntimeError("status-cycle", status_id)
        self.status_stack.append(status_id)
        try:
            row = self.connection.execute(
                "SELECT * FROM produce_exam_status_enchant WHERE id = ?", (status_id,)
            ).fetchone()
            if row is None:
                raise Plan1PItemRuntimeError("status-not-found", status_id)
            trigger_id = _text(row["produce_exam_trigger_id"], f"{status_id}.trigger_id")
            trigger = self._trigger(trigger_id)
            effect_ids = tuple(
                _text(value, f"{status_id}.effect_ids[{index}]")
                for index, value in enumerate(
                    _array(row["produce_exam_effect_ids_json"], f"{status_id}.effect_ids")
                )
            )
            effects = tuple(self.effect(effect_id) for effect_id in effect_ids)
            result = Plan1PItemStatusGraph(status_id, trigger_id, trigger, effect_ids, effects)
            self.statuses[status_id] = result
            return result
        finally:
            self.status_stack.pop()

    def effect(self, effect_id: str) -> Plan1PItemEffectNode:
        effect_id = _text(effect_id, "effect_id")
        cached = self.effects.get(effect_id)
        if cached is not None:
            return cached
        if effect_id in self.effect_stack:
            raise Plan1PItemRuntimeError("effect-cycle", effect_id)
        self.effect_stack.append(effect_id)
        try:
            raw = _raw_effect_fields(_master_effect_raw(self.connection, effect_id), effect_id)
            raw_id = _text(raw["id"], f"{effect_id}.id")
            if raw_id != effect_id:
                raise Plan1PItemRuntimeError("effect-id-drift", effect_id)
            effect_type = _text(raw["effectType"], f"{effect_id}.effect_type")
            blockers: list[Plan1PItemRuntimeBlocker] = []
            if effect_type not in SERIALIZED_EFFECT_ENUM:
                blockers.append(_block("effect-type-unknown", f"{effect_id}:{effect_type}"))
            values: dict[str, int] = {}
            for name in ("effectValue1", "effectValue2", "effectCount", "effectTurn"):
                value = raw[name]
                if type(value) is not int:
                    blockers.append(_block("effect-field-invalid", f"{effect_id}:{name}"))
                    values[name] = 0
                else:
                    values[name] = value
            status_id = _text(raw["produceExamStatusEnchantId"], f"{effect_id}.status_id", empty=True)
            chain_head = _text(raw["chainProduceExamEffectId"], f"{effect_id}.chain_id", empty=True)
            chain_ids = _text_tuple(raw["chainProduceExamEffectIds"], f"{effect_id}.chain_ids", empty=True)
            if chain_head and chain_ids:
                blockers.append(_block("effect-chain-storage-conflict", effect_id))
            chain_id = chain_head or (chain_ids[0] if len(chain_ids) == 1 else "")
            if len(chain_ids) > 1:
                blockers.append(_block("effect-chain-cardinality-unsupported", effect_id))
            children = (self.effect(chain_id),) if chain_id else ()
            status = self.status(status_id) if status_id else None
            if raw["produceCardStatusEnchantId"]:
                blockers.append(
                    _block("effect-card-status-enchant-unsupported", effect_id)
                )
            for name in (
                "targetProduceCardId",
                "produceCardSearchId",
                "produceCardSearchId2",
                "movePositionType",
                "pickRangeType",
                "pickRangeType2",
                "pickCountType",
                "pickCountType2",
                "pickCountReferenceProduceCardSearchId",
                "pickCountReferenceProduceCardSearchId2",
            ):
                if type(raw[name]) is not str:
                    blockers.append(_block("effect-field-invalid", f"{effect_id}:{name}"))
            for name in (
                "targetUpgradeCount",
                "pickCountMin",
                "pickCountMax",
                "pickCountMin2",
                "pickCountMax2",
            ):
                if type(raw[name]) is not int or raw[name] < 0:
                    blockers.append(_block("effect-field-invalid", f"{effect_id}:{name}"))
            for name in ("chainProduceExamEffectIds", "produceCardGrowEffectIds", "effectGroupIds"):
                try:
                    _text_tuple(raw[name], f"{effect_id}.{name}", empty=False)
                except Plan1PItemRuntimeError:
                    blockers.append(_block("effect-array-invalid", f"{effect_id}:{name}"))
            if effect_type in {
                "ProduceExamEffectType_ExamParameterBuff",
                "ProduceExamEffectType_ExamBlockFix",
                "ProduceExamEffectType_ExamBlock",
                "ProduceExamEffectType_ExamPlayableValueAdd",
                "ProduceExamEffectType_ExamLessonBuff",
            }:
                if effect_type == "ProduceExamEffectType_ExamParameterBuff":
                    valid = (
                        values["effectValue1"] == 0
                        and values["effectValue2"] == 0
                        and values["effectCount"] == 0
                        and values["effectTurn"] >= 0
                    )
                elif effect_type in {"ProduceExamEffectType_ExamBlockFix", "ProduceExamEffectType_ExamBlock"}:
                    valid = (
                        values["effectValue1"] >= 0
                        and values["effectValue2"] == 0
                        and values["effectCount"] == 0
                        and values["effectTurn"] == 0
                    )
                elif effect_type == "ProduceExamEffectType_ExamPlayableValueAdd":
                    valid = (
                        values["effectValue1"] == 0
                        and values["effectValue2"] == 0
                        and values["effectCount"] > 0
                        and values["effectTurn"] == 0
                    )
                else:
                    valid = (
                        values["effectValue1"] > 0
                        and values["effectValue2"] == 0
                        and values["effectCount"] == 0
                        and values["effectTurn"] == 0
                    )
                if not valid:
                    blockers.append(_block("effect-shape-unsupported", effect_id))
            else:
                # The generic graph still records every current Master type,
                # but this first Plan 1 scalar executor only owns the two
                # effect families proven at the card transaction boundary.
                blockers.append(_block("effect-runtime-unsupported", f"{effect_id}:{effect_type}"))
            node = Plan1PItemEffectNode(
                id=effect_id,
                effect_type=effect_type,
                value1=values["effectValue1"],
                value2=values["effectValue2"],
                effect_count=values["effectCount"],
                effect_turn=values["effectTurn"],
                status_enchant_id=status_id,
                chain_effect_id=chain_id,
                children=children,
                status_enchant=status,
                raw_payload=tuple(sorted(raw.items(), key=lambda item: item[0])),
                blockers=tuple(dict.fromkeys(blockers)),
            )
            self.effects[effect_id] = node
            return node
        finally:
            self.effect_stack.pop()


def load_plan1_pitem_graph(
    item_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan1PItemGraph:
    """Load one Plan 1 item graph from Master, retaining all source order."""

    item_id = _text(item_id, "item_id")
    path = Path(database).resolve()
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, name, plan_type, fire_limit, fire_interval, "
            "produce_trigger_id, produce_trigger_ids_json, "
            "produce_item_effect_ids_json, is_exam_effect FROM produce_item WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise Plan1PItemRuntimeError("item-not-found", item_id)
        blockers: list[Plan1PItemRuntimeBlocker] = []
        plan_type = str(row["plan_type"])
        if plan_type not in PLAN1_PITEM_PLAN_TYPES:
            blockers.append(_block("item-plan-mismatch", f"{item_id}:{plan_type}"))
        if row["fire_limit"] != 0 or row["fire_interval"] != 0 or row["is_exam_effect"] != 1:
            blockers.append(_block("item-lifecycle-shape-unsupported", item_id))
        if row["produce_trigger_id"] != "":
            blockers.append(_block("item-produce-trigger-unsupported", item_id))
        try:
            trigger_ids = _text_tuple(row["produce_trigger_ids_json"], f"{item_id}.produce_trigger_ids", empty=True)
        except Plan1PItemRuntimeError:
            trigger_ids = ()
            blockers.append(_block("item-trigger-list-invalid", item_id))
        if trigger_ids:
            blockers.append(_block("item-produce-trigger-unsupported", item_id))
        effect_ids = tuple(
            _text(value, f"{item_id}.item_effect_ids[{index}]")
            for index, value in enumerate(
                _array(row["produce_item_effect_ids_json"], f"{item_id}.item_effect_ids")
            )
        )
        loader = _GraphLoader(connection, path)
        enchantments: list[Plan1PItemEnchantmentGraph] = []
        for item_effect_id in effect_ids:
            effect_row = connection.execute(
                "SELECT effect_type, effect_turn, effect_count, "
                "produce_exam_status_enchant_id FROM produce_item_effect WHERE id = ?",
                (item_effect_id,),
            ).fetchone()
            if effect_row is None:
                blockers.append(_block("item-effect-not-found", item_effect_id))
                continue
            if effect_row["effect_type"] != ITEM_EFFECT_STATUS_ENCHANT:
                blockers.append(_block("item-effect-type-unsupported", f"{item_effect_id}:{effect_row['effect_type']}"))
                continue
            status_id = effect_row["produce_exam_status_enchant_id"]
            try:
                status = loader.status(_text(status_id, f"{item_effect_id}.status_id"))
                enchantments.append(
                    Plan1PItemEnchantmentGraph(
                        id=status.id,
                        item_effect_id=item_effect_id,
                        trigger=status.trigger,
                        status=status,
                        # Item-effect count zero is the native unlimited-use
                        # sentinel; LocalSave materializes it as -1.
                        max_uses=(
                            -1
                            if effect_row["effect_count"] == 0
                            else _integer(
                                effect_row["effect_count"],
                                f"{item_effect_id}.effect_count",
                                minimum=1,
                            )
                        ),
                        effect_turn=_integer(effect_row["effect_turn"], f"{item_effect_id}.effect_turn"),
                    )
                )
            except (Plan1PItemRuntimeError, TypeError, ValueError) as error:
                blockers.append(_block(getattr(error, "code", "item-graph-invalid"), str(error)))
        return Plan1PItemGraph(
            item_id=item_id,
            item_name=str(row["name"]),
            plan_type=plan_type,
            item_effect_ids=effect_ids,
            enchantments=tuple(enchantments),
            blockers=tuple(dict.fromkeys(blockers)),
        )


def resolve_plan1_pitem_graph(
    item_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
) -> tuple[Plan1PItemGraph | None, tuple[Plan1PItemRuntimeBlocker, ...]]:
    """Non-throwing graph entry point for unattended callers."""

    try:
        graph = load_plan1_pitem_graph(item_id, database=database)
    except (Plan1PItemRuntimeError, OSError, sqlite3.Error, TypeError, ValueError) as error:
        return None, (_block(getattr(error, "code", "item-graph-load-failed"), str(error)),)
    return graph, graph.blockers


# ---------------------------------------------------------------------------
# Generic Master trigger/event dispatch

SearchEvidence = Literal["bool", "counted"]


@dataclass(frozen=True, slots=True)
class Plan1PItemCardSearchEvidence:
    search_id: str
    matched: bool
    result_count: int | None = None
    card_id: str = ""
    card_upgrade: int | None = None

    def __post_init__(self) -> None:
        _text(self.search_id, "search_id")
        if type(self.matched) is not bool:
            raise Plan1PItemRuntimeError("search-match-invalid", self.search_id)
        if self.result_count is not None:
            _integer(self.result_count, "result_count", minimum=0)
        _text(self.card_id, "card_id", empty=True)
        if self.card_upgrade is not None:
            _integer(self.card_upgrade, "card_upgrade", minimum=0)
        if bool(self.card_id) != (self.card_upgrade is not None):
            raise Plan1PItemRuntimeError("search-card-identity-incomplete", self.search_id)


@dataclass(frozen=True, slots=True)
class Plan1PItemEventContext:
    """Typed evidence at one native event boundary."""

    phase: str | PItemEventPhase
    phase_values: tuple[int, ...] = ()
    field_values: Mapping[str, object] = field(default_factory=dict)
    field_search_values: Mapping[tuple[str, str], object] = field(default_factory=dict)
    card_search_matches: Mapping[str, object] = field(default_factory=dict)
    # Plan2's first generic callers used ``card_searches``; retain the
    # spelling as an input alias while exposing one merged map to evaluation.
    card_searches: Mapping[str, object] = field(default_factory=dict)
    card_id: str = ""
    card_upgrade: int | None = None
    card_category: str = ""
    card_effect_group_ids: tuple[str, ...] = ()
    effect_types: tuple[str, ...] = ()
    card_move_position_type: str = CARD_MOVE_UNKNOWN
    lesson_type: str = LESSON_UNKNOWN

    def __post_init__(self) -> None:
        phase = self.phase.value if isinstance(self.phase, PItemEventPhase) else self.phase
        _text(phase, "context.phase")
        if phase not in KNOWN_PHASES:
            raise Plan1PItemRuntimeError("unsupported-phase", phase)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "phase_values", tuple(
            _integer(value, f"context.phase_values[{index}]", minimum=0)
            for index, value in enumerate(self.phase_values)
        ))
        for name in (
            "field_values",
            "field_search_values",
            "card_search_matches",
            "card_searches",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping")
            object.__setattr__(self, name, dict(value))
        for key in self.field_values:
            _text(key, "context.field_values key")
        for key in self.field_search_values:
            if not isinstance(key, tuple) or len(key) != 2:
                raise Plan1PItemRuntimeError("field-search-key-invalid", repr(key))
            _text(key[0], "context.field_search_values field")
            _text(key[1], "context.field_search_values search")
        merged_searches = dict(self.card_searches)
        for search_id, value in self.card_search_matches.items():
            if search_id in merged_searches and merged_searches[search_id] != value:
                merged_searches[search_id] = _CONFLICTING_SEARCH
            else:
                merged_searches[search_id] = value
        object.__setattr__(self, "card_searches", merged_searches)
        object.__setattr__(self, "card_search_matches", merged_searches)
        _text(self.card_id, "context.card_id", empty=True)
        if self.card_upgrade is not None:
            _integer(self.card_upgrade, "context.card_upgrade", minimum=0)
        _text(self.card_category, "context.card_category", empty=True)
        groups = tuple(self.card_effect_group_ids)
        if any(type(value) is not str or not value for value in groups):
            raise Plan1PItemRuntimeError("card-effect-group-invalid", self.card_id)
        object.__setattr__(self, "card_effect_group_ids", groups)
        effects = tuple(self.effect_types)
        if any(type(value) is not str or not value for value in effects):
            raise Plan1PItemRuntimeError("effect-filter-invalid", self.phase)
        object.__setattr__(self, "effect_types", effects)
        _text(self.card_move_position_type, "context.card_move_position_type")
        if self.lesson_type not in KNOWN_LESSON_TYPES:
            raise Plan1PItemRuntimeError("lesson-type-invalid", self.lesson_type)


@dataclass(frozen=True, slots=True)
class Plan1PItemEvent:
    """Convenience event DTO mirroring the native item-event boundary.

    ``Plan1PItemEventContext`` is the normalized predicate context.  This
    companion accepts the scalar spellings commonly emitted by a replay
    recorder and projects only explicitly supplied values into that context;
    it never fills an absent predicate with zero.
    """

    phase: str | PItemEventPhase
    round_number: int = 1
    card_id: str = ""
    card_upgrade: int = 0
    card_category: str = ""
    card_effect_group_ids: tuple[str, ...] = ()
    phase_values: tuple[int, ...] = ()
    field_values: Mapping[str, object] | None = None
    field_search_values: Mapping[tuple[str, str], object] | None = None
    card_search_matches: Mapping[str, object] | None = None
    effect_types: tuple[str, ...] = ()
    card_move_position_type: str = CARD_MOVE_UNKNOWN
    lesson_type: str = LESSON_UNKNOWN
    review: int | None = None
    block: int | None = None
    remaining_turn: int | None = None
    stamina_multiple: int | None = None
    aggressive: int | None = None
    changed_effect_type: str = ""

    def __post_init__(self) -> None:
        phase = self.phase.value if isinstance(self.phase, PItemEventPhase) else self.phase
        _text(phase, "phase")
        object.__setattr__(self, "phase", phase)
        _integer(self.round_number, "round_number", minimum=1)
        _integer(self.card_upgrade, "card_upgrade", minimum=0)
        _text(self.card_id, "card_id", empty=True)
        for name in ("review", "block", "remaining_turn", "stamina_multiple", "aggressive"):
            value = getattr(self, name)
            if value is not None:
                _integer(value, name, minimum=0)
        if not isinstance(self.field_values, (Mapping, type(None))):
            raise TypeError("field_values must be a mapping or None")
        if not isinstance(self.field_search_values, (Mapping, type(None))):
            raise TypeError("field_search_values must be a mapping or None")
        if not isinstance(self.card_search_matches, (Mapping, type(None))):
            raise TypeError("card_search_matches must be a mapping or None")

    def to_context(self) -> Plan1PItemEventContext:
        fields = {} if self.field_values is None else dict(self.field_values)
        aliases = {
            "ProduceExamFieldStatusType_ReviewUp": self.review,
            "ProduceExamFieldStatusType_BlockUp": self.block,
            "ProduceExamFieldStatusType_RemainingTurn": self.remaining_turn,
            "ProduceExamFieldStatusType_StaminaUpMultiple": self.stamina_multiple,
            "ProduceExamFieldStatusType_CardPlayAggressiveUp": self.aggressive,
        }
        for key, value in aliases.items():
            if value is not None:
                fields.setdefault(key, value)
        effects = self.effect_types
        if not effects and self.changed_effect_type:
            effects = (self.changed_effect_type,)
        return Plan1PItemEventContext(
            phase=self.phase,
            phase_values=self.phase_values,
            field_values=fields,
            field_search_values=(
                {} if self.field_search_values is None else self.field_search_values
            ),
            card_search_matches=(
                {} if self.card_search_matches is None else self.card_search_matches
            ),
            card_id=self.card_id,
            card_upgrade=self.card_upgrade if self.card_id else None,
            card_category=self.card_category,
            card_effect_group_ids=self.card_effect_group_ids,
            effect_types=effects,
            card_move_position_type=self.card_move_position_type,
            lesson_type=self.lesson_type,
        )


@dataclass(frozen=True, slots=True)
class Plan1PItemCardSearchReference:
    search_id: str
    kind: Literal["field", "produce"]
    field_index: int | None = None
    upper_count: int = 0
    lower_count: int = 0
    rule: ProduceCardSearchRule | None = None

    def __post_init__(self) -> None:
        _text(self.search_id, "search_id")
        if self.kind not in ("field", "produce"):
            raise Plan1PItemRuntimeError("search-reference-kind-invalid", self.search_id)
        if self.kind == "field":
            if type(self.field_index) is not int or self.field_index < 0:
                raise Plan1PItemRuntimeError("field-search-index-invalid", self.search_id)
        elif self.field_index is not None:
            raise Plan1PItemRuntimeError("produce-search-index-invalid", self.search_id)
        _integer(self.upper_count, "upper_count", minimum=0)
        _integer(self.lower_count, "lower_count", minimum=0)
        if self.rule is not None and not isinstance(self.rule, ProduceCardSearchRule):
            raise TypeError("rule must be ProduceCardSearchRule or None")


@dataclass(frozen=True, slots=True)
class Plan1PItemEventDispatch:
    trigger: CompositeTriggerSpec
    search_references: tuple[Plan1PItemCardSearchReference, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.trigger, CompositeTriggerSpec):
            raise TypeError("trigger must be CompositeTriggerSpec")
        refs = tuple(self.search_references)
        expected: list[tuple[str, int | None]] = [
            (predicate.card_search_id, index)
            for index, predicate in enumerate(self.trigger.fields)
            if predicate.card_search_id
        ]
        if self.trigger.produce_card_search_id:
            expected.append((self.trigger.produce_card_search_id, None))
        if [(ref.search_id, ref.field_index) for ref in refs] != expected:
            raise Plan1PItemRuntimeError("search-reference-order", self.trigger.trigger_id)
        object.__setattr__(self, "search_references", refs)

    @property
    def trigger_id(self) -> str:
        return self.trigger.trigger_id

    @property
    def phase(self) -> str:
        return self.trigger.phase

    @property
    def master_order(self) -> tuple[str, ...]:
        return tuple(
            [f"field[{index}]" for index in range(len(self.trigger.fields))]
            + (["produce-card-search"] if self.trigger.produce_card_search_id else [])
        )


@dataclass(frozen=True, slots=True)
class Plan1PItemEventDispatchEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None


def _validate_dispatch_shape(trigger: CompositeTriggerSpec) -> None:
    if trigger.phase not in KNOWN_PHASES:
        raise Plan1PItemRuntimeError("unsupported-phase", trigger.phase)
    for index, predicate in enumerate(trigger.fields):
        if predicate.field_type not in KNOWN_PITEM_FIELD_TYPES:
            raise Plan1PItemRuntimeError(
                "field-type-unknown", f"{trigger.trigger_id}:{index}:{predicate.field_type}"
            )
        if predicate.check_type not in (CHECK_DEFAULT, CHECK_NOT):
            raise Plan1PItemRuntimeError("check-type-invalid", trigger.trigger_id)
        if predicate.value is not None and type(predicate.value) is not int:
            raise Plan1PItemRuntimeError("field-threshold-invalid", trigger.trigger_id)
    for effect_type in trigger.effect_types:
        if effect_type not in KNOWN_PITEM_EFFECT_TYPES:
            raise Plan1PItemRuntimeError(
                "trigger-effect-type-unknown", f"{trigger.trigger_id}:{effect_type}"
            )
    if trigger.produce_card_search_id:
        if trigger.upper_search_count < 0 or trigger.lower_search_count < 0:
            raise Plan1PItemRuntimeError("search-count-invalid", trigger.trigger_id)
    elif trigger.upper_search_count or trigger.lower_search_count not in (0, 1):
        raise Plan1PItemRuntimeError("search-count-without-search", trigger.trigger_id)


def parse_plan1_pitem_event_dispatch(
    raw: object,
    *,
    card_search_rule: ProduceCardSearchRule | None = None,
    field_card_search_rules: Mapping[str, ProduceCardSearchRule] | None = None,
) -> Plan1PItemEventDispatch:
    """Parse one Master trigger into an ID-independent event contract."""

    try:
        if isinstance(raw, CompositeTriggerSpec):
            trigger = raw
            if (
                card_search_rule is not None
                and trigger.card_search_rule is not None
                and trigger.card_search_rule != card_search_rule
            ):
                raise Plan1PItemRuntimeError(
                    "card-search-rule-drift", trigger.trigger_id
                )
            if card_search_rule is not None and trigger.card_search_rule is None:
                trigger = replace(trigger, card_search_rule=card_search_rule)
        else:
            trigger = parse_composite_trigger(raw, card_search_rule=card_search_rule)
    except (CompositeTriggerError, TypeError, ValueError) as error:
        raise Plan1PItemRuntimeError(getattr(error, "code", "trigger-parse-error"), str(error)) from error
    _validate_dispatch_shape(trigger)
    refs: list[Plan1PItemCardSearchReference] = []
    rules = field_card_search_rules
    if rules is not None and not isinstance(rules, Mapping):
        raise TypeError("field_card_search_rules must be a mapping or None")
    expected_field_ids = {
        predicate.card_search_id for predicate in trigger.fields if predicate.card_search_id
    }
    if rules is not None and set(rules).difference(expected_field_ids):
        raise Plan1PItemRuntimeError("field-search-rule-extra", repr(set(rules).difference(expected_field_ids)))
    for index, predicate in enumerate(trigger.fields):
        if not predicate.card_search_id:
            continue
        rule = None if rules is None else rules.get(predicate.card_search_id)
        if rules is not None and rule is None:
            raise Plan1PItemRuntimeError("field-search-rule-missing", predicate.card_search_id)
        refs.append(Plan1PItemCardSearchReference(predicate.card_search_id, "field", index, rule=rule))
    if trigger.produce_card_search_id:
        refs.append(
            Plan1PItemCardSearchReference(
                trigger.produce_card_search_id,
                "produce",
                upper_count=trigger.upper_search_count,
                lower_count=trigger.lower_search_count,
                rule=trigger.card_search_rule,
            )
        )
    return Plan1PItemEventDispatch(trigger, tuple(refs))


def resolve_plan1_pitem_event_dispatch(
    raw: object,
    *,
    card_search_rule: ProduceCardSearchRule | None = None,
    field_card_search_rules: Mapping[str, ProduceCardSearchRule] | None = None,
) -> tuple[Plan1PItemEventDispatch | None, tuple[Plan1PItemRuntimeBlocker, ...]]:
    try:
        return parse_plan1_pitem_event_dispatch(
            raw,
            card_search_rule=card_search_rule,
            field_card_search_rules=field_card_search_rules,
        ), ()
    except (Plan1PItemRuntimeError, TypeError, ValueError) as error:
        return None, (_block(getattr(error, "code", "dispatch-parse-failed"), str(error)),)


def _match_playing_search(
    rule: ProduceCardSearchRule,
    context: Plan1PItemEventContext,
) -> tuple[bool | None, str | None]:
    """Evaluate a selected Playing card against a neutral Master search."""

    if rule.card_position_type != "ProduceCardPositionType_Playing":
        return False, f"search-position-unsupported:{rule.id}"
    neutral = (
        not rule.card_rarities
        and rule.plan_type == "ProducePlanType_Unknown"
        and rule.card_status_type == "ProduceCardSearchStatusType_Unknown"
        and rule.order_type == "ProduceCardOrderType_Unknown"
        and not rule.card_search_tag
        and not rule.produce_card_random_pool_id
        and rule.stamina_min_max_type == "ConditionMinMaxType_Unknown"
        and rule.stamina_min == 0
        and rule.stamina_max == 0
        and rule.exam_effect_type == "ProduceExamEffectType_Unknown"
        and not rule.is_self
        and not rule.produce_card_pool_id
        and rule.cost_type == "ExamCostType_Unknown"
        and not rule.is_customized
        and rule.limit_count in (0, 1)
    )
    if not neutral:
        return False, f"playing-search-shape:{rule.id}"
    if context.card_id == "" or context.card_upgrade is None:
        return None, "playing-search-card-evidence-missing"
    if rule.produce_card_ids and context.card_id not in rule.produce_card_ids:
        return False, None
    if rule.upgrade_counts and context.card_upgrade not in rule.upgrade_counts:
        return False, None
    if rule.card_categories and context.card_category not in rule.card_categories:
        return False, None
    if rule.effect_group_ids and not all(
        value in context.card_effect_group_ids for value in rule.effect_group_ids
    ):
        return False, None
    if not context.card_category:
        return None, "playing-search-card-category-missing"
    return True, None


def _search_match(
    reference: Plan1PItemCardSearchReference,
    context: Plan1PItemEventContext,
) -> tuple[bool | None, tuple[str, ...]]:
    evidence = context.card_search_matches.get(reference.search_id)
    if evidence is _CONFLICTING_SEARCH:
        return None, ("search-evidence-conflict",)
    # A recorder-provided boolean is useful evidence, but it cannot override
    # the already-bound card identity at this event boundary.  Reconcile both
    # sources whenever the Master search rule can be evaluated locally; this
    # prevents a stale ``True`` map entry from firing an item for a different
    # currently-playing card.
    direct_match: bool | None = None
    direct_issue: str | None = None
    if (
        reference.kind == "produce"
        and reference.rule is not None
        and context.card_id
        and context.card_upgrade is not None
    ):
        if reference.rule.card_position_type == "ProduceCardPositionType_Target":
            direct_match, direct_issue = match_exact_target_card_context_search(
                reference.rule,
                card_id=context.card_id,
                upgrade_count=context.card_upgrade,
                card_category=context.card_category,
                card_effect_group_ids=context.card_effect_group_ids,
            )
        elif reference.rule.card_position_type == "ProduceCardPositionType_Playing":
            direct_match, direct_issue = _match_playing_search(reference.rule, context)
        if direct_issue is not None:
            return None, (f"search-shape:{direct_issue}",)
        if direct_match is False:
            return False, ()
    if isinstance(evidence, Plan1PItemCardSearchEvidence):
        if evidence.search_id != reference.search_id:
            return None, ("search-reference-mismatch",)
        if reference.rule is not None and evidence.card_id:
            if reference.rule.card_position_type == "ProduceCardPositionType_Target":
                matched, issue = match_exact_target_card_context_search(
                    reference.rule,
                    card_id=evidence.card_id,
                    upgrade_count=(
                        evidence.card_upgrade
                        if evidence.card_upgrade is not None
                        else -1
                    ),
                    card_category=context.card_category,
                    card_effect_group_ids=context.card_effect_group_ids,
                )
                if issue is not None:
                    return None, (f"search-shape:{issue}",)
                if matched is not evidence.matched:
                    return None, ("search-evidence-conflict",)
        if reference.lower_count and (
            evidence.result_count is not None
            and evidence.result_count < reference.lower_count
        ):
            return False, ("search-result-below-lower",)
        if reference.upper_count and (
            evidence.result_count is not None
            and evidence.result_count > reference.upper_count
        ):
            return False, ("search-result-above-upper",)
        if (reference.lower_count > 1 or reference.upper_count > 1) and evidence.result_count is None:
            return None, ("search-result-count-missing",)
        if direct_match is not None and direct_match is not evidence.matched:
            return None, ("search-evidence-conflict",)
        return evidence.matched, ()
    if type(evidence) is bool:
        if reference.lower_count > 1 or reference.upper_count > 1:
            return None, ("search-result-count-missing",)
        if direct_match is not None and direct_match is not evidence:
            return None, ("search-evidence-conflict",)
        return evidence, ()
    if reference.kind == "produce" and reference.rule is not None:
        if reference.rule.card_position_type == "ProduceCardPositionType_Target":
            if not context.card_id or context.card_upgrade is None:
                return None, ("card-search-evidence-missing",)
            matched, issue = match_exact_target_card_context_search(
                reference.rule,
                card_id=context.card_id,
                upgrade_count=context.card_upgrade,
                card_category=context.card_category,
                card_effect_group_ids=context.card_effect_group_ids,
            )
            return (None, (f"search-shape:{issue}",)) if issue else (matched, ())
        matched, issue = _match_playing_search(reference.rule, context)
        return matched, (() if issue is None else (issue,))
    return None, ("card-search-evidence-missing",)


def _field_value(
    predicate: CompositeFieldPredicate,
    context: Plan1PItemEventContext,
) -> tuple[object, bool]:
    if predicate.card_search_id:
        key = (predicate.field_type, predicate.card_search_id)
        if key in context.field_search_values:
            return context.field_search_values[key], True
        return None, False
    if predicate.field_type in context.field_values:
        return context.field_values[predicate.field_type], True
    return None, False


def _evaluate_field(predicate: CompositeFieldPredicate, actual: object) -> tuple[bool | None, str | None]:
    if predicate.value is None:
        if type(actual) is not bool:
            return None, "bool-required"
        result = actual
    else:
        if type(actual) is not int:
            return None, "int-required"
        result = (
            actual <= predicate.value
            if predicate.field_type == "ProduceExamFieldStatusType_RemainingTurn"
            else actual >= predicate.value
        )
    return (not result if predicate.check_not else result), None


def evaluate_plan1_pitem_event_dispatch(
    dispatch: Plan1PItemEventDispatch,
    context: Plan1PItemEventContext,
) -> Plan1PItemEventDispatchEvaluation:
    if not isinstance(dispatch, Plan1PItemEventDispatch):
        raise TypeError("dispatch must be Plan1PItemEventDispatch")
    if not isinstance(context, Plan1PItemEventContext):
        raise TypeError("context must be Plan1PItemEventContext")
    reasons: list[str] = []
    trigger = dispatch.trigger
    if context.phase != trigger.phase:
        return Plan1PItemEventDispatchEvaluation(trigger.trigger_id, True, False)
    if context.phase_values != trigger.phase_values:
        reasons.append("phase-values-mismatch")
    for index, predicate in enumerate(trigger.fields):
        actual, present = _field_value(predicate, context)
        if not present:
            reasons.append(f"field-missing:{index}:{predicate.field_type}")
            continue
        matched, issue = _evaluate_field(predicate, actual)
        if issue:
            reasons.append(f"field-invalid:{index}:{predicate.field_type}:{issue}")
        elif matched is False:
            return Plan1PItemEventDispatchEvaluation(trigger.trigger_id, True, False, tuple(reasons))
    if trigger.produce_card_search_id:
        reference = dispatch.search_references[-1]
        matched, search_reasons = _search_match(reference, context)
        reasons.extend(search_reasons)
        if matched is False:
            return Plan1PItemEventDispatchEvaluation(trigger.trigger_id, True, False, tuple(dict.fromkeys(reasons)))
        if matched is None:
            return Plan1PItemEventDispatchEvaluation(trigger.trigger_id, True, None, tuple(dict.fromkeys(reasons)))
    if trigger.effect_types:
        if not context.effect_types:
            reasons.append("effect-types-evidence-missing")
        elif context.effect_types != trigger.effect_types:
            return Plan1PItemEventDispatchEvaluation(trigger.trigger_id, True, False, tuple(dict.fromkeys(reasons)))
    if trigger.card_move_position_type != CARD_MOVE_UNKNOWN:
        if context.card_move_position_type == CARD_MOVE_UNKNOWN:
            reasons.append("card-move-evidence-missing")
        elif context.card_move_position_type != trigger.card_move_position_type:
            return Plan1PItemEventDispatchEvaluation(trigger.trigger_id, True, False, tuple(dict.fromkeys(reasons)))
    if trigger.lesson_type != LESSON_UNKNOWN:
        if context.lesson_type == LESSON_UNKNOWN:
            reasons.append("lesson-evidence-missing")
        elif context.lesson_type != trigger.lesson_type:
            return Plan1PItemEventDispatchEvaluation(trigger.trigger_id, True, False, tuple(dict.fromkeys(reasons)))
    if reasons:
        return Plan1PItemEventDispatchEvaluation(trigger.trigger_id, True, None, tuple(dict.fromkeys(reasons)))
    return Plan1PItemEventDispatchEvaluation(trigger.trigger_id, True, True)


# ---------------------------------------------------------------------------
# LocalSave graph restoration and scalar effect application


@dataclass(frozen=True, slots=True)
class Plan1LocalSaveItemSource:
    item_id: str
    fire_count: int = 0
    reaction_count: int = 0
    item_type: int = 1
    parent_custom_item_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.item_id, "item_id")
        _integer(self.item_type, "item_type", minimum=0)
        _integer(self.fire_count, "fire_count", minimum=0)
        _integer(self.reaction_count, "reaction_count", minimum=0)
        parents = tuple(self.parent_custom_item_ids)
        if any(type(value) is not str or not value for value in parents):
            raise Plan1PItemRuntimeError("item-parent-id-invalid", self.item_id)
        object.__setattr__(self, "parent_custom_item_ids", parents)


@dataclass(frozen=True, slots=True)
class Plan1LocalSaveActiveReference:
    rid: int
    class_name: str
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        _integer(self.rid, "rid", minimum=1)
        _text(self.class_name, "class_name")
        if not isinstance(self.data, Mapping):
            raise TypeError("data must be a mapping")
        object.__setattr__(self, "data", dict(self.data))


@dataclass(frozen=True, slots=True)
class Plan1LocalSaveItemReferenceGraph:
    item_sources: tuple[Plan1LocalSaveItemSource, ...]
    active_references: tuple[Plan1LocalSaveActiveReference, ...]
    effect_create_count: int = 0
    blockers: tuple[Plan1PItemRuntimeBlocker, ...] = ()

    def __post_init__(self) -> None:
        sources = tuple(self.item_sources)
        refs = tuple(self.active_references)
        if any(not isinstance(value, Plan1LocalSaveItemSource) for value in sources):
            raise TypeError("item_sources must contain Plan1LocalSaveItemSource values")
        if any(not isinstance(value, Plan1LocalSaveActiveReference) for value in refs):
            raise TypeError("active_references must contain Plan1LocalSaveActiveReference values")
        _integer(self.effect_create_count, "effect_create_count", minimum=0)
        object.__setattr__(self, "item_sources", sources)
        object.__setattr__(self, "active_references", refs)
        object.__setattr__(self, "blockers", tuple(self.blockers))

    @property
    def supported(self) -> bool:
        return not self.blockers


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise Plan1PItemRuntimeError("object-required", label)
    if any(type(key) is not str for key in value):
        raise Plan1PItemRuntimeError("object-key-invalid", label)
    return value


def _parse_item_source(raw: object, index: int) -> tuple[Plan1LocalSaveItemSource | None, Plan1PItemRuntimeBlocker | None]:
    label = f"itemList[{index}]"
    try:
        value = _mapping(raw, label)
        if set(value) != _ITEM_FIELDS:
            return None, _block("local-save-item-shape-unsupported", label)
        parents_raw = value["_parentCustomItemIds"]
        parents = tuple(
            _text(item, f"{label}._parentCustomItemIds[{position}]")
            for position, item in enumerate(_array(parents_raw, f"{label}._parentCustomItemIds"))
        )
        source = Plan1LocalSaveItemSource(
            item_id=_text(value["_id"], f"{label}._id"),
            item_type=_integer(value["_itemType"], f"{label}._itemType"),
            fire_count=_integer(value["_fireCount"], f"{label}._fireCount", minimum=0),
            reaction_count=_integer(value["_reactionCount"], f"{label}._reactionCount", minimum=0),
            parent_custom_item_ids=parents,
        )
        if source.item_type != 1 or source.parent_custom_item_ids:
            return None, _block("local-save-item-source-unsupported", source.item_id)
        return source, None
    except (Plan1PItemRuntimeError, TypeError, ValueError) as error:
        return None, _block("local-save-item-field-invalid", f"{label}:{error}")


def _active_refs_from_root(
    status_raw: object,
    references_raw: object,
    blockers: list[Plan1PItemRuntimeBlocker],
) -> tuple[Plan1LocalSaveActiveReference, ...]:
    try:
        status = _mapping(status_raw, "status")
        references = _mapping(references_raw, "references")
        if set(status) != _STATUS_FIELDS:
            blockers.append(_block("local-save-status-shape-unsupported", "status"))
            return ()
        if set(references) != _REFERENCE_FIELDS:
            blockers.append(_block("local-save-references-shape-unsupported", "references"))
            return ()
        links = _array(status["_effectList"], "status._effectList")
        link_ids: list[int] = []
        for index, raw_link in enumerate(links):
            link = _mapping(raw_link, f"status._effectList[{index}]")
            if set(link) != {"rid"}:
                raise Plan1PItemRuntimeError("active-link-shape-invalid", str(index))
            link_ids.append(_integer(link["rid"], f"status._effectList[{index}].rid", minimum=1))
        by_rid: dict[int, Plan1LocalSaveActiveReference] = {}
        entries = _array(references["RefIds"], "references.RefIds")
        for index, raw_entry in enumerate(entries):
            entry = _mapping(raw_entry, f"references.RefIds[{index}]")
            if set(entry) != _REFERENCE_ENTRY_FIELDS:
                raise Plan1PItemRuntimeError("reference-entry-shape-invalid", str(index))
            rid = _integer(entry["rid"], f"references.RefIds[{index}].rid")
            if rid <= 0:
                continue
            type_raw = _mapping(entry["type"], f"references.RefIds[{index}].type")
            if set(type_raw) != _REFERENCE_TYPE_FIELDS:
                raise Plan1PItemRuntimeError("reference-type-shape-invalid", str(index))
            class_name = _text(type_raw["class"], f"references.RefIds[{index}].type.class")
            if rid in by_rid:
                raise Plan1PItemRuntimeError("reference-rid-duplicate", str(rid))
            by_rid[rid] = Plan1LocalSaveActiveReference(
                rid, class_name, _mapping(entry["data"], f"references.RefIds[{index}].data")
            )
        active: list[Plan1LocalSaveActiveReference] = []
        seen: set[int] = set()
        for rid in link_ids:
            if rid in seen:
                blockers.append(_block("active-reference-duplicate", str(rid)))
                continue
            seen.add(rid)
            ref = by_rid.get(rid)
            if ref is None:
                blockers.append(_block("active-reference-missing", str(rid)))
            else:
                active.append(ref)
        _integer(status["_effectCreateCount"], "status._effectCreateCount", minimum=0)
        return tuple(active)
    except (Plan1PItemRuntimeError, TypeError, ValueError) as error:
        blockers.append(_block("local-save-reference-graph-invalid", str(error)))
        return ()


def parse_plan1_local_save_item_graph(
    payload: LocalSaveExamState | Mapping[str, object] | Sequence[object],
    active_references: Sequence[object] | None = None,
) -> Plan1LocalSaveItemReferenceGraph:
    """Parse ``itemList`` plus the active reference graph without inference.

    ``payload`` may be a typed ``LocalSaveExamState``, a raw ExamSaveData-like
    mapping, or the item-list sequence itself.  When a sequence of
    ``(rid, data)`` pairs is supplied as ``active_references``, the class name
    is ``TriggerEffectStatusEffect`` by explicit API contract.  A full raw
    ``status``/``references`` mapping is preferred because it preserves class
    identity and graph order.
    """

    blockers: list[Plan1PItemRuntimeBlocker] = []
    raw_items: object = payload
    root: Mapping[str, object] | None = None
    if isinstance(payload, LocalSaveExamState):
        if payload.root_runtime is None:
            blockers.append(_block("local-save-root-runtime-missing"))
        else:
            value = payload.root_runtime.opaque_fields.to_value()
            root = _mapping(value, "root_runtime.opaque_fields")
            raw_items = root.get("itemList")
    elif isinstance(payload, Mapping):
        if "root_runtime" in payload:
            root_value = payload.get("root_runtime")
            if isinstance(root_value, Mapping) and isinstance(root_value.get("opaque_fields"), Mapping):
                root = _mapping(root_value["opaque_fields"], "root_runtime.opaque_fields")
                raw_items = root.get("itemList")
            else:
                blockers.append(_block("local-save-root-runtime-invalid"))
        elif "opaque_fields" in payload and isinstance(payload.get("opaque_fields"), Mapping):
            root = _mapping(payload["opaque_fields"], "opaque_fields")
            raw_items = root.get("itemList")
        elif "itemList" in payload:
            root = payload
            raw_items = payload.get("itemList")
    if not isinstance(raw_items, (list, tuple)):
        blockers.append(_block("local-save-item-list-invalid"))
        raw_items = ()
    sources: list[Plan1LocalSaveItemSource] = []
    for index, raw in enumerate(raw_items):
        source, issue = _parse_item_source(raw, index)
        if issue is not None:
            blockers.append(issue)
        elif source is not None:
            sources.append(source)
    source_ids = tuple(source.item_id for source in sources)
    if len(source_ids) != len(set(source_ids)):
        blockers.append(_block("local-save-item-id-duplicate"))

    refs: tuple[Plan1LocalSaveActiveReference, ...] = ()
    create_count = 0
    if active_references is not None:
        parsed_refs: list[Plan1LocalSaveActiveReference] = []
        for index, raw in enumerate(active_references):
            try:
                if isinstance(raw, Plan1LocalSaveActiveReference):
                    parsed_refs.append(raw)
                    continue
                if isinstance(raw, tuple) and len(raw) == 2:
                    rid = _integer(raw[0], f"active_references[{index}].rid", minimum=1)
                    data = _mapping(raw[1], f"active_references[{index}].data")
                    parsed_refs.append(Plan1LocalSaveActiveReference(rid, "TriggerEffectStatusEffect", data))
                elif isinstance(raw, Mapping):
                    parsed_refs.append(
                        Plan1LocalSaveActiveReference(
                            _integer(raw.get("rid"), f"active_references[{index}].rid", minimum=1),
                            _text(raw.get("class"), f"active_references[{index}].class"),
                            _mapping(raw.get("data"), f"active_references[{index}].data"),
                        )
                    )
                else:
                    raise Plan1PItemRuntimeError("active-reference-entry-invalid", str(index))
            except (Plan1PItemRuntimeError, TypeError, ValueError) as error:
                blockers.append(_block("active-reference-entry-invalid", f"{index}:{error}"))
        refs = tuple(parsed_refs)
    elif root is not None:
        refs = _active_refs_from_root(root.get("status"), root.get("references"), blockers)
        status = root.get("status")
        if isinstance(status, Mapping) and type(status.get("_effectCreateCount")) is int:
            create_count = int(status["_effectCreateCount"])
    return Plan1LocalSaveItemReferenceGraph(tuple(sources), refs, create_count, tuple(dict.fromkeys(blockers)))


@dataclass(frozen=True, slots=True)
class Plan1PItemListener:
    source: Plan1LocalSaveItemSource
    enchantment: Plan1PItemEnchantmentGraph
    dispatch: Plan1PItemEventDispatch
    remaining_uses: int
    status_uid: int
    turn_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.source, Plan1LocalSaveItemSource):
            raise TypeError("source must be Plan1LocalSaveItemSource")
        if not isinstance(self.enchantment, Plan1PItemEnchantmentGraph):
            raise TypeError("enchantment must be Plan1PItemEnchantmentGraph")
        if not isinstance(self.dispatch, Plan1PItemEventDispatch):
            raise TypeError("dispatch must be Plan1PItemEventDispatch")
        _integer(self.remaining_uses, "remaining_uses", minimum=-1)
        _integer(self.status_uid, "status_uid", minimum=1)
        _integer(self.turn_count, "turn_count", minimum=0)
        max_uses = self.enchantment.max_uses
        if max_uses == -1 and self.remaining_uses != -1:
            raise Plan1PItemRuntimeError("unlimited-remaining-mismatch", self.enchantment.id)
        if max_uses >= 0 and not 0 <= self.remaining_uses <= max_uses:
            raise Plan1PItemRuntimeError("remaining-uses-out-of-range", self.enchantment.id)

    @property
    def enchantment_id(self) -> str:
        return self.enchantment.id

    @property
    def source_item_id(self) -> str:
        return self.source.item_id

    @property
    def trigger(self) -> Plan1PItemEventDispatch:
        return self.dispatch

    @property
    def effects(self) -> tuple[Plan1PItemEffectNode, ...]:
        return self.enchantment.status.effects

    @property
    def max_uses(self) -> int:
        return self.enchantment.max_uses

    @property
    def executable(self) -> bool:
        return self.enchantment.status.supported


@dataclass(frozen=True, slots=True)
class Plan1PItemRuntime:
    sources: tuple[Plan1LocalSaveItemSource, ...] = ()
    listeners: tuple[Plan1PItemListener, ...] = ()
    next_status_uid: int = 1
    blockers: tuple[Plan1PItemRuntimeBlocker, ...] = ()

    def __post_init__(self) -> None:
        sources = tuple(self.sources)
        listeners = tuple(self.listeners)
        if any(not isinstance(value, Plan1LocalSaveItemSource) for value in sources):
            raise TypeError("sources must contain Plan1LocalSaveItemSource values")
        if any(not isinstance(value, Plan1PItemListener) for value in listeners):
            raise TypeError("listeners must contain Plan1PItemListener values")
        source_ids = tuple(value.item_id for value in sources)
        if len(source_ids) != len(set(source_ids)):
            raise Plan1PItemRuntimeError("runtime-source-duplicate")
        uids = tuple(value.status_uid for value in listeners)
        if len(uids) != len(set(uids)):
            raise Plan1PItemRuntimeError("runtime-status-uid-duplicate")
        _integer(self.next_status_uid, "next_status_uid", minimum=1)
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan1PItemRuntimeBlocker) for value in blockers):
            raise TypeError(
                "blockers must contain Plan1PItemRuntimeBlocker values"
            )
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "listeners", listeners)
        object.__setattr__(self, "blockers", blockers)

    @property
    def supported(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class Plan1PItemRuntimeCompilation:
    runtime: Plan1PItemRuntime | None
    graph: Plan1LocalSaveItemReferenceGraph
    item_graphs: tuple[Plan1PItemGraph, ...] = ()
    blockers: tuple[Plan1PItemRuntimeBlocker, ...] = ()

    @property
    def supported(self) -> bool:
        return self.runtime is not None and not self.blockers

    @property
    def fail_closed(self) -> bool:
        return not self.supported


def _item_status_identity_ok(
    status: Mapping[str, object],
    source: Plan1LocalSaveItemSource,
    enchantment: Plan1PItemEnchantmentGraph,
) -> tuple[bool, tuple[Plan1PItemRuntimeBlocker, ...]]:
    blockers: list[Plan1PItemRuntimeBlocker] = []
    if set(status) != _TRIGGER_STATUS_FIELDS:
        blockers.append(_block("local-save-item-status-shape-unsupported", enchantment.id))
        return False, tuple(blockers)
    expected_item = {
        "_fireCount": source.fire_count,
        "_id": source.item_id,
        "_itemType": source.item_type,
        "_parentCustomItemIds": list(source.parent_custom_item_ids),
        "_reactionCount": source.reaction_count,
    }
    trigger = status.get("_trigger")
    expected_phase_value = SERIALIZED_PHASE_ENUM.get(enchantment.trigger.phase)
    expected_trigger = {
        "_id": enchantment.trigger.trigger_id,
        "_phaseTypeList": [] if expected_phase_value is None else [expected_phase_value],
        "_phaseValueList": list(enchantment.trigger.phase_values),
    }
    if expected_phase_value is None:
        blockers.append(_block("local-save-item-trigger-phase-unsupported", enchantment.trigger.phase))
    expected_identity = {
        "_isItemDirectEnchant": True,
        "_statusEnchantId": enchantment.id,
        "_trigger": expected_trigger,
        "_triggerItem": expected_item,
        "_originType": 1,
        "_overrideType": 31,
        "_isFromEnchantEffect": False,
        "_isEncoreEnchant": False,
        "_startEnchantOriginId": "",
        "_startEnchantOriginLevel": 0,
        "_startEnchantOriginType": 0,
        "_startEnchantOwnerId": "",
        "_fromExamEffectIdList": [],
        "_descriptionReactiveDataTextList": [],
        "_descriptionReactiveDataTypeList": [],
        "_triggerCard": _EMPTY_TRIGGER_CARD,
        "_triggerDrink": {"_id": ""},
        "_triggerGimmickGroup": _EMPTY_TRIGGER_GIMMICK,
    }
    for key, expected in expected_identity.items():
        if not _same_json(status.get(key), expected):
            blockers.append(_block("local-save-item-status-identity-drift", f"{enchantment.id}:{key}"))
    return not blockers, tuple(dict.fromkeys(blockers))


def _serialize_effect_node(node: Plan1PItemEffectNode) -> dict[str, object] | None:
    raw = dict(node.raw_payload)
    try:
        values = {
            "_cardGrowEffectIdList": list(_text_tuple(raw["produceCardGrowEffectIds"], f"{node.id}.grow_ids", empty=True)),
            "_cardSearchId": _text(raw["produceCardSearchId"], f"{node.id}.search", empty=True),
            "_cardSearchId2": _text(raw["produceCardSearchId2"], f"{node.id}.search2", empty=True),
            "_chainEffectId": _text(raw["chainProduceExamEffectId"], f"{node.id}.chain", empty=True),
            "_chainEffectIdList": list(_text_tuple(raw["chainProduceExamEffectIds"], f"{node.id}.chain_ids", empty=True)),
            "_effectCount": node.effect_count,
            "_effectGroupIdList": list(_text_tuple(raw["effectGroupIds"], f"{node.id}.groups", empty=True)),
            "_effectTurn": node.effect_turn,
            "_effectType": SERIALIZED_EFFECT_ENUM[node.effect_type],
            "_effectValue1": node.value1,
            "_effectValue2": node.value2,
            "_id": node.id,
            "_judgeTargetIndex": 0,
            "_movePositionType": SERIALIZED_MOVE_POSITION_ENUM[_text(raw["movePositionType"], f"{node.id}.move")],
            "_pickCount": 0,
            "_pickCountMax": _integer(raw["pickCountMax"], f"{node.id}.pick_max", minimum=0),
            "_pickCountMax2": _integer(raw["pickCountMax2"], f"{node.id}.pick_max2", minimum=0),
            "_pickCountMin": _integer(raw["pickCountMin"], f"{node.id}.pick_min", minimum=0),
            "_pickCountMin2": _integer(raw["pickCountMin2"], f"{node.id}.pick_min2", minimum=0),
            "_pickCountReferenceProduceCardSearchId": _text(raw["pickCountReferenceProduceCardSearchId"], f"{node.id}.pick_ref", empty=True),
            "_pickCountReferenceProduceCardSearchId2": _text(raw["pickCountReferenceProduceCardSearchId2"], f"{node.id}.pick_ref2", empty=True),
            "_pickCountType": SERIALIZED_PICK_COUNT_ENUM[_text(raw["pickCountType"], f"{node.id}.pick_type")],
            "_pickCountType2": SERIALIZED_PICK_COUNT_ENUM[_text(raw["pickCountType2"], f"{node.id}.pick_type2")],
            "_pickRangeType": SERIALIZED_PICK_RANGE_ENUM[_text(raw["pickRangeType"], f"{node.id}.pick_range")],
            "_pickRangeType2": SERIALIZED_PICK_RANGE_ENUM[_text(raw["pickRangeType2"], f"{node.id}.pick_range2")],
            "_statusEnchantId": node.status_enchant_id,
            "_targetExamEffectType": SERIALIZED_EFFECT_ENUM[_text(raw["targetExamEffectType"], f"{node.id}.target_type")],
            "_targetProduceCardId": _text(raw["targetProduceCardId"], f"{node.id}.target_card", empty=True),
            "_targetUpgradeCount": _integer(raw["targetUpgradeCount"], f"{node.id}.target_upgrade", minimum=0),
        }
    except (KeyError, Plan1PItemRuntimeError):
        return None
    return values


def _validate_active_status_payload(
    status: Mapping[str, object],
    source: Plan1LocalSaveItemSource,
    enchantment: Plan1PItemEnchantmentGraph,
) -> tuple[int, int, tuple[Plan1PItemRuntimeBlocker, ...]]:
    ok, blockers = _item_status_identity_ok(status, source, enchantment)
    if not ok:
        return 0, 0, blockers
    uid = status.get("_uid")
    if type(uid) is not int or uid < 1:
        return 0, 0, (_block("local-save-item-status-uid-invalid", enchantment.id),)
    turn_count = status.get("_turnCount")
    if type(turn_count) is not int or turn_count < 0:
        return 0, 0, (_block("local-save-item-status-turn-count-invalid", enchantment.id),)
    limit = status.get("_limitCount")
    if type(limit) is not int:
        return 0, 0, (_block("local-save-item-status-limit-invalid", enchantment.id),)
    if enchantment.max_uses == -1:
        if limit != -1:
            return 0, 0, (_block("local-save-item-status-limit-invalid", enchantment.id),)
    elif not 0 <= limit <= enchantment.max_uses:
        return 0, 0, (_block("local-save-item-status-limit-invalid", enchantment.id),)
    if status.get("_isTurnLimited") is not False or status.get("_turn") != -1:
        return 0, 0, (_block("local-save-item-status-lifecycle-invalid", enchantment.id),)
    # Plan 1's generic item dispatcher does not yet model a per-turn quota or
    # a turn-start lifecycle transition.  Retaining a malformed/non-neutral
    # serializer value here would silently reinterpret an item with different
    # native timing.  Keep the listener boundary fail-closed until those
    # fields have an exact executor.
    if type(status.get("_isPassingTurnStart")) is not bool:
        return 0, 0, (
            _block("local-save-item-status-lifecycle-invalid", enchantment.id),
        )
    if status.get("_limitCountInTurn") != -1 or status.get("_limitCountInTurnRemain") != -1:
        return 0, 0, (
            _block("local-save-item-status-lifecycle-invalid", enchantment.id),
        )
    phase_counts = status.get("_phaseCountDictionary")
    if (
        not isinstance(phase_counts, Mapping)
        or set(phase_counts) != {"_list"}
        or not isinstance(phase_counts.get("_list"), list)
    ):
        return 0, 0, (
            _block("local-save-item-status-lifecycle-invalid", enchantment.id),
        )
    expected_effects = tuple(_serialize_effect_node(effect) for effect in enchantment.status.effects)
    raw_effects = status.get("_effectList")
    if not isinstance(raw_effects, list) or any(value is None for value in expected_effects):
        return 0, 0, (_block("local-save-item-effect-serialization-unsupported", enchantment.id),)
    if len(raw_effects) != len(expected_effects) or any(
        not isinstance(value, Mapping)
        or expected is None
        or set(value) != _SERIALIZED_EFFECT_FIELDS
        or not _same_json(value, expected)
        for value, expected in zip(raw_effects, expected_effects, strict=True)
    ):
        return 0, 0, (_block("local-save-item-effect-mismatch", enchantment.id),)
    return int(uid), int(turn_count), ()


def restore_plan1_local_save_item_runtime(
    payload: LocalSaveExamState | Mapping[str, object] | Sequence[object] | None = None,
    active_references: Sequence[object] | None = None,
    *,
    database: Path = DEFAULT_DATABASE,
    active_trigger_statuses: Sequence[object] | None = None,
    item_list: Sequence[object] | None = None,
) -> Plan1PItemRuntimeCompilation:
    """Restore generic Plan 1 item listeners from LocalSave state-before."""

    if payload is None:
        if item_list is None:
            raise TypeError("payload or item_list is required")
        payload = item_list
    elif item_list is not None:
        raise TypeError("pass payload or item_list, not both")
    if active_references is not None and active_trigger_statuses is not None:
        raise TypeError(
            "pass active_references or active_trigger_statuses, not both"
        )
    if active_trigger_statuses is not None:
        active_references = active_trigger_statuses
    graph = parse_plan1_local_save_item_graph(payload, active_references)
    blockers = list(graph.blockers)
    if blockers:
        return Plan1PItemRuntimeCompilation(None, graph, (), tuple(dict.fromkeys(blockers)))
    item_graphs: list[Plan1PItemGraph] = []
    rules_by_item: dict[str, Plan1PItemGraph] = {}
    for source in graph.item_sources:
        loaded, issues = resolve_plan1_pitem_graph(source.item_id, database=database)
        if loaded is None:
            blockers.extend(issues)
            continue
        item_graphs.append(loaded)
        rules_by_item[source.item_id] = loaded
        blockers.extend(loaded.blockers)
    if blockers:
        return Plan1PItemRuntimeCompilation(None, graph, tuple(item_graphs), tuple(dict.fromkeys(blockers)))
    by_enchantment: dict[str, list[Plan1LocalSaveActiveReference]] = {}
    for ref in graph.active_references:
        if ref.class_name != "TriggerEffectStatusEffect":
            if ref.data.get("_isItemDirectEnchant") is True:
                blockers.append(
                    _block(
                        "local-save-item-status-class-unsupported",
                        f"{ref.rid}:{ref.class_name}",
                    )
                )
            # Non-item statuses are owned by other Plan1 runtimes.  They stay
            # in the captured graph but do not become item listeners here.
            continue
        raw = ref.data
        if raw.get("_isItemDirectEnchant") is True:
            status_id = raw.get("_statusEnchantId")
            if not isinstance(status_id, str) or not status_id:
                blockers.append(_block("local-save-item-status-id-invalid", str(ref.rid)))
            else:
                by_enchantment.setdefault(status_id, []).append(ref)
    listeners: list[Plan1PItemListener] = []
    seen_refs: set[int] = set()
    for source in graph.item_sources:
        item_graph = rules_by_item[source.item_id]
        for enchantment in item_graph.enchantments:
            matches = by_enchantment.get(enchantment.id, [])
            if len(matches) != 1:
                blockers.append(_block("local-save-item-enchantment-reference-count", f"{enchantment.id}:{len(matches)}"))
                continue
            ref = matches[0]
            seen_refs.add(ref.rid)
            status = ref.data
            uid, turn_count, status_blockers = _validate_active_status_payload(status, source, enchantment)
            blockers.extend(status_blockers)
            dispatch, dispatch_blockers = resolve_plan1_pitem_event_dispatch(
                enchantment.trigger,
                card_search_rule=enchantment.trigger.card_search_rule,
            )
            blockers.extend(dispatch_blockers)
            if dispatch is None or status_blockers:
                continue
            limit = int(status["_limitCount"])
            listeners.append(Plan1PItemListener(source, enchantment, dispatch, limit, uid, turn_count))
    for ref in graph.active_references:
        if ref.class_name == "TriggerEffectStatusEffect" and ref.data.get("_isItemDirectEnchant") is True and ref.rid not in seen_refs:
            blockers.append(_block("local-save-item-enchantment-unclaimed-reference", str(ref.rid)))
    uids = tuple(listener.status_uid for listener in listeners)
    if len(uids) != len(set(uids)):
        blockers.append(_block("local-save-item-status-uid-duplicate"))
    if graph.effect_create_count and any(uid > graph.effect_create_count for uid in uids):
        blockers.append(_block("local-save-item-status-uid-create-count-mismatch"))
    if blockers:
        return Plan1PItemRuntimeCompilation(None, graph, tuple(item_graphs), tuple(dict.fromkeys(blockers)))
    next_uid = max((*uids, graph.effect_create_count), default=0) + 1
    runtime = Plan1PItemRuntime(tuple(graph.item_sources), tuple(listeners), next_uid)
    return Plan1PItemRuntimeCompilation(runtime, graph, tuple(item_graphs), ())


@dataclass(frozen=True, slots=True)
class Plan1PItemEffectApplication:
    effect_id: str
    before: object
    after: object
    value: int


@dataclass(frozen=True, slots=True)
class Plan1PItemEffectExecution:
    before: object
    after: object
    trace: tuple[Plan1PItemEffectApplication, ...] = ()
    blockers: tuple[Plan1PItemRuntimeBlocker, ...] = ()

    @property
    def applied(self) -> bool:
        return not self.blockers


def execute_plan1_pitem_effects(
    state: object,
    effects: Sequence[Plan1PItemEffectNode],
) -> Plan1PItemEffectExecution:
    """Apply the generic scalar subset without FKTN identity checks.

    ``state`` is intentionally duck-typed only at this narrow helper boundary
    (the public Plan 1 scalar is imported lazily), keeping graph parsing usable
    by tools that do not load the card kernel.  The accepted operations are
    shape-driven ``ExamParameterBuff`` and ``ExamBlockFix`` rows.
    """

    from .plan1_native_core import Plan1ScalarState

    if not isinstance(state, Plan1ScalarState):
        raise TypeError("state must be Plan1ScalarState")
    current = state
    trace: list[Plan1PItemEffectApplication] = []
    blockers: list[Plan1PItemRuntimeBlocker] = []
    for effect in effects:
        if not isinstance(effect, Plan1PItemEffectNode):
            blockers.append(_block("effect-type-invalid"))
            break
        if effect.blockers:
            blockers.extend(effect.blockers)
            break
        before = current
        if effect.effect_type == "ProduceExamEffectType_ExamParameterBuff":
            if effect.value1 != effect.value2 or effect.value1 != 0 or effect.effect_count != 0 or effect.effect_turn < 0:
                blockers.append(_block("effect-shape-unsupported", effect.id))
                break
            next_turns = current.parameter_buff_turns + effect.effect_turn
            if next_turns > (2**31 - 1):
                blockers.append(_block("effect-runtime-domain", f"{effect.id}:parameter_buff_turns"))
                break
            current = replace(current, parameter_buff_turns=next_turns)
            trace.append(Plan1PItemEffectApplication(effect.id, before, current, effect.effect_turn))
        elif effect.effect_type == "ProduceExamEffectType_ExamBlockFix":
            if effect.value1 < 0 or effect.value2 != 0 or effect.effect_count != 0 or effect.effect_turn != 0:
                blockers.append(_block("effect-shape-unsupported", effect.id))
                break
            next_block = current.block + effect.value1
            if next_block > (2**31 - 1):
                blockers.append(_block("effect-runtime-domain", f"{effect.id}:block"))
                break
            current = replace(current, block=next_block)
            trace.append(Plan1PItemEffectApplication(effect.id, before, current, effect.value1))
        elif effect.effect_type == "ProduceExamEffectType_ExamBlock":
            from .plan1_native_core import Plan1CompiledEffect, execute_plan1_effects
            compiled = Plan1CompiledEffect(effect.id, effect.effect_type, effect.value1, effect.value2, effect.effect_count, effect.effect_turn)
            applied = execute_plan1_effects(current, (compiled,))
            if applied.blockers:
                blockers.extend(_block(value.code, value.detail) for value in applied.blockers)
                break
            current = applied.after
            trace.append(Plan1PItemEffectApplication(effect.id, before, current, current.block-before.block))
        elif effect.effect_type == "ProduceExamEffectType_ExamPlayableValueAdd":
            if effect.value1 != 0 or effect.value2 != 0 or effect.effect_count <= 0 or effect.effect_turn != 0:
                blockers.append(_block("effect-shape-unsupported", effect.id))
                break
            next_plays = current.plays_remaining + effect.effect_count
            if next_plays > (2**31 - 1):
                blockers.append(_block("effect-runtime-domain", f"{effect.id}:plays_remaining"))
                break
            current = replace(current, plays_remaining=next_plays)
            trace.append(Plan1PItemEffectApplication(effect.id, before, current, effect.effect_count))
        elif effect.effect_type == "ProduceExamEffectType_ExamLessonBuff":
            if effect.value1 <= 0 or effect.value2 != 0 or effect.effect_count != 0 or effect.effect_turn != 0:
                blockers.append(_block("effect-shape-unsupported", effect.id))
                break
            next_lesson = current.lesson_buff + effect.value1
            if next_lesson > (2**31 - 1):
                blockers.append(_block("effect-runtime-domain", f"{effect.id}:lesson_buff"))
                break
            current = replace(current, lesson_buff=next_lesson)
            trace.append(Plan1PItemEffectApplication(effect.id, before, current, effect.value1))
        else:
            blockers.append(_block("effect-runtime-unsupported", f"{effect.id}:{effect.effect_type}"))
            break
    if blockers:
        return Plan1PItemEffectExecution(state, state, (), tuple(dict.fromkeys(blockers)))
    return Plan1PItemEffectExecution(state, current, tuple(trace))


@dataclass(frozen=True, slots=True)
class Plan1PItemDispatchResult:
    before: Plan1PItemRuntime
    after: Plan1PItemRuntime
    effects: tuple[Plan1PItemEffectNode, ...]
    fired_enchantment_ids: tuple[str, ...]
    evaluations: tuple[Plan1PItemEventDispatchEvaluation, ...] = ()
    execution: Plan1PItemEffectExecution | None = None
    blockers: tuple[Plan1PItemRuntimeBlocker, ...] = ()

    @property
    def applied(self) -> bool:
        if self.blockers:
            return False
        return self.execution is None or self.execution.applied

    @property
    def runtime(self) -> Plan1PItemRuntime:
        return self.after

    @property
    def state_before(self) -> object | None:
        return None if self.execution is None else self.execution.before

    @property
    def state_after(self) -> object | None:
        return None if self.execution is None else self.execution.after


def dispatch_plan1_pitem_event(
    runtime: Plan1PItemRuntime,
    context: Plan1PItemEventContext | Plan1PItemEvent,
    state: object | None = None,
) -> Plan1PItemDispatchResult:
    """Dispatch one event and spend listener uses only after a proven match."""

    if not isinstance(runtime, Plan1PItemRuntime):
        raise TypeError("runtime must be Plan1PItemRuntime")
    if isinstance(context, Plan1PItemEvent):
        context = context.to_context()
    if not isinstance(context, Plan1PItemEventContext):
        raise TypeError("context must be Plan1PItemEventContext or Plan1PItemEvent")
    listeners: list[Plan1PItemListener] = []
    effects: list[Plan1PItemEffectNode] = []
    fired: list[str] = []
    evaluations: list[Plan1PItemEventDispatchEvaluation] = []
    blockers: list[Plan1PItemRuntimeBlocker] = list(runtime.blockers)
    for listener in runtime.listeners:
        evaluation = evaluate_plan1_pitem_event_dispatch(listener.dispatch, context)
        evaluations.append(evaluation)
        if evaluation.fires is None:
            blockers.append(_block("item-event-evidence-incomplete", f"{listener.enchantment_id}:{','.join(evaluation.reasons)}"))
            listeners.append(listener)
            continue
        if not evaluation.fires or (listener.remaining_uses == 0):
            listeners.append(listener)
            continue
        remaining = -1 if listener.remaining_uses == -1 else listener.remaining_uses - 1
        listeners.append(replace(listener, remaining_uses=remaining))
        effects.extend(listener.enchantment.status.effects)
        fired.append(listener.enchantment_id)
    after = replace(runtime, listeners=tuple(listeners))
    execution: Plan1PItemEffectExecution | None = None
    # Listener-use spending and scalar effects form one boundary.  If any
    # listener has an incomplete predicate (or the runtime already carries a
    # blocker), publishing the tentative decrements would expose a partially
    # consumed runtime even though this dispatch is not applicable.
    if blockers:
        return Plan1PItemDispatchResult(
            runtime,
            runtime,
            (),
            (),
            tuple(evaluations),
            None,
            tuple(dict.fromkeys(blockers)),
        )
    if state is not None and effects:
        execution = execute_plan1_pitem_effects(state, effects)
        blockers.extend(execution.blockers)
        if execution.blockers:
            # The event dispatch is atomic at the helper boundary.  Do not
            # expose a consumed listener if its scalar effect cannot apply.
            after = runtime
            fired = []
            effects = []
    return Plan1PItemDispatchResult(
        runtime,
        after,
        tuple(effects),
        tuple(fired),
        tuple(evaluations),
        execution,
        tuple(dict.fromkeys(blockers)),
    )


# Naming aliases used by callers that mirror the Plan2 runtime vocabulary.
Plan1NativeItemSource = Plan1LocalSaveItemSource
Plan1NativeItemReference = Plan1LocalSaveActiveReference
Plan1NativeItemReferenceGraph = Plan1LocalSaveItemReferenceGraph
Plan1NativeItemRuntime = Plan1PItemRuntime
Plan1NativeItemCompilation = Plan1PItemRuntimeCompilation
Plan1NativeItemListener = Plan1PItemListener
Plan1NativeItemEffect = Plan1PItemEffectNode
Plan1NativeItemEvent = Plan1PItemEvent
Plan1NativeItemDispatch = Plan1PItemDispatchResult
Plan1PItemEventPhase = PItemEventPhase
Plan1PItemPredicateContext = Plan1PItemEventContext
Plan1PItemDispatchEvaluation = Plan1PItemEventDispatchEvaluation
PItemCardSearchEvidence = Plan1PItemCardSearchEvidence
PItemCardSearchReference = Plan1PItemCardSearchReference
Plan1PItemTriggerContext = Plan1PItemEventContext
Plan1PItemVariant = Plan1PItemGraph
load_plan1_pitem_variant = load_plan1_pitem_graph
resolve_plan1_pitem_variant = resolve_plan1_pitem_graph
compile_plan1_pitem_graph = load_plan1_pitem_graph
parse_plan1_item_event_dispatch = parse_plan1_pitem_event_dispatch
resolve_plan1_item_event_dispatch = resolve_plan1_pitem_event_dispatch
evaluate_plan1_item_event_dispatch = evaluate_plan1_pitem_event_dispatch
parse_plan1_item_reference_graph = parse_plan1_local_save_item_graph
build_plan1_item_reference_graph = parse_plan1_local_save_item_graph
restore_plan1_native_item_runtime = restore_plan1_local_save_item_runtime
restore_plan1_pitem_runtime = restore_plan1_local_save_item_runtime
restore_plan1_item_runtime = restore_plan1_local_save_item_runtime
compile_plan1_local_save_item_runtime = restore_plan1_local_save_item_runtime
dispatch_plan1_native_item_event = dispatch_plan1_pitem_event


__all__ = [
    "ITEM_EFFECT_STATUS_ENCHANT",
    "KNOWN_PHASES",
    "PLAN1",
    "Plan1LocalSaveActiveReference",
    "Plan1LocalSaveItemReferenceGraph",
    "Plan1LocalSaveItemSource",
    "Plan1NativeItemCompilation",
    "Plan1NativeItemDispatch",
    "Plan1NativeItemEvent",
    "Plan1NativeItemEffect",
    "Plan1NativeItemListener",
    "Plan1NativeItemReference",
    "Plan1NativeItemReferenceGraph",
    "Plan1NativeItemRuntime",
    "Plan1NativeItemSource",
    "Plan1PItemCardSearchEvidence",
    "Plan1PItemCardSearchReference",
    "PItemCardSearchEvidence",
    "PItemCardSearchReference",
    "Plan1PItemDispatchResult",
    "Plan1PItemEffectApplication",
    "Plan1PItemEffectExecution",
    "Plan1PItemEffectGraphError",
    "Plan1PItemEffectNode",
    "Plan1PItemEnchantmentGraph",
    "Plan1PItemEventContext",
    "Plan1PItemEventPhase",
    "Plan1PItemEventDispatch",
    "Plan1PItemEventDispatchEvaluation",
    "Plan1PItemPredicateContext",
    "Plan1PItemDispatchEvaluation",
    "Plan1PItemEvent",
    "Plan1PItemGraph",
    "Plan1PItemRuntime",
    "Plan1PItemRuntimeBlocker",
    "Plan1PItemRuntimeCompilation",
    "Plan1PItemRuntimeError",
    "Plan1PItemStatusGraph",
    "Plan1PItemTriggerContext",
    "Plan1PItemVariant",
    "SERIALIZED_EFFECT_ENUM",
    "SERIALIZED_PHASE_ENUM",
    "compile_plan1_local_save_item_runtime",
    "build_plan1_item_reference_graph",
    "dispatch_plan1_native_item_event",
    "dispatch_plan1_pitem_event",
    "evaluate_plan1_item_event_dispatch",
    "evaluate_plan1_pitem_event_dispatch",
    "execute_plan1_pitem_effects",
    "load_plan1_pitem_graph",
    "load_plan1_pitem_variant",
    "compile_plan1_pitem_graph",
    "parse_plan1_item_event_dispatch",
    "parse_plan1_item_reference_graph",
    "parse_plan1_local_save_item_graph",
    "parse_plan1_pitem_event_dispatch",
    "resolve_plan1_item_event_dispatch",
    "resolve_plan1_pitem_event_dispatch",
    "resolve_plan1_pitem_graph",
    "resolve_plan1_pitem_variant",
    "restore_plan1_native_item_runtime",
    "restore_plan1_item_runtime",
    "restore_plan1_pitem_runtime",
    "restore_plan1_local_save_item_runtime",
]
