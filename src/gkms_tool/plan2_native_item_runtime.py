"""Typed equipped-item runtime for the Plan2 native horizon.

The runtime is compiled from the already verified ``item_rules`` Master
chain.  It admits only trigger/effect families whose Android execution order
and scalar owner have been recovered.  Unknown trigger/effect shapes fail
closed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Final
from .runtime_lesson_context import LESSON_STEP_VALUES, lesson_step_rule

from .card_search import (
    ProduceCardSearchRule,
    match_exact_playing_card_search,
    match_exact_target_card_search,
    match_plan3_card_move_search,
)
from .plan2_pitem_composite_trigger import (
    CARD_MOVE_UNKNOWN as COMPOSITE_CARD_MOVE_UNKNOWN,
    parse_composite_trigger,
    CompositeTriggerSpec,
)
from .plan2_pitem_event_dispatch import (
    PItemEventDispatch,
    PItemPredicateContext,
    evaluate_plan2_pitem_event_dispatch,
    parse_plan2_pitem_event_dispatch,
)
from .item_rules import (
    CARD_MOVE_UNKNOWN,
    LESSON_DANCE,
    LESSON_UNKNOWN,
    PHASE_CARD_PLAY_AFTER,
    PHASE_END_TURN_INTERVAL,
    PHASE_START_TURN,
    PHASE_STATUS_CHANGE,
    EquippedItemRule,
    ItemEnchantment,
    load_item_rule,
)
from .master_db import DEFAULT_DATABASE
from .logic_engine import (
    EFFECT_PLAYABLE_VALUE_ADD,
    EFFECT_STAMINA_REDUCE_FIX,
    MasterCardEffect,
    load_master_effect,
)
from .plan2_native_serializer_progress import (
    INT32_MAX,
    Plan2NativeSerializerLifecyclePolicy,
    Plan2NativeSerializerProgress,
    Plan2NativeSerializerProgressError,
    restore_plan2_native_serializer_progress,
)
from .plan3_stamina_multiple_trigger import (
    FIELD_STAMINA_UP_MULTIPLE,
    StaminaField,
    StaminaSnapshot,
    evaluate_stamina_multiple,
)


EFFECT_REVIEW: Final = "ProduceExamEffectType_ExamReview"
EFFECT_REVIEW_MULTIPLE: Final = "ProduceExamEffectType_ExamReviewMultiple"
EFFECT_AGGRESSIVE: Final = "ProduceExamEffectType_ExamCardPlayAggressive"
EFFECT_BLOCK: Final = "ProduceExamEffectType_ExamBlock"
EFFECT_LESSON_DEPEND_BLOCK: Final = "ProduceExamEffectType_ExamLessonDependBlock"
EFFECT_REVIEW_REDUCE: Final = "ProduceExamEffectType_ExamReviewReduce"
EFFECT_CARD_DRAW: Final = "ProduceExamEffectType_ExamCardDraw"
EFFECT_BLOCK_ADD_MULTIPLE_AGGRESSIVE: Final = "ProduceExamEffectType_ExamBlockAddMultipleAggressive"
EFFECT_BLOCK_DEPEND_REVIEW: Final = (
    "ProduceExamEffectType_ExamBlockDependExamReview"
)
EFFECT_BLOCK_FIX: Final = "ProduceExamEffectType_ExamBlockFix"
EFFECT_STAMINA_REDUCE: Final = "ProduceExamEffectType_ExamStaminaReduce"
EFFECT_LESSON_DEPEND_REVIEW: Final = (
    "ProduceExamEffectType_ExamLessonDependExamReview"
)
EFFECT_LESSON_DEPEND_CARD_PLAY_AGGRESSIVE: Final = (
    "ProduceExamEffectType_ExamLessonDependExamCardPlayAggressive"
)
EFFECT_STAMINA_RECOVER_FIX: Final = (
    "ProduceExamEffectType_ExamStaminaRecoverFix"
)
EFFECT_REVIEW_ADDITIVE: Final = (
    "ProduceExamEffectType_ExamReviewAdditive"
)
EFFECT_AGGRESSIVE_ADDITIVE: Final = (
    "ProduceExamEffectType_ExamAggressiveAdditive"
)
EFFECT_STAMINA_CONSUMPTION_DOWN: Final = (
    "ProduceExamEffectType_ExamStaminaConsumptionDown"
)
EFFECT_LESSON_MULTIPLE: Final = (
    "ProduceExamEffectType_ExamLessonValueMultiple"
)
EFFECT_LESSON_MULTIPLE_DOWN: Final = (
    "ProduceExamEffectType_ExamLessonValueMultipleDown"
)
EFFECT_TIMER: Final = "ProduceExamEffectType_ExamEffectTimer"
ITEM_PHASE_CARD_PLAY_AFTER: Final = PHASE_CARD_PLAY_AFTER
ITEM_PHASE_END_TURN: Final = PHASE_END_TURN_INTERVAL
ITEM_PHASE_TURN_START: Final = PHASE_START_TURN
ITEM_PHASE_TURN_INTERVAL: Final = "ProduceExamPhaseType_ExamTurnInterval"
ITEM_PHASE_STATUS_CHANGE: Final = PHASE_STATUS_CHANGE
ITEM_PHASE_START_EXAM: Final = "ProduceExamPhaseType_ExamStartExam"
ITEM_PHASE_START_PLAY: Final = "ProduceExamPhaseType_StartPlay"
ITEM_PHASE_CARD_PLAY: Final = "ProduceExamPhaseType_ExamCardPlay"
ITEM_PHASE_PLAY_COUNT_INTERVAL: Final = (
    "ProduceExamPhaseType_ExamPlayCountInterval"
)
ITEM_PHASE_PLAY_TURN_COUNT_INTERVAL: Final = (
    "ProduceExamPhaseType_ExamPlayTurnCountInterval"
)
ITEM_PHASE_STAMINA_REDUCE: Final = "ProduceExamPhaseType_ExamStaminaReduce"
ITEM_PHASE_BUFF_CONSUME: Final = "ProduceExamPhaseType_ExamBuffConsume"
ITEM_PHASE_AGGRESSIVE_UP_INTERVAL: Final = (
    "ProduceExamPhaseType_ExamAggressiveUpInterval"
)
FIELD_REVIEW_UP: Final = "ProduceExamFieldStatusType_ReviewUp"

_PHASE_ENUM: Final = {
    ITEM_PHASE_CARD_PLAY_AFTER: 3,
    ITEM_PHASE_TURN_START: 4,
    ITEM_PHASE_STATUS_CHANGE: 13,
    ITEM_PHASE_END_TURN: 43,
    ITEM_PHASE_TURN_INTERVAL: 22,
    ITEM_PHASE_START_EXAM: 6,
    ITEM_PHASE_START_PLAY: 27,
    ITEM_PHASE_CARD_PLAY: 2,
    ITEM_PHASE_PLAY_COUNT_INTERVAL: 23,
    ITEM_PHASE_PLAY_TURN_COUNT_INTERVAL: 25,
    ITEM_PHASE_STAMINA_REDUCE: 20,
    ITEM_PHASE_BUFF_CONSUME: 34,
    ITEM_PHASE_AGGRESSIVE_UP_INTERVAL: 53,
}
_EFFECT_ENUM: Final = {
    EFFECT_STAMINA_REDUCE_FIX: 7,
    EFFECT_PLAYABLE_VALUE_ADD: 14,
    EFFECT_STAMINA_RECOVER_FIX: 28,
    EFFECT_REVIEW: 31,
    EFFECT_AGGRESSIVE: 42,
    EFFECT_BLOCK_DEPEND_REVIEW: 127,
    EFFECT_BLOCK_ADD_MULTIPLE_AGGRESSIVE: 143,
    EFFECT_BLOCK_FIX: 119,
    EFFECT_STAMINA_REDUCE: 60,
    EFFECT_LESSON_DEPEND_REVIEW: 124,
    EFFECT_AGGRESSIVE_ADDITIVE: 176,
    EFFECT_REVIEW_ADDITIVE: 177,
    EFFECT_STAMINA_CONSUMPTION_DOWN: 5,
    EFFECT_LESSON_MULTIPLE: 39,
    EFFECT_TIMER: 103,
    EFFECT_LESSON_MULTIPLE_DOWN: 155,
}
_SERIALIZED_PHASE_ENUM: Final = {
    **_PHASE_ENUM,
    # These phase values are not currently emitted by the item transition
    # adapter, but are valid v3.2.3 Master enum members.  Keeping them here is
    # required when restoring an exhausted listener: the serializer proof
    # must compare the exact Master integer, not reject a structurally valid
    # status merely because the old executable slice did not dispatch it.
    "ProduceExamPhaseType_ExamCardDraw": 1,
    "ProduceExamPhaseType_ExamCardMove": 7,
    "ProduceExamPhaseType_ExamCardAdd": 8,
    "ProduceExamPhaseType_ExamLesson": 9,
    "ProduceExamPhaseType_ExamForecast": 10,
    "ProduceExamPhaseType_ExamSearchCardPlay": 11,
    "ProduceExamPhaseType_ExamStanceChange": 12,
    "ProduceExamPhaseType_ExamTurnCheck": 16,
    "ProduceExamPhaseType_ExamUseDrink": 17,
    "ProduceExamPhaseType_ExamGetPoint": 18,
    "ProduceExamPhaseType_ExamShuffle": 19,
    "ProduceExamPhaseType_ExamTurnTimer": 21,
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
    "ProduceExamPhaseType_ExamPlayCountIntervalAfter": 44,
    "ProduceExamPhaseType_ExamStanceChangeFromConcentration": 45,
    "ProduceExamPhaseType_ExamStanceChangeFromPreservation": 46,
    "ProduceExamPhaseType_ExamStanceChangeFromFullPower": 47,
    "ProduceExamPhaseType_ExamCardUpgrade": 48,
    "ProduceExamPhaseType_ExamPlayCardMoveGrave": 49,
    "ProduceExamPhaseType_ExamParameterBuffUpInterval": 50,
    "ProduceExamPhaseType_ExamLessonBuffUpInterval": 51,
    "ProduceExamPhaseType_ExamReviewUpInterval": 52,
    "ProduceExamPhaseType_ExamFullPowerPointUpInterval": 54,
    "ProduceExamPhaseType_ExamStanceChangePreservationInterval": 55,
    "ProduceExamPhaseType_ExamStanceChangeConcentrationInterval": 56,
    "ProduceExamPhaseType_ExamStanceChangeFullPowerInterval": 57,
    "ProduceExamPhaseType_ExamCardUpgradeInterval": 58,
    "ProduceExamPhaseType_ExamCardDrawInterval": 59,
    "ProduceExamPhaseType_None": 999,
    "ProduceExamPhaseType_ExamPlayCountInterval": 23,
}
_SERIALIZED_PHASE_NAME_BY_ENUM: Final = {
    value: name for name, value in _SERIALIZED_PHASE_ENUM.items()
}
_SERIALIZED_EFFECT_ENUM: Final = {
    **_EFFECT_ENUM,
    # The serializer stores the complete enum even for an unsupported child
    # effect.  These members are retained for exact exhausted-listener proof;
    # execution remains fail-closed until an effect graph binds the family.
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
    "ProduceExamEffectType_ExamFullPowerPointReduce": 51,
    "ProduceExamEffectType_ExamLessonAddBlock": 52,
    "ProduceExamEffectType_ExamLessonFullPowerPoint": 56,
    "ProduceExamEffectType_ExamSearchPlayCardStaminaConsumptionChange": 59,
    "ProduceExamEffectType_ExamStaminaReduce": 60,
    "ProduceExamEffectType_ExamUplifting": 62,
    "ProduceExamEffectType_ExamExtraTurn": 63,
    "ProduceExamEffectType_ExamAntiDebuff": 66,
    "ProduceExamEffectType_ExamStaminaConsumptionAdd": 69,
    "ProduceExamEffectType_ExamThresholdDown": 70,
    "ProduceExamEffectType_ExamBlockAddDown": 73,
    "ProduceExamEffectType_ExamBlockAddDownRestriction": 74,
    "ProduceExamEffectType_ExamStaminaRecoverAdd": 76,
    "ProduceExamEffectType_ExamStaminaReduceChange": 77,
    "ProduceExamEffectType_ExamPanic": 78,
    "ProduceExamEffectType_ExamLessonChangeSpecifyLessThan": 81,
    "ProduceExamEffectType_ExamHandHold": 82,
    "ProduceExamEffectType_ExamStaminaConsumptionAddFix": 84,
    "ProduceExamEffectType_ExamStaminaConsumptionAddDown": 85,
    "ProduceExamEffectType_ExamStaminaRecoverRestriction": 86,
    "ProduceExamEffectType_ExamStaminaConsumptionDownAdd": 89,
    "ProduceExamEffectType_ExamGetCardUpgrade": 90,
    "ProduceExamEffectType_ExamStaminaConsumptionDownFix": 93,
    "ProduceExamEffectType_ExamHandGraveCountCardDraw": 98,
    "ProduceExamEffectType_ExamHandGraveCountCardAdd": 99,
    "ProduceExamEffectType_ExamEffectTimer": 103,
    "ProduceExamEffectType_ExamGimmickLessonDebuff": 105,
    "ProduceExamEffectType_ExamGimmickParameterDebuff": 106,
    "ProduceExamEffectType_ExamGimmickSleepy": 107,
    "ProduceExamEffectType_ExamGimmickEnthusiastic": 109,
    "ProduceExamEffectType_ExamGimmickPlayCardLimit": 113,
    "ProduceExamEffectType_ExamGimmickSlump": 114,
    "ProduceExamEffectType_ExamGimmickStartTurnCardDrawDown": 115,
    "ProduceExamEffectType_ExamStaminaRecoverMultiple": 117,
    "ProduceExamEffectType_ExamLessonPerSearchCount": 118,
    "ProduceExamEffectType_ExamLessonAddMultipleLessonBuff": 120,
    "ProduceExamEffectType_ExamCardStatusEnchant": 121,
    "ProduceExamEffectType_ExamBlockDown": 122,
    "ProduceExamEffectType_ExamLessonChangeSpecifyMoreThan": 123,
    "ProduceExamEffectType_ExamLessonDependExamCardPlayAggressive": 125,
    "ProduceExamEffectType_ExamReviewDependExamBlock": 126,
    "ProduceExamEffectType_ExamReviewDependExamCardPlayAggressive": 128,
    "ProduceExamEffectType_ExamParameterBuffMultiplePerTurn": 129,
    "ProduceExamEffectType_ExamLessonBuffDependParameterBuff": 130,
    "ProduceExamEffectType_ExamLessonDependParameterBuff": 131,
    "ProduceExamEffectType_ExamLessonAddMultipleParameterBuff": 132,
    "ProduceExamEffectType_ExamBlockPerUseCardCount": 133,
    "ProduceExamEffectType_ExamChainEffect": 140,
    "ProduceExamEffectType_StanceLock": 141,
    "ProduceExamEffectType_ExamLessonDependStamina": 142,
    "ProduceExamEffectType_ExamBlockAddMultipleAggressive": 143,
    "ProduceExamEffectType_ExamLessonDependStaminaConsumptionSum": 144,
    "ProduceExamEffectType_ExamChainEffectPerPassedTurn": 145,
    "ProduceExamEffectType_ExamChainEffectPerRemainTurn": 146,
    "ProduceExamEffectType_ExamLessonDependPlayCardCountSum": 147,
    "ProduceExamEffectType_ExamDebuffRecover": 148,
    "ProduceExamEffectType_ExamAggressiveValueMultiple": 149,
    "ProduceExamEffectType_ExamItemFireLimitAdd": 150,
    "ProduceExamEffectType_ExamReviewReduce": 151,
    "ProduceExamEffectType_ExamAggressiveReduce": 152,
    "ProduceExamEffectType_ExamLessonBuffReduce": 153,
    "ProduceExamEffectType_ExamParameterBuffReduce": 154,
    "ProduceExamEffectType_ExamLessonValueMultipleDown": 155,
    "ProduceExamEffectType_ExamAddGrowEffect": 156,
    "ProduceExamEffectType_ExamParameterBuffPerSearchCount": 157,
    "ProduceExamEffectType_ExamLessonBuffPerSearchCount": 158,
    "ProduceExamEffectType_ExamReviewPerSearchCount": 159,
    "ProduceExamEffectType_ExamAggressivePerSearchCount": 160,
    "ProduceExamEffectType_ExamBlockPerSearchCount": 161,
    "ProduceExamEffectType_ExamFullPowerPointPerSearchCount": 162,
    "ProduceExamEffectType_ExamLessonDependBlockAndSearchCount": 163,
    "ProduceExamEffectType_ExamLessonDependAggressiveAndSearchCount": 164,
    "ProduceExamEffectType_ExamLessonDependReviewAndSearchCount": 165,
    "ProduceExamEffectType_ExamEffectPerSearchCount": 166,
    "ProduceExamEffectType_ExamOverPreservation": 167,
    "ProduceExamEffectType_ExamParameterBuffDependLessonBuff": 168,
    "ProduceExamEffectType_ExamAggressiveDependReview": 169,
    "ProduceExamEffectType_ExamEnthusiasticAdditive": 170,
    "ProduceExamEffectType_ExamEnthusiasticMultiple": 171,
    "ProduceExamEffectType_ExamFullPowerLessonMultipleAdditive": 172,
    "ProduceExamEffectType_ExamConcentrationLessonMultipleAdditive": 173,
    "ProduceExamEffectType_ExamLessonBuffAdditive": 174,
    "ProduceExamEffectType_ExamParameterBuffAdditive": 175,
    "ProduceExamEffectType_ExamFullPowerPointAdditive": 178,
    "ProduceExamEffectType_ExamGrowEffectLessonAddAdditive": 179,
    "ProduceExamEffectType_ExamParameterBuffMultiplePerTurnReduce": 180,
    "ProduceExamEffectType_ExamLessonValueMultipleDependReviewOrAggressive": 181,
    "ProduceExamEffectType_ExamReviewMultiple": 182,
    "ProduceExamEffectType_ExamMultipleEnthusiasticLesson": 183,
    "ProduceExamEffectType_ExamMultipleConcentrationLesson": 184,
    "ProduceExamEffectType_ExamMultipleFullPowerLesson": 185,
    "ProduceExamEffectType_ExamLessonDependBlockConsumptionSum": 186,
    "ProduceExamEffectType_ExamForcePlayCardSearchWithCost": 187,
    "ProduceExamEffectType_ExamBlockDependBlockConsumptionSum": 188,
    "ProduceExamEffectType_ExamEnthusiasticTurnAdd": 189,
    "ProduceExamEffectType_ExamEffectTimerEndTurn": 190,
    "ProduceExamEffectType_ExamStanceLockConcentration": 191,
    "ProduceExamEffectType_ExamStanceLockFullPower": 192,
    "ProduceExamEffectType_ExamStanceLockPreservation": 193,
    "ProduceExamEffectType_ExamCardShuffleDeckGrave": 194,
    "ProduceExamEffectType_ExamReviewCountAdd": 195,
    "ProduceExamEffectType_ExamReviewTurnEndReduceLock": 196,
    "ProduceExamEffectType_ExamParameterBuffTurnEndReduceLock": 197,
    "ProduceExamEffectType_ExamBuffConsumptionDown": 198,
    "ProduceExamEffectType_ExamBuffConsumptionAdd": 199,
    "ProduceExamEffectType_ExamSearchPlayCardBuffConsumptionChange": 200,
    "ProduceExamEffectType_ExamPlayCardLimitPlayableValueAdd": 201,
    "ProduceExamEffectType_ExamReviewDependReviewConsumptionSum": 202,
    "ProduceExamEffectType_ExamLessonBuffReduceCancellable": 203,
    "ProduceExamEffectType_ExamParameterBuffReduceCancellable": 204,
    "ProduceExamEffectType_ExamAggressiveReduceCancellable": 205,
    "ProduceExamEffectType_ExamReviewReduceCancellable": 206,
    "ProduceExamEffectType_ExamFullPowerPointReduceCancellable": 207,
    "ProduceExamEffectType_ExamStatusEnchantTurnAdd": 208,
    "ProduceExamEffectType_ExamStatusEnchantCountAdd": 209,
    "ProduceExamEffectType_ExamParameterBuffAdditiveFix": 210,
    "ProduceExamEffectType_ExamLessonBuffAdditiveFix": 211,
    "ProduceExamEffectType_ExamAggressiveAdditiveFix": 212,
    "ProduceExamEffectType_ExamReviewAdditiveFix": 213,
    "ProduceExamEffectType_ExamFullPowerPointAdditiveFix": 214,
    "ProduceExamEffectType_ExamMoveGrowEffect": 215,
    "ProduceExamEffectType_ExamLessonDependEnthusiasticGetSum": 216,
    "ProduceExamEffectType_ExamCardShuffleDeckLost": 217,
    "ProduceExamEffectType_ExamFullPowerPointDependFullPowerPointGetSum": 218,
    "ProduceExamEffectType_ExamStatusEnchantEncore": 219,
}
# LocalSave stores the enum-backed effect payload as integers while Master
# keeps the corresponding names.  These tables are the v3.2.3 enum contract,
# not execution support: an unsupported effect can still be restored as an
# exact finite exhausted listener without being executed by this runtime.
_SERIALIZED_MOVE_POSITION_ENUM: Final = {
    "ProduceCardMovePositionType_Unknown": 0,
    "ProduceCardMovePositionType_Hand": 1,
    "ProduceCardMovePositionType_DeckFirst": 2,
    "ProduceCardMovePositionType_DeckLast": 3,
    "ProduceCardMovePositionType_DeckRandom": 4,
    "ProduceCardMovePositionType_Grave": 5,
    "ProduceCardMovePositionType_Lost": 6,
    "ProduceCardMovePositionType_Hold": 7,
}
_SERIALIZED_PICK_RANGE_ENUM: Final = {
    "ProducePickRangeType_Unknown": 0,
    "ProducePickRangeType_Select": 1,
    "ProducePickRangeType_Random": 2,
    "ProducePickRangeType_All": 3,
}
_SERIALIZED_PICK_COUNT_ENUM: Final = {
    "ProducePickCountType_Unknown": 0,
    "ProducePickCountType_Normal": 1,
    "ProducePickCountType_Shortage": 2,
    "ProducePickCountType_Over": 3,
}
# Item effects use the same Master enum as serialized child effects.  Keep one
# authoritative numeric table for both active dispatch records and exhausted
# listener restoration; the old executable subset is still enforced by
# ``_compile_effect`` and (later) by the effect-graph executor.
_EFFECT_ENUM.update(_SERIALIZED_EFFECT_ENUM)
_ITEM_FIELDS: Final = frozenset(
    {"_fireCount", "_id", "_itemType", "_parentCustomItemIds", "_reactionCount"}
)
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
_EMPTY_CARD_STATUS: Final = {
    "_effectGroupIdList": [],
    "_id": "",
    "_phaseCountDictionary": {"_list": []},
    "_produceCardGrowEffectIdList": [],
    "_produceExamTriggerId": "",
    "_spendCount": 0,
    "_spendTurn": 0,
    "_triggerCount": 0,
}
_EMPTY_TRIGGER_CARD: Final = {
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
    "_statusEffect": _EMPTY_CARD_STATUS,
    "_supportUpgradeIdList": [],
    "_tmpUpgradeCount": 0,
}
_EMPTY_TRIGGER_GIMMICK: Final = {
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
_ITEM_STATUS_NEUTRAL_IDENTITY: Final = {
    "_descriptionReactiveDataTextList": [],
    "_descriptionReactiveDataTypeList": [],
    "_fromExamEffectIdList": [],
    "_isEncoreEnchant": False,
    "_isFromEnchantEffect": False,
    "_originType": 1,
    "_overrideType": 31,
    "_startEnchantOriginId": "",
    "_startEnchantOriginLevel": 0,
    "_startEnchantOriginType": 0,
    "_startEnchantOwnerId": "",
    "_triggerCard": _EMPTY_TRIGGER_CARD,
    "_triggerDrink": {"_id": ""},
    "_triggerGimmickGroup": _EMPTY_TRIGGER_GIMMICK,
}
_LESSON_TYPE_VALUE_BY_NAME: Final = {
    "ProduceStepLessonType_Unknown": 0,
    "ProduceStepLessonType_Lesson": 1,
    "ProduceStepLessonType_LessonNormal": 2,
    "ProduceStepLessonType_LessonSp": 3,
    "ProduceStepLessonType_LessonHard": 4,
    "ProduceStepLessonType_LessonVocal": 5,
    "ProduceStepLessonType_LessonDance": 6,
    "ProduceStepLessonType_LessonVisual": 7,
    "ProduceStepLessonType_LessonVocalNormal": 8,
    "ProduceStepLessonType_LessonVocalSp": 9,
    "ProduceStepLessonType_LessonVocalHard": 10,
    "ProduceStepLessonType_LessonDanceNormal": 11,
    "ProduceStepLessonType_LessonDanceSp": 12,
    "ProduceStepLessonType_LessonDanceHard": 13,
    "ProduceStepLessonType_LessonVisualNormal": 14,
    "ProduceStepLessonType_LessonVisualSp": 15,
    "ProduceStepLessonType_LessonVisualHard": 16,
}


def _plain_int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _same_json_value(actual: object, expected: object) -> bool:
    """Compare serializer values without Python's bool/int aliasing."""

    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(  # type: ignore[arg-type]
            _same_json_value(actual[key], value)  # type: ignore[index]
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(  # type: ignore[arg-type]
            _same_json_value(left, right)
            for left, right in zip(actual, expected)  # type: ignore[arg-type]
        )
    return actual == expected


def plan2_native_lesson_trigger_is_start_valid(
    lesson_type: str,
    step_type_value: int,
) -> bool:
    """Exact Android v3.2.3 lesson listener installation gate.

    This is deliberately the start/install gate, not the ordinary runtime
    predicate.  Hard lessons widen installation for several lesson types, so
    reducing this check to an attribute equality would incorrectly discard a
    listener which can become active later in the stage.
    """

    value = _LESSON_TYPE_VALUE_BY_NAME.get(_text(lesson_type, "lesson_type"))
    if value is None:
        raise ValueError(f"unsupported lesson_type: {lesson_type}")
    _plain_int(step_type_value, "step_type_value", minimum=1)
    step_type_value = lesson_step_rule(step_type_value).predicate_equivalent_step
    if value in (0, 1):
        return True
    step_kind = (step_type_value - 1) % 3
    if value in (2, 3, 4):
        ordinary = step_kind == value - 2
    elif value in (5, 6, 7):
        ordinary = (step_type_value - 1) // 3 == value - 5
    else:
        ordinary = step_type_value == value - 7
    if ordinary:
        return True
    if step_type_value not in (3, 6, 9):
        return False
    return value not in {2, 3, 8, 9, 11, 12, 14, 15}


def plan2_native_lesson_trigger_is_runtime_valid(
    lesson_type: str,
    step_type_value: int,
    *,
    is_clear: bool,
) -> bool:
    """Exact Android v3.2.3 runtime lesson predicate."""

    if type(is_clear) is not bool:
        raise TypeError("is_clear must be boolean")
    value = _LESSON_TYPE_VALUE_BY_NAME.get(_text(lesson_type, "lesson_type"))
    if value is None:
        raise ValueError(f"unsupported lesson_type: {lesson_type}")
    _plain_int(step_type_value, "step_type_value", minimum=1)
    step_type_value = lesson_step_rule(step_type_value).predicate_equivalent_step
    if is_clear and step_type_value in (3, 6, 9) and value in (5, 6, 7):
        return True
    if value in (0, 1):
        return True
    step_kind = (step_type_value - 1) % 3
    if value in (2, 3, 4):
        return step_kind == value - 2
    if value in (5, 6, 7):
        return (step_type_value - 1) // 3 == value - 5
    return step_type_value == value - 7


@dataclass(frozen=True, slots=True)
class Plan2NativeItemSource:
    item_id: str
    fire_count: int = 0
    reaction_count: int = 0

    def __post_init__(self) -> None:
        _text(self.item_id, "item_id")
        _plain_int(self.fire_count, "fire_count")
        _plain_int(self.reaction_count, "reaction_count")


@dataclass(frozen=True, slots=True)
class Plan2NativeItemEffect:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    amount: int
    timer_child: Plan2NativeItemEffect | None = None

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        if self.effect_type not in _EFFECT_ENUM:
            raise ValueError(f"unsupported item effect type: {self.effect_type}")
        for name in ("value1", "value2", "effect_count", "amount"):
            _plain_int(getattr(self, name), name)
        _plain_int(self.effect_turn, "effect_turn", minimum=-1)
        if self.amount < 1:
            raise ValueError("item effect amount must be positive")
        if self.effect_type == EFFECT_TIMER:
            if not isinstance(self.timer_child, Plan2NativeItemEffect):
                raise TypeError("item timer requires a typed child")
        elif self.timer_child is not None:
            raise ValueError("non-timer item effect cannot carry a timer child")


@dataclass(frozen=True, slots=True)
class Plan2NativeItemTrigger:
    trigger_id: str
    phase: str
    interval: int = 0
    minimum_review: int = 0
    target_search: ProduceCardSearchRule | None = None
    lesson_type: str = LESSON_UNKNOWN
    changed_effect_type: str = ""
    minimum_status_difference: int = 0
    minimum_block: int = 0
    # The legacy fields above cover the first executable slice.  Every
    # mandatory Plan2 P-item now also retains the exact ordered Master
    # conjunction and its generic event-dispatch contract.  They are
    # intentionally optional for backwards compatibility with existing
    # hand-built fixtures and serialized callers.
    master_trigger: CompositeTriggerSpec | None = None
    event_dispatch: PItemEventDispatch | None = None

    def __post_init__(self) -> None:
        _text(self.trigger_id, "trigger_id")
        if self.phase not in _PHASE_ENUM:
            raise ValueError(f"unsupported item phase: {self.phase}")
        _plain_int(self.interval, "interval")
        _plain_int(self.minimum_review, "minimum_review")
        _plain_int(self.minimum_status_difference, "minimum_status_difference")
        _plain_int(self.minimum_block, "minimum_block")
        # Generic Master contracts are validated by the shared composite/event
        # parser.  The legacy branch below intentionally remains strict for
        # hand-built first-slice listeners, but must not reject a valid
        # CardPlay/StartExam/interval row before its generic contract is seen.
        generic_contract = self.master_trigger is not None or self.event_dispatch is not None
        if not generic_contract:
            if self.phase == ITEM_PHASE_CARD_PLAY_AFTER:
                if self.interval:
                    raise ValueError("CardPlayAfter cannot carry an interval")
                if (self.target_search is None) == (self.minimum_review == 0):
                    raise ValueError(
                        "CardPlayAfter requires exactly one target-search or ReviewUp predicate"
                    )
            elif self.target_search is not None:
                raise ValueError("non-card item phase cannot carry a target search")
            elif self.phase == ITEM_PHASE_END_TURN and self.interval < 1:
                raise ValueError("EndTurnInterval requires a positive interval")
            elif self.phase == ITEM_PHASE_TURN_INTERVAL:
                if self.interval < 1 or self.minimum_review < 1:
                    raise ValueError(
                        "ExamTurnInterval requires interval and ReviewUp threshold"
                    )
            elif self.phase == ITEM_PHASE_TURN_START and self.interval:
                raise ValueError("TurnStart cannot carry an interval")
            elif self.minimum_review and self.phase != ITEM_PHASE_TURN_START:
                raise ValueError(
                    "only CardPlayAfter, TurnStart or ExamTurnInterval can require ReviewUp"
                )
            if (
                self.phase not in {ITEM_PHASE_TURN_START, ITEM_PHASE_STATUS_CHANGE}
                and self.lesson_type != LESSON_UNKNOWN
            ):
                raise ValueError(
                    "only TurnStart or StatusChange can carry a lesson predicate"
                )
        if self.lesson_type not in _LESSON_TYPE_VALUE_BY_NAME:
            raise ValueError(f"unsupported lesson_type: {self.lesson_type}")
        if self.master_trigger is not None:
            if not isinstance(self.master_trigger, CompositeTriggerSpec):
                raise TypeError("master_trigger must be CompositeTriggerSpec or None")
            if self.master_trigger.phase != self.phase:
                raise ValueError("master_trigger phase differs from native trigger phase")
        if self.event_dispatch is not None:
            if not isinstance(self.event_dispatch, PItemEventDispatch):
                raise TypeError("event_dispatch must be PItemEventDispatch or None")
            if self.event_dispatch.trigger.phase != self.phase:
                raise ValueError("event_dispatch phase differs from native trigger phase")
            if self.master_trigger is not None and (
                self.event_dispatch.trigger.shape_fingerprint()
                != self.master_trigger.shape_fingerprint()
            ):
                raise ValueError("event_dispatch and master_trigger differ")
        elif self.master_trigger is not None:
            # A raw composite spec without a dispatch contract is useful for
            # read-only provenance but cannot be evaluated as a listener.  Do
            # not reject it; fail-closed dispatch will report missing event
            # evidence instead of manufacturing a predicate.
            pass
        if not generic_contract:
            if self.phase == ITEM_PHASE_STATUS_CHANGE:
                if (
                    self.interval
                    or self.minimum_review
                    or self.target_search is not None
                    or not self.changed_effect_type
                    or self.minimum_status_difference < 1
                ):
                    raise ValueError("StatusChange item trigger shape is unsupported")
            elif (
                self.changed_effect_type
                or self.minimum_status_difference
                or self.minimum_block
            ):
                raise ValueError(
                    "only StatusChange can carry a changed effect predicate"
                )


@dataclass(frozen=True, slots=True)
class Plan2NativeItemListener:
    source_item_id: str
    enchantment_id: str
    trigger: Plan2NativeItemTrigger
    effects: tuple[Plan2NativeItemEffect, ...]
    max_uses: int
    remaining_uses: int
    status_uid: int
    passing_turn_start: bool = True
    turn_count: int = 0
    serializer_progress: Plan2NativeSerializerProgress | None = None

    def __post_init__(self) -> None:
        _text(self.source_item_id, "source_item_id")
        _text(self.enchantment_id, "enchantment_id")
        if not isinstance(self.trigger, Plan2NativeItemTrigger):
            raise TypeError("trigger must be Plan2NativeItemTrigger")
        effects = tuple(self.effects)
        if not effects or any(not isinstance(value, Plan2NativeItemEffect) for value in effects):
            raise TypeError("effects must contain typed item effects")
        object.__setattr__(self, "effects", effects)
        _plain_int(self.max_uses, "max_uses")
        _plain_int(self.remaining_uses, "remaining_uses")
        if self.max_uses == 0:
            if self.remaining_uses != 0:
                raise ValueError("unlimited listener remaining_uses must be zero")
        elif self.remaining_uses > self.max_uses:
            raise ValueError("remaining_uses exceeds max_uses")
        _plain_int(self.status_uid, "status_uid", minimum=1)
        if type(self.passing_turn_start) is not bool:
            raise TypeError("passing_turn_start must be boolean")
        _plain_int(self.turn_count, "turn_count")
        progress = self.serializer_progress
        expected_total = -1 if self.max_uses == 0 else self.remaining_uses
        if progress is None:
            progress = Plan2NativeSerializerProgress(
                turn=-1,
                turn_count=self.turn_count,
                limit_count=expected_total,
                limit_count_in_turn_remaining=-1,
                is_passing_turn_start=self.passing_turn_start,
            )
        elif not isinstance(progress, Plan2NativeSerializerProgress):
            raise TypeError(
                "serializer_progress must be Plan2NativeSerializerProgress"
            )
        if (
            progress.turn != -1
            or progress.turn_count != self.turn_count
            or progress.limit_count != expected_total
            or progress.limit_count_in_turn_remaining != -1
            or progress.is_passing_turn_start is not self.passing_turn_start
        ):
            raise ValueError("serializer_progress conflicts with item listener state")
        object.__setattr__(self, "serializer_progress", progress)

    @property
    def uses(self) -> int:
        # Master effectCount=0 is Android's unlimited listener sentinel.  Its
        # LocalSave counter is -1 and it is never spent, so no finite usage
        # total can be derived from remaining uses.
        return 0 if self.max_uses == 0 else self.max_uses - self.remaining_uses


@dataclass(frozen=True, slots=True)
class Plan2NativeProvenExhaustedItemListener:
    """An exact finite native listener with no remaining activation.

    Its trigger/effect family is intentionally not executable by this runtime.
    The entry exists only after LocalSave identity, serialized Master effects,
    lifecycle, and ``remaining == 0`` have all been proven.  Keeping its UID
    prevents the shared native status allocator from reusing an active slot.
    """

    source_item_id: str
    enchantment_id: str
    trigger_id: str
    max_uses: int
    status_uid: int
    passing_turn_start: bool
    turn_count: int
    serializer_progress: Plan2NativeSerializerProgress | None = None

    def __post_init__(self) -> None:
        _text(self.source_item_id, "source_item_id")
        _text(self.enchantment_id, "enchantment_id")
        _text(self.trigger_id, "trigger_id")
        _plain_int(self.max_uses, "max_uses", minimum=1)
        _plain_int(self.status_uid, "status_uid", minimum=1)
        if type(self.passing_turn_start) is not bool:
            raise TypeError("passing_turn_start must be boolean")
        _plain_int(self.turn_count, "turn_count")
        progress = self.serializer_progress
        if progress is None:
            progress = Plan2NativeSerializerProgress(
                turn=-1,
                turn_count=self.turn_count,
                limit_count=0,
                limit_count_in_turn_remaining=-1,
                is_passing_turn_start=self.passing_turn_start,
            )
        elif not isinstance(progress, Plan2NativeSerializerProgress):
            raise TypeError(
                "serializer_progress must be Plan2NativeSerializerProgress"
            )
        if (
            progress.turn != -1
            or progress.turn_count != self.turn_count
            or progress.limit_count != 0
            or progress.limit_count_in_turn_remaining != -1
            or progress.is_passing_turn_start is not self.passing_turn_start
        ):
            raise ValueError(
                "serializer_progress conflicts with exhausted item listener state"
            )
        object.__setattr__(self, "serializer_progress", progress)


@dataclass(frozen=True, slots=True)
class Plan2NativeItemRuntime:
    sources: tuple[Plan2NativeItemSource, ...] = ()
    listeners: tuple[Plan2NativeItemListener, ...] = ()
    next_status_uid: int = 1
    proven_exhausted: tuple[Plan2NativeProvenExhaustedItemListener, ...] = ()

    def __post_init__(self) -> None:
        sources = tuple(self.sources)
        listeners = tuple(self.listeners)
        proven_exhausted = tuple(self.proven_exhausted)
        if any(not isinstance(value, Plan2NativeItemSource) for value in sources):
            raise TypeError("sources must contain typed item sources")
        if any(not isinstance(value, Plan2NativeItemListener) for value in listeners):
            raise TypeError("listeners must contain typed item listeners")
        if any(
            not isinstance(value, Plan2NativeProvenExhaustedItemListener)
            for value in proven_exhausted
        ):
            raise TypeError(
                "proven_exhausted must contain typed exhausted item listeners"
            )
        source_ids = tuple(value.item_id for value in sources)
        enchantment_ids = tuple(
            value.enchantment_id for value in (*listeners, *proven_exhausted)
        )
        listener_uids = tuple(value.status_uid for value in listeners)
        exhausted_uids = tuple(value.status_uid for value in proven_exhausted)
        uids = tuple(sorted((*listener_uids, *exhausted_uids)))
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("item source IDs must be unique")
        if len(enchantment_ids) != len(set(enchantment_ids)):
            raise ValueError("item enchantment IDs must be unique")
        if (
            len(listener_uids) != len(set(listener_uids))
            or listener_uids != tuple(sorted(listener_uids))
            or len(exhausted_uids) != len(set(exhausted_uids))
            or exhausted_uids != tuple(sorted(exhausted_uids))
            or len(uids) != len(set(uids))
        ):
            raise ValueError("item listener UIDs must be unique and ordered")
        if any(
            value.source_item_id not in source_ids
            for value in (*listeners, *proven_exhausted)
        ):
            raise ValueError("item listener source is not equipped")
        _plain_int(self.next_status_uid, "next_status_uid", minimum=1)
        if uids and self.next_status_uid <= max(uids):
            raise ValueError("next_status_uid must exceed active item UIDs")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "listeners", listeners)
        object.__setattr__(self, "proven_exhausted", proven_exhausted)

    @property
    def active_status_uids(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                (
                    *(value.status_uid for value in self.listeners),
                    *(value.status_uid for value in self.proven_exhausted),
                )
            )
        )

    @property
    def usage_counts(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            (value.enchantment_id, value.uses) for value in self.listeners
        ) + tuple(
            (value.enchantment_id, value.max_uses)
            for value in self.proven_exhausted
        )


def advance_plan2_native_item_turn_start(
    runtime: Plan2NativeItemRuntime,
) -> Plan2NativeItemRuntime:
    """Advance every retained item listener across one native TurnStart.

    Android retains equipped-item ``TriggerEffectStatusEffect`` objects after
    their final finite activation.  ``IncrementTurnCount`` therefore applies
    to active, zero-use, and proven-exhausted listeners alike, before the
    TurnStart predicate/effect dispatch.  Item source fire/reaction counters
    are presentation ledgers and are deliberately left untouched here.
    """

    if not isinstance(runtime, Plan2NativeItemRuntime):
        raise TypeError("runtime must be Plan2NativeItemRuntime")
    retained = (*runtime.listeners, *runtime.proven_exhausted)
    overflowing = next(
        (value for value in retained if value.turn_count == INT32_MAX),
        None,
    )
    if overflowing is not None:
        raise ValueError(
            "item-listener-turn-count-overflow:"
            f"{overflowing.enchantment_id}"
        )

    def advance(listener):
        progress = listener.serializer_progress
        assert isinstance(progress, Plan2NativeSerializerProgress)
        turn_count = listener.turn_count + 1
        return replace(
            listener,
            turn_count=turn_count,
            passing_turn_start=True,
            serializer_progress=replace(
                progress,
                turn_count=turn_count,
                is_passing_turn_start=True,
            ),
        )

    return replace(
        runtime,
        listeners=tuple(advance(value) for value in runtime.listeners),
        proven_exhausted=tuple(
            advance(value) for value in runtime.proven_exhausted
        ),
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeItemCompilation:
    runtime: Plan2NativeItemRuntime | None
    blockers: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return self.runtime is not None and not self.blockers


@dataclass(frozen=True, slots=True)
class Plan2NativeItemEvent:
    phase: str
    round_number: int
    card_id: str = ""
    card_upgrade: int = 0
    review: int = 0
    lesson_step_type_value: int = 0
    lesson_is_clear: bool = False
    is_lesson: bool | None = None
    changed_effect_type: str = ""
    status_difference: int = 0
    status_change_committed: bool = False
    lesson_type: str = LESSON_UNKNOWN
    block: int = 0
    # Generic P-item evidence.  The legacy scalar fields remain the canonical
    # bridge for the first listener slice; these maps are only consulted by a
    # listener carrying the shared event-dispatch contract.  Empty evidence is
    # intentionally preserved as unknown and therefore fails closed.
    phase_values: tuple[int, ...] = ()
    field_values: Mapping[str, int | bool] | None = None
    field_search_values: Mapping[tuple[str, str], int | bool] | None = None
    card_search_matches: Mapping[str, bool] | None = None
    effect_types: tuple[str, ...] = ()
    card_move_position_type: str = COMPOSITE_CARD_MOVE_UNKNOWN
    remaining_turn: int | None = None
    stamina_multiple: int | None = None
    aggressive: int | None = None
    current_stamina: int | None = None
    max_stamina: int | None = None
    card_search_counts: Mapping[tuple[str, str], int | bool] | None = None
    # CardPlay events carry the already-bound Playing card's Master category
    # and effect-group evidence.  The generic event dispatcher uses these
    # fields for ``ProduceCardPositionType_Playing`` searches; callers are not
    # expected to infer a match from an item or card identity.
    card_category: str = ""
    card_effect_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.phase not in _PHASE_ENUM:
            raise ValueError(f"unsupported item event phase: {self.phase}")
        _plain_int(self.round_number, "round_number", minimum=1)
        _plain_int(self.card_upgrade, "card_upgrade")
        _plain_int(self.review, "review")
        if self.phase in (ITEM_PHASE_CARD_PLAY, ITEM_PHASE_CARD_PLAY_AFTER):
            _text(self.card_id, "card_id")
        elif self.card_id:
            raise ValueError("non-card item event cannot carry a card")
        if self.phase not in (
            ITEM_PHASE_CARD_PLAY_AFTER,
            ITEM_PHASE_TURN_INTERVAL,
            ITEM_PHASE_TURN_START,
        ) and self.review and not (
            self.phase_values
            or self.field_values
            or self.field_search_values
            or self.card_search_matches
            or self.card_search_counts
            or self.effect_types
            or self.card_move_position_type != COMPOSITE_CARD_MOVE_UNKNOWN
            or self.lesson_type != LESSON_UNKNOWN
            or self.remaining_turn is not None
            or self.stamina_multiple is not None
            or self.aggressive is not None
        ):
            raise ValueError(
                "only CardPlayAfter, TurnStart or ExamTurnInterval can carry ReviewUp"
            )
        if type(self.lesson_is_clear) is not bool:
            raise TypeError("lesson_is_clear must be boolean")
        if self.is_lesson is not None and type(self.is_lesson) is not bool:
            raise TypeError("is_lesson must be boolean or None")
        _plain_int(self.lesson_step_type_value, "lesson_step_type_value")
        if self.lesson_step_type_value != 0 and self.lesson_step_type_value not in LESSON_STEP_VALUES:
            raise ValueError("lesson_step_type_value must be zero or a known native lesson step")
        phase_values = tuple(self.phase_values)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in phase_values
        ):
            raise ValueError("phase_values must contain non-negative integers")
        object.__setattr__(self, "phase_values", phase_values)
        for name in (
            "field_values",
            "field_search_values",
            "card_search_matches",
            "card_search_counts",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping or None")
            if value is not None:
                object.__setattr__(self, name, dict(value))
        if self.field_search_values is not None:
            for key in self.field_search_values:
                if (
                    not isinstance(key, tuple)
                    or len(key) != 2
                    or not isinstance(key[0], str)
                    or not key[0]
                    or not isinstance(key[1], str)
                    or not key[1]
                ):
                    raise ValueError("field_search_values keys must be (field, search) text tuples")
        if self.card_search_counts is not None:
            for key in self.card_search_counts:
                if (
                    not isinstance(key, tuple)
                    or len(key) != 2
                    or not isinstance(key[0], str)
                    or not key[0]
                    or not isinstance(key[1], str)
                    or not key[1]
                ):
                    raise ValueError("card_search_counts keys must be (field, search) text tuples")
        effects = tuple(self.effect_types)
        if any(not isinstance(value, str) or not value for value in effects):
            raise ValueError("effect_types must contain non-empty text")
        object.__setattr__(self, "effect_types", effects)
        if not isinstance(self.card_move_position_type, str) or not self.card_move_position_type:
            raise ValueError("card_move_position_type must be non-empty text")
        if not isinstance(self.card_category, str):
            raise TypeError("card_category must be text")
        card_effect_group_ids = tuple(self.card_effect_group_ids)
        if any(
            not isinstance(value, str) or not value
            for value in card_effect_group_ids
        ):
            raise ValueError(
                "card_effect_group_ids must contain non-empty text"
            )
        object.__setattr__(self, "card_effect_group_ids", card_effect_group_ids)
        for name in (
            "remaining_turn",
            "stamina_multiple",
            "aggressive",
            "current_stamina",
            "max_stamina",
        ):
            value = getattr(self, name)
            if value is not None:
                _plain_int(value, name, minimum=0)
        if (self.current_stamina is None) != (self.max_stamina is None):
            raise ValueError(
                "current_stamina and max_stamina must be supplied together"
            )
        if self.phase != ITEM_PHASE_TURN_START and (
            self.lesson_step_type_value
            or self.lesson_is_clear
            or self.is_lesson is not None
        ):
            raise ValueError("only TurnStart can carry lesson runtime facts")
        if isinstance(self.status_difference, bool) or not isinstance(
            self.status_difference, int
        ):
            raise TypeError("status_difference must be an integer")
        if type(self.status_change_committed) is not bool:
            raise TypeError("status_change_committed must be boolean")
        if self.lesson_type not in _LESSON_TYPE_VALUE_BY_NAME:
            raise ValueError(f"unsupported lesson_type: {self.lesson_type}")
        if self.phase == ITEM_PHASE_STATUS_CHANGE:
            _text(self.changed_effect_type, "changed_effect_type")
            _plain_int(self.block, "block")
        elif self.phase == ITEM_PHASE_TURN_START:
            _plain_int(self.block, "block")
            if (
                self.changed_effect_type
                or self.status_difference
                or self.status_change_committed
            ):
                raise ValueError(
                    "only StatusChange can carry status-change runtime facts"
                )
        elif (
            self.changed_effect_type
            or self.status_difference
            or self.status_change_committed
            or self.block
        ):
            raise ValueError(
                "only StatusChange can carry status-change runtime facts"
            )


@dataclass(frozen=True, slots=True)
class Plan2NativeItemDispatch:
    before: Plan2NativeItemRuntime
    after: Plan2NativeItemRuntime
    effects: tuple[Plan2NativeItemEffect, ...]
    fired_enchantment_ids: tuple[str, ...]
    trace: tuple[str, ...]


def _compile_effect(effect: MasterCardEffect) -> tuple[Plan2NativeItemEffect | None, str]:
    if effect.effect_type == EFFECT_TIMER:
        if (
            effect.value1 < 1
            or effect.value2 != 0
            or effect.effect_count != 1
            or effect.effect_turn != 0
            or not effect.chain_effect_id
            or effect.status_enchant_id
            or effect.trigger_id
            or effect.hide_icon
            or effect.once
            or effect.status_enchant_rule is not None
        ):
            return None, f"item-effect-shape-unsupported:{effect.id}"
        try:
            child_master = load_master_effect(effect.chain_effect_id)
        except (KeyError, OSError, TypeError, ValueError, sqlite3.Error):
            return None, f"item-effect-chain-unavailable:{effect.id}"
        child, issue = _compile_effect(child_master)
        if issue or child is None:
            return None, issue or f"item-effect-chain-unsupported:{effect.id}"
        if child.effect_type not in {EFFECT_LESSON_MULTIPLE, EFFECT_BLOCK,
                EFFECT_LESSON_DEPEND_BLOCK, EFFECT_LESSON_DEPEND_REVIEW, EFFECT_CARD_DRAW} or child.value2 != 0:
            return None, f"item-effect-chain-owner-unbound:{effect.id}:{child.effect_type}"
        return (
            Plan2NativeItemEffect(
                effect.id,
                effect.effect_type,
                effect.value1,
                effect.value2,
                effect.effect_count,
                effect.effect_turn,
                effect.value1,
                child,
            ),
            "",
        )
    neutral_links = (
        (effect.value2 == 0 or effect.effect_type in {EFFECT_BLOCK_ADD_MULTIPLE_AGGRESSIVE, EFFECT_LESSON_DEPEND_BLOCK})
        and not effect.status_enchant_id
        and not effect.chain_effect_id
        and not effect.trigger_id
        and effect.hide_icon is False
        and effect.once is False
        and effect.status_enchant_rule is None
    )
    if not neutral_links:
        return None, f"item-effect-shape-unsupported:{effect.id}"
    # Review/Aggressive additive effects are status installations, not a
    # one-shot scalar delta.  Keep their finite lifetime and source values in
    # the typed effect so the native Playing executor can hand them to the
    # corresponding dynamic-status owners in Master order.  Other finite
    # item effects remain fail-closed until an owner is bound; collapsing
    # those into an immediate amount would lose their lifecycle semantics.
    if effect.effect_type == EFFECT_REVIEW_MULTIPLE:
        if (
            effect.value1 <= 0
            or effect.effect_count != 0
            or effect.effect_turn == 0
            or effect.effect_turn < -1
        ):
            return None, f"item-effect-grammar-unsupported:{effect.effect_type}:{effect.id}"
        amount = effect.value1
    elif effect.effect_type in {EFFECT_REVIEW_ADDITIVE, EFFECT_AGGRESSIVE_ADDITIVE}:
        if effect.value1 <= 0 or effect.effect_count != 0 or effect.effect_turn <= 0:
            return None, f"item-effect-grammar-unsupported:{effect.effect_type}:{effect.id}"
        amount = effect.value1
    elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN:
        if (
            effect.value1 != 0
            or effect.value2 != 0
            or effect.effect_count != 0
            or effect.effect_turn < 1
        ):
            return None, f"item-effect-grammar-unsupported:{effect.effect_type}:{effect.id}"
        amount = effect.effect_turn
    elif effect.effect_type == EFFECT_LESSON_MULTIPLE_DOWN:
        if (
            effect.value1 <= 0
            or effect.value2 != 0
            or effect.effect_count != 0
            or effect.effect_turn < 1
        ):
            return None, f"item-effect-grammar-unsupported:{effect.effect_type}:{effect.id}"
        amount = effect.value1
    elif effect.effect_type == EFFECT_LESSON_MULTIPLE:
        if (
            effect.value1 <= 0
            or effect.value2 != 0
            or effect.effect_count != 0
            or effect.effect_turn < 1
        ):
            return None, f"item-effect-grammar-unsupported:{effect.effect_type}:{effect.id}"
        amount = effect.value1
    elif effect.effect_turn != 0:
        return None, f"item-effect-shape-unsupported:{effect.id}"
    elif effect.effect_type == EFFECT_LESSON_DEPEND_BLOCK:
        if effect.value1 <= 0 or effect.value2 not in (0, 500, 1000) or effect.effect_count < 1 or (effect.value2 and effect.effect_count != 1):
            return None, f"item-effect-grammar-unsupported:{effect.effect_type}:{effect.id}"
        amount = effect.value1
    elif effect.effect_type in {EFFECT_REVIEW_REDUCE, EFFECT_CARD_DRAW}:
        if effect.value1 <= 0 or effect.effect_count != 0:
            return None, f"item-effect-grammar-unsupported:{effect.effect_type}:{effect.id}"
        amount = effect.value1
    elif (
        effect.effect_type == EFFECT_BLOCK_ADD_MULTIPLE_AGGRESSIVE
        and effect.value1 > 0
        and effect.value2 >= 0
        and effect.effect_count == 1
    ):
        # Preserve both the base block and the additional Aggressive permil.
        # The horizon uses the existing CalculateAddBlock owner at execution;
        # compiling this into an ordinary flat Block would lose the multiplier.
        amount = effect.value1
    elif effect.effect_type == EFFECT_REVIEW and effect.value1 > 0 and effect.effect_count == 0:
        amount = effect.value1
    elif (
        effect.effect_type == EFFECT_BLOCK
        and effect.value1 > 0
        and effect.effect_count == 0
    ):
        # Direct ExamBlock uses the same CalculateAddBlock owner as cards,
        # drinks and gimmicks.  The item boundary keeps only the proven Master
        # amount here; CardPlayAggressive and StatusChange listeners are
        # resolved later by the native horizon in exact child order.
        amount = effect.value1
    elif (
        effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD
        and effect.value1 == 0
        and effect.effect_count > 0
    ):
        amount = effect.effect_count
    elif (
        effect.effect_type == EFFECT_STAMINA_REDUCE_FIX
        and effect.value1 > 0
        and effect.effect_count == 0
    ):
        amount = effect.value1
    elif (
        effect.effect_type == EFFECT_STAMINA_RECOVER_FIX
        and effect.value1 > 0
        and effect.effect_count == 0
    ):
        amount = effect.value1
    elif (
        effect.effect_type == EFFECT_AGGRESSIVE
        and effect.value1 > 0
        and effect.effect_count == 0
    ):
        amount = effect.value1
    elif (
        effect.effect_type == EFFECT_BLOCK_DEPEND_REVIEW
        and effect.value1 > 0
        and effect.effect_count == 1
    ):
        # Android BlockDependReview executes CalculateAddBlock once per
        # ``count``.  Restrict the first supported family to the observed
        # single-hit item so the horizon cannot silently collapse repeated
        # aggressive additions into one calculation.
        amount = effect.value1
    elif (
        effect.effect_type == EFFECT_BLOCK_FIX
        and effect.value1 > 0
        and effect.effect_count == 0
    ):
        amount = effect.value1
    elif (
        effect.effect_type == EFFECT_STAMINA_REDUCE
        and effect.value1 > 0
        and effect.effect_count == 0
    ):
        amount = effect.value1
    elif (
        effect.effect_type == EFFECT_LESSON_DEPEND_REVIEW
        and effect.value1 > 0
        and effect.effect_count > 0
    ):
        amount = effect.value1
    elif (
        effect.effect_type == EFFECT_LESSON_DEPEND_CARD_PLAY_AGGRESSIVE
        and effect.value1 > 0
        and effect.effect_count > 0
    ):
        # The native executor snapshots the current CardPlayAggressive value
        # at the item event and turns this permille into one or more lesson
        # hits.  Keep the ratio in ``amount``; the horizon applies it only
        # after the generic trigger has proved the target card.
        amount = effect.value1
    else:
        return None, f"item-effect-grammar-unsupported:{effect.effect_type}:{effect.id}"
    return (
        Plan2NativeItemEffect(
            effect.id,
            effect.effect_type,
            effect.value1,
            effect.value2,
            effect.effect_count,
            effect.effect_turn,
            amount,
        ),
        "",
    )


def _neutral_trigger(enchantment: ItemEnchantment) -> bool:
    trigger = enchantment.trigger
    return (
        not trigger.field_check_types
        and not trigger.field_types
        and not trigger.field_values
        and not trigger.field_card_search_ids
        and trigger.upper_search_count == 0
        and trigger.card_move_position_type == CARD_MOVE_UNKNOWN
        and not trigger.effect_types
        and trigger.lesson_type == LESSON_UNKNOWN
    )


def _compile_trigger_legacy(
    enchantment: ItemEnchantment,
) -> tuple[Plan2NativeItemTrigger | None, str]:
    trigger = enchantment.trigger
    if trigger.phase_types == (ITEM_PHASE_STATUS_CHANGE,):
        aggressive_lesson_shape = (
            not trigger.phase_values
            and not trigger.field_check_types
            and not trigger.field_types
            and not trigger.field_values
            and not trigger.field_card_search_ids
            and not trigger.produce_card_search_id
            and trigger.card_search_rule is None
            and trigger.upper_search_count == 0
            and trigger.lower_search_count == 0
            and trigger.card_move_position_type == CARD_MOVE_UNKNOWN
            and trigger.effect_types == (EFFECT_AGGRESSIVE,)
            and trigger.lesson_type == LESSON_DANCE
        )
        review_block_shape = (
            len(trigger.phase_values) == 1
            and trigger.phase_values[0] > 0
            and not trigger.field_check_types
            and trigger.field_types == ("ProduceExamFieldStatusType_BlockUp",)
            and len(trigger.field_values) == 1
            and trigger.field_values[0] > 0
            and not trigger.field_card_search_ids
            and not trigger.produce_card_search_id
            and trigger.card_search_rule is None
            and trigger.upper_search_count == 0
            and trigger.lower_search_count == 0
            and trigger.card_move_position_type == CARD_MOVE_UNKNOWN
            and trigger.effect_types == (EFFECT_REVIEW,)
            and trigger.lesson_type == LESSON_UNKNOWN
        )
        if not (aggressive_lesson_shape or review_block_shape):
            return None, f"item-trigger-status-change-shape-unsupported:{trigger.id}"
        if aggressive_lesson_shape:
            return (
                Plan2NativeItemTrigger(
                    trigger.id,
                    ITEM_PHASE_STATUS_CHANGE,
                    lesson_type=LESSON_DANCE,
                    changed_effect_type=EFFECT_AGGRESSIVE,
                    minimum_status_difference=1,
                ),
                "",
            )
        return (
            Plan2NativeItemTrigger(
                trigger.id,
                ITEM_PHASE_STATUS_CHANGE,
                changed_effect_type=EFFECT_REVIEW,
                minimum_status_difference=trigger.phase_values[0],
                minimum_block=trigger.field_values[0],
            ),
            "",
        )
    if trigger.phase_types == (ITEM_PHASE_TURN_INTERVAL,):
        if (
            len(trigger.phase_values) != 1
            or trigger.phase_values[0] < 1
            or trigger.field_check_types
            or trigger.field_types != (FIELD_REVIEW_UP,)
            or len(trigger.field_values) != 1
            or trigger.field_values[0] < 1
            or trigger.field_card_search_ids
            or trigger.produce_card_search_id
            or trigger.upper_search_count != 0
            or trigger.lower_search_count != 0
            or trigger.card_move_position_type != CARD_MOVE_UNKNOWN
            or trigger.effect_types
            or trigger.lesson_type != LESSON_UNKNOWN
        ):
            return None, f"item-trigger-turn-interval-shape-unsupported:{trigger.id}"
        return (
            Plan2NativeItemTrigger(
                trigger.id,
                ITEM_PHASE_TURN_INTERVAL,
                interval=trigger.phase_values[0],
                minimum_review=trigger.field_values[0],
            ),
            "",
        )
    if trigger.phase_types == (ITEM_PHASE_CARD_PLAY_AFTER,):
        review_up_shape = (
            not trigger.phase_values
            and not trigger.field_check_types
            and trigger.field_types == (FIELD_REVIEW_UP,)
            and len(trigger.field_values) == 1
            and trigger.field_values[0] > 0
            and not trigger.field_card_search_ids
            and not trigger.produce_card_search_id
            and trigger.card_search_rule is None
            and trigger.upper_search_count == 0
            and trigger.lower_search_count == 1
            and trigger.card_move_position_type == CARD_MOVE_UNKNOWN
            and not trigger.effect_types
            and trigger.lesson_type == LESSON_UNKNOWN
        )
        if review_up_shape:
            return (
                Plan2NativeItemTrigger(
                    trigger.id,
                    ITEM_PHASE_CARD_PLAY_AFTER,
                    minimum_review=trigger.field_values[0],
                ),
                "",
            )
        if not _neutral_trigger(enchantment):
            return None, f"item-trigger-field-unsupported:{trigger.id}"
        if (
            trigger.phase_values
            or trigger.lower_search_count != 1
            or not trigger.produce_card_search_id
            or trigger.card_search_rule is None
        ):
            return None, f"item-trigger-card-play-after-shape-unsupported:{trigger.id}"
        matched, reason = match_exact_target_card_search(
            trigger.card_search_rule,
            trigger.card_search_rule.produce_card_ids[0]
            if trigger.card_search_rule.produce_card_ids
            else "",
            trigger.card_search_rule.upgrade_counts[0]
            if trigger.card_search_rule.upgrade_counts
            else 0,
        )
        if reason is not None or not matched:
            return None, reason or f"item-trigger-target-search-empty:{trigger.id}"
        return (
            Plan2NativeItemTrigger(
                trigger.id,
                ITEM_PHASE_CARD_PLAY_AFTER,
                target_search=trigger.card_search_rule,
            ),
            "",
        )
    if trigger.phase_types == (ITEM_PHASE_END_TURN,):
        if not _neutral_trigger(enchantment):
            return None, f"item-trigger-field-unsupported:{trigger.id}"
        if (
            len(trigger.phase_values) != 1
            or trigger.phase_values[0] < 1
            or trigger.lower_search_count != 0
            or trigger.produce_card_search_id
        ):
            return None, f"item-trigger-end-turn-shape-unsupported:{trigger.id}"
        return (
            Plan2NativeItemTrigger(
                trigger.id,
                ITEM_PHASE_END_TURN,
                interval=trigger.phase_values[0],
            ),
            "",
        )
    if trigger.phase_types == (ITEM_PHASE_TURN_START,):
        review_up_shape = (
            not trigger.phase_values
            and not trigger.field_check_types
            and trigger.field_types == (FIELD_REVIEW_UP,)
            and len(trigger.field_values) == 1
            and trigger.field_values[0] > 0
            and not trigger.field_card_search_ids
            and trigger.lower_search_count == 0
            and not trigger.produce_card_search_id
            and trigger.upper_search_count == 0
            and trigger.card_move_position_type == CARD_MOVE_UNKNOWN
            and not trigger.effect_types
            and trigger.lesson_type == LESSON_UNKNOWN
        )
        if review_up_shape:
            return (
                Plan2NativeItemTrigger(
                    trigger.id,
                    ITEM_PHASE_TURN_START,
                    minimum_review=trigger.field_values[0],
                ),
                "",
            )
        if (
            trigger.phase_values
            or trigger.field_check_types
            or trigger.field_types
            or trigger.field_values
            or trigger.field_card_search_ids
            or trigger.lower_search_count != 0
            or trigger.produce_card_search_id
            or trigger.upper_search_count != 0
            or trigger.card_move_position_type != CARD_MOVE_UNKNOWN
            or trigger.effect_types
            or trigger.lesson_type not in _LESSON_TYPE_VALUE_BY_NAME
        ):
            return None, f"item-trigger-turn-start-shape-unsupported:{trigger.id}"
        return (
            Plan2NativeItemTrigger(
                trigger.id,
                ITEM_PHASE_TURN_START,
                lesson_type=trigger.lesson_type,
            ),
            "",
        )
    return None, f"item-trigger-phase-unsupported:{trigger.id}:{trigger.phase_types!r}"


def _compile_trigger(
    enchantment: ItemEnchantment,
    *,
    source_item_id: str | None = None,
) -> tuple[Plan2NativeItemTrigger | None, str]:
    """Compile one trigger through the shared Master-order contract first.

    The legacy compiler still supplies the specialized runtime fields for the
    already executable slice.  When that slice does not recognize a valid
    Master row, retain a generic listener contract instead of collapsing the
    row into an item-ID branch.  Effect execution remains a separate gate:
    the generic listener can be restored and evaluated, while unsupported
    children continue to fail closed in ``_compile_effect``.
    """

    try:
        master_trigger = parse_composite_trigger(
            enchantment.trigger,
            card_search_rule=enchantment.trigger.card_search_rule,
        )
    except (TypeError, ValueError) as error:
        return None, (
            "item-trigger-composite-shape-unsupported:"
            f"{enchantment.trigger.id}:{type(error).__name__}:{error}"
        )
    try:
        dispatch = parse_plan2_pitem_event_dispatch(
            enchantment.trigger,
            card_search_rule=enchantment.trigger.card_search_rule,
        )
    except (TypeError, ValueError) as error:
        # The legacy CardPlayAfter ReviewUp row uses lowerSearchCount=1 as a
        # native card-boundary marker without a produce-card-search ID.  The
        # first scalar runtime already proves that shape; retain its exact
        # composite Master spec while leaving generic dispatch unbound.
        legacy, legacy_issue = _compile_trigger_legacy(enchantment)
        if legacy is not None and not legacy_issue:
            return replace(legacy, master_trigger=master_trigger), ""
        return None, (
            "item-trigger-composite-shape-unsupported:"
            f"{enchantment.trigger.id}:{type(error).__name__}:{error}"
        )
    legacy, issue = _compile_trigger_legacy(enchantment)
    if legacy is not None and not issue:
        return replace(
            legacy,
            master_trigger=master_trigger,
            event_dispatch=dispatch,
        ), ""
    if issue and source_item_id and source_item_id.startswith("pitem_"):
        # When compiling a real Master item, a caller-supplied mutation of a
        # known legacy row must not be silently widened into the generic
        # contract.  Compare the source trigger by enchantment identity; this
        # is provenance validation, not an item-ID execution branch.  Custom
        # fixtures (which do not exist in Master) remain generic-testable.
        try:
            canonical_rule = load_item_rule(source_item_id)
            canonical = next(
                value
                for value in canonical_rule.enchantments
                if value.id == enchantment.id
            )
        except (KeyError, OSError, TypeError, ValueError, StopIteration):
            canonical = None
        if canonical is not None and canonical.trigger != enchantment.trigger:
            return None, issue
    # Generic contracts have no legacy interval/card/status projection.  The
    # event dispatcher owns their exact positional predicates and phase
    # arguments; leave scalar fields at their neutral values.
    try:
        generic = Plan2NativeItemTrigger(
            enchantment.trigger.id,
            dispatch.phase.value,
            master_trigger=master_trigger,
            event_dispatch=dispatch,
        )
    except (TypeError, ValueError) as error:
        return None, (
            "item-trigger-composite-runtime-unsupported:"
            f"{enchantment.trigger.id}:{type(error).__name__}:{error}"
        )
    return generic, ""


def compile_plan2_native_item_rules(
    rules: Sequence[EquippedItemRule],
    *,
    first_status_uid: int = 1,
    sources: Sequence[Plan2NativeItemSource] | None = None,
) -> Plan2NativeItemCompilation:
    """Compile equipped items in exact source/enchantment/effect order."""

    _plain_int(first_status_uid, "first_status_uid", minimum=1)
    rules = tuple(rules)
    if any(not isinstance(value, EquippedItemRule) for value in rules):
        raise TypeError("rules must contain EquippedItemRule")
    runtime_sources = (
        tuple(Plan2NativeItemSource(value.id) for value in rules)
        if sources is None
        else tuple(sources)
    )
    if tuple(value.item_id for value in runtime_sources) != tuple(value.id for value in rules):
        return Plan2NativeItemCompilation(None, ("item-source-order-mismatch",))
    blockers: list[str] = []
    listeners: list[Plan2NativeItemListener] = []
    next_uid = first_status_uid
    for item in rules:
        blockers.extend(f"item-rule-unsupported:{item.id}:{value}" for value in item.unsupported_rules)
        if not item.enchantments:
            blockers.append(f"item-enchantment-missing:{item.id}")
        for enchantment in item.enchantments:
            trigger, issue = _compile_trigger(
                enchantment,
                source_item_id=item.id,
            )
            if issue:
                blockers.append(issue)
                continue
            effects: list[Plan2NativeItemEffect] = []
            for effect in enchantment.effects:
                compiled, effect_issue = _compile_effect(effect)
                if effect_issue:
                    blockers.append(effect_issue)
                elif compiled is not None:
                    effects.append(compiled)
            if len(effects) != len(enchantment.effects) or not effects or trigger is None:
                continue
            listeners.append(
                Plan2NativeItemListener(
                    item.id,
                    enchantment.id,
                    trigger,
                    tuple(effects),
                    enchantment.max_uses,
                    enchantment.max_uses,
                    next_uid,
                )
            )
            next_uid += 1
    blockers = list(dict.fromkeys(blockers))
    if blockers:
        return Plan2NativeItemCompilation(None, tuple(blockers))
    return Plan2NativeItemCompilation(
        Plan2NativeItemRuntime(runtime_sources, tuple(listeners), next_uid)
    )


def _generic_event_context(
    trigger: CompositeTriggerSpec,
    event: Plan2NativeItemEvent,
) -> PItemPredicateContext:
    """Build typed event evidence without filling unknown values with zero."""

    fields: dict[str, int | bool] = {}
    if event.field_values is not None:
        fields.update(event.field_values)
    # These scalar aliases are evidence observed at the native boundary.  We
    # only project them for fields whose value is explicitly carried by the
    # event; arbitrary Master fields remain missing and evaluate to ``None``.
    aliases: dict[str, int | None] = {
        "ProduceExamFieldStatusType_ReviewUp": event.review,
        "ProduceExamFieldStatusType_BlockUp": event.block,
        "ProduceExamFieldStatusType_CardPlayAggressiveUp": event.aggressive,
        "ProduceExamFieldStatusType_RemainingTurn": event.remaining_turn,
        "ProduceExamFieldStatusType_StaminaUpMultiple": event.stamina_multiple,
    }
    for predicate in trigger.fields:
        if predicate.card_search_id:
            continue
        value = aliases.get(predicate.field_type)
        if value is not None and predicate.field_type not in fields:
            fields[predicate.field_type] = value
    field_searches: dict[tuple[str, str], int | bool] = {}
    if event.field_search_values is not None:
        field_searches.update(event.field_search_values)
    if event.card_search_counts is not None:
        for key, value in event.card_search_counts.items():
            field_searches.setdefault(key, value)
    matches = {} if event.card_search_matches is None else dict(event.card_search_matches)
    # A direct card search can be evaluated by the shared layer only when the
    # caller supplies card identity and upgrade.  It deliberately does not
    # infer a match from the listener's target rule alone.
    effect_types = event.effect_types
    if not effect_types and event.changed_effect_type:
        effect_types = (event.changed_effect_type,)
    return PItemPredicateContext(
        phase=event.phase,
        phase_values=event.phase_values,
        field_values=fields,
        field_search_values=field_searches,
        card_search_matches=matches,
        card_id=event.card_id,
        card_upgrade=event.card_upgrade if event.card_id else None,
        card_category=event.card_category,
        card_effect_group_ids=event.card_effect_group_ids,
        effect_types=effect_types,
        card_move_position_type=event.card_move_position_type,
        lesson_type=event.lesson_type,
    )


def _has_legacy_runtime_shape(trigger: Plan2NativeItemTrigger) -> bool:
    """Whether the first native slice has enough scalar fields to dispatch.

    Generic contracts are attached to every compiled listener for provenance,
    including listeners already handled by the old scalar bridge.  Selecting
    the bridge here keeps existing callers' round/lesson semantics intact;
    rows without one of these complete shapes use the generic evaluator.
    """

    if trigger.phase == ITEM_PHASE_STATUS_CHANGE:
        return bool(
            trigger.changed_effect_type
            and trigger.minimum_status_difference > 0
        )
    if trigger.phase == ITEM_PHASE_CARD_PLAY_AFTER:
        return trigger.target_search is not None or trigger.minimum_review > 0
    if trigger.phase in {
        ITEM_PHASE_END_TURN,
        ITEM_PHASE_TURN_INTERVAL,
    }:
        return trigger.interval > 0 and (
            trigger.phase == ITEM_PHASE_END_TURN
            or trigger.minimum_review > 0
        )
    if trigger.phase == ITEM_PHASE_TURN_START:
        return bool(trigger.lesson_type != LESSON_UNKNOWN or trigger.minimum_review > 0)
    return False


def _listener_fires(
    listener: Plan2NativeItemListener,
    event: Plan2NativeItemEvent,
) -> tuple[bool, str]:
    trigger = listener.trigger
    if trigger.phase != event.phase or (
        listener.max_uses > 0 and listener.remaining_uses == 0
    ):
        return False, ""
    generic_runtime = (
        trigger.master_trigger is not None
        and not _has_legacy_runtime_shape(trigger)
    )
    generic_event = event
    if generic_runtime and trigger.master_trigger is not None:
        expected_phase_values = trigger.master_trigger.phase_values
        if event.phase_values and event.phase_values != expected_phase_values:
            # The native phase value is a proved predicate mismatch for this
            # listener, not missing evidence.  Other listeners sharing the
            # same phase may legitimately bind a different interval value.
            return False, ""
        if (
            trigger.phase == ITEM_PHASE_END_TURN
            and not expected_phase_values
        ):
            return False, "generic-end-turn-interval-value-missing"
        # EndTurnInterval's phaseValues are the Master interval argument, not
        # a per-event counter.  The horizon event may omit it because a single
        # event is shared by listeners with different intervals; evaluate each
        # listener against its own exact Master vector after applying the
        # round gate below.
        if trigger.phase == ITEM_PHASE_END_TURN and not event.phase_values:
            generic_event = replace(
                event,
                phase_values=expected_phase_values,
            )
        interval = (
            expected_phase_values[0]
            if trigger.phase == ITEM_PHASE_END_TURN
            and len(expected_phase_values) == 1
            else None
        )
        if trigger.phase == ITEM_PHASE_END_TURN:
            if interval is None or interval < 1:
                return False, "generic-end-turn-interval-shape-invalid"
            if event.round_number % interval:
                return False, ""
        stamina_predicates = tuple(
            predicate
            for predicate in trigger.master_trigger.fields
            if (
                predicate.field_type == FIELD_STAMINA_UP_MULTIPLE
                and not predicate.card_search_id
            )
        )
        if stamina_predicates and (
            event.field_values is None
            or FIELD_STAMINA_UP_MULTIPLE not in event.field_values
        ):
            if len(stamina_predicates) != 1:
                return False, "item-stamina-multiple-predicate-count-unproven"
            predicate = stamina_predicates[0]
            if predicate.value is None:
                return False, "item-stamina-multiple-threshold-missing"
            if event.current_stamina is None or event.max_stamina is None:
                return False, "item-stamina-multiple-snapshot-missing"
            evaluated = evaluate_stamina_multiple(
                StaminaField.STAMINA_UP_MULTIPLE,
                predicate.value,
                StaminaSnapshot(
                    event.current_stamina,
                    event.max_stamina,
                ),
            )
            if evaluated.fires is None:
                return False, (
                    "item-stamina-multiple-evaluation-unresolved:"
                    + ",".join(evaluated.reasons)
                )
            field_values = dict(event.field_values or {})
            field_values[FIELD_STAMINA_UP_MULTIPLE] = (
                predicate.value
                if evaluated.fires
                else max(0, predicate.value - 1)
            )
            generic_event = replace(
                generic_event,
                field_values=field_values,
            )
        produce_search_id = trigger.master_trigger.produce_card_search_id
        produce_search = trigger.master_trigger.card_search_rule
        if (
            produce_search_id
            and produce_search is not None
            and produce_search_id not in (generic_event.card_search_matches or {})
            and produce_search.card_position_type
            == "ProduceCardPositionType_Playing"
        ):
            if not generic_event.card_id:
                return False, "item-playing-card-search-input-missing"
            identity_filters = bool(
                produce_search.produce_card_ids
                or produce_search.upgrade_counts
            )
            context_filters = bool(
                produce_search.card_categories
                or produce_search.effect_group_ids
            )
            if identity_filters and context_filters:
                return False, (
                    "item-playing-card-search-mixed-filter-shape:"
                    f"{produce_search.id}"
                )
            if context_filters:
                matched, issue = match_exact_playing_card_search(
                    produce_search,
                    card_category=generic_event.card_category,
                    card_effect_group_ids=(
                        generic_event.card_effect_group_ids
                    ),
                    expected_categories=produce_search.card_categories,
                    expected_effect_group_ids=(
                        produce_search.effect_group_ids
                    ),
                )
            else:
                matched, issue = match_plan3_card_move_search(
                    produce_search,
                    generic_event.card_id,
                    generic_event.card_upgrade,
                )
            if issue is not None:
                return False, f"item-playing-card-search-unresolved:{issue}"
            card_search_matches = dict(
                generic_event.card_search_matches or {}
            )
            card_search_matches[produce_search_id] = matched
            generic_event = replace(
                generic_event,
                card_search_matches=card_search_matches,
            )
    if trigger.event_dispatch is not None and not _has_legacy_runtime_shape(trigger):
        try:
            evaluation = evaluate_plan2_pitem_event_dispatch(
                trigger.event_dispatch,
                _generic_event_context(
                    trigger.master_trigger or trigger.event_dispatch.trigger,
                    generic_event,
                ),
            )
        except (TypeError, ValueError) as error:
            return False, f"generic-event-invalid:{type(error).__name__}:{error}"
        if evaluation.fires is None:
            return False, "generic-event-evidence-missing:" + ",".join(evaluation.reasons)
        return evaluation.fires, ""
    if event.phase == ITEM_PHASE_CARD_PLAY_AFTER:
        if trigger.target_search is not None:
            matched, issue = match_exact_target_card_search(
                trigger.target_search,
                event.card_id,
                event.card_upgrade,
            )
            return matched, "" if issue is None else issue
        return event.review >= trigger.minimum_review, ""
    if event.phase == ITEM_PHASE_END_TURN:
        return event.round_number % trigger.interval == 0, ""
    if event.phase == ITEM_PHASE_TURN_INTERVAL:
        return (
            event.round_number % trigger.interval == 0
            and event.review >= trigger.minimum_review
        ), ""
    if event.phase == ITEM_PHASE_TURN_START and trigger.minimum_review:
        return event.review >= trigger.minimum_review, ""
    if event.phase == ITEM_PHASE_TURN_START and trigger.lesson_type != LESSON_UNKNOWN:
        # Auditions share ExamStartTurn with lessons.  A bare audition event
        # (``is_lesson=False`` with no parameter slot) remains inapplicable,
        # preserving the fail-closed behaviour of older callers.  The native
        # ExamSave bridge can, however, carry the current audition parameter
        # as a 1..3 lesson-step-compatible value; in that case Android applies
        # the same Vocal/Dance/Visual predicate to the listener.
        if event.is_lesson is False and event.lesson_step_type_value == 0:
            return False, ""
        if event.lesson_step_type_value == 0:
            return False, "item-turn-start-lesson-runtime-missing"
        try:
            return (
                plan2_native_lesson_trigger_is_runtime_valid(
                    trigger.lesson_type,
                    event.lesson_step_type_value,
                    is_clear=event.lesson_is_clear,
                ),
                "",
            )
        except (TypeError, ValueError) as error:
            return False, f"item-turn-start-lesson-runtime-invalid:{error}"
    if event.phase == ITEM_PHASE_STATUS_CHANGE:
        return (
            event.status_change_committed
            and event.status_difference >= trigger.minimum_status_difference
            and event.changed_effect_type == trigger.changed_effect_type
            and event.block >= trigger.minimum_block
            and (
                trigger.lesson_type == LESSON_UNKNOWN
                or event.lesson_type == trigger.lesson_type
            )
        ), ""
    return True, ""


def dispatch_plan2_native_item_event(
    runtime: Plan2NativeItemRuntime,
    event: Plan2NativeItemEvent,
) -> Plan2NativeItemDispatch:
    """Resolve one real phase and spend uses only after an exact match."""

    if not isinstance(runtime, Plan2NativeItemRuntime):
        raise TypeError("runtime must be Plan2NativeItemRuntime")
    if not isinstance(event, Plan2NativeItemEvent):
        raise TypeError("event must be Plan2NativeItemEvent")
    listeners: list[Plan2NativeItemListener] = []
    effects: list[Plan2NativeItemEffect] = []
    fired: list[str] = []
    source_fire_deltas: dict[str, int] = {}
    # Retained-card replay must re-dispatch CardPlayAfter from the exact
    # post-card/pre-item Review value.  Keep that native event input in the
    # deterministic trace; using the settled pre-effect LocalSave scalar would
    # incorrectly read zero for ReviewUp listeners.
    trace: list[str] = [
        f"item-phase:{event.phase}",
        f"native-item-event:{event.phase}:{event.card_id}@{event.card_upgrade}:"
        f"review={event.review}",
    ]
    if (
        event.phase_values
        or event.field_values
        or event.field_search_values
        or event.card_search_matches
        or event.card_search_counts
        or event.effect_types
        or event.card_move_position_type != COMPOSITE_CARD_MOVE_UNKNOWN
        or event.lesson_type != LESSON_UNKNOWN
        or event.remaining_turn is not None
        or event.stamina_multiple is not None
        or event.aggressive is not None
        or event.current_stamina is not None
        or event.max_stamina is not None
        or event.card_category
        or event.card_effect_group_ids
    ):
        # Keep generic evidence in one canonical, item-ID-independent trace
        # record so retained journal replay can reconstruct the exact event
        # without scraping localized descriptions or guessing missing fields.
        trace.append(
            "native-pitem-context:"
            + json.dumps(
                {
                    "phase_values": list(event.phase_values),
                    "field_values": event.field_values or {},
                    "field_search_values": [
                        [list(key), value]
                        for key, value in sorted(
                            (event.field_search_values or {}).items(),
                            key=lambda pair: repr(pair[0]),
                        )
                    ],
                    "card_search_matches": event.card_search_matches or {},
                    "card_search_counts": [
                        [list(key), value]
                        for key, value in sorted(
                            (event.card_search_counts or {}).items(),
                            key=lambda pair: repr(pair[0]),
                        )
                    ],
                    "effect_types": list(event.effect_types),
                    "card_move_position_type": event.card_move_position_type,
                    "lesson_type": event.lesson_type,
                    "remaining_turn": event.remaining_turn,
                    "stamina_multiple": event.stamina_multiple,
                    "aggressive": event.aggressive,
                    "current_stamina": event.current_stamina,
                    "max_stamina": event.max_stamina,
                    "card_category": event.card_category,
                    "card_effect_group_ids": list(
                        event.card_effect_group_ids
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    if event.phase == ITEM_PHASE_STATUS_CHANGE:
        trace.append(
            "native-item-status-change:"
            f"{event.changed_effect_type}:difference={event.status_difference}:"
            f"block={event.block}:lesson={event.lesson_type}"
        )
    for listener in runtime.listeners:
        does_fire, issue = _listener_fires(listener, event)
        if issue:
            raise ValueError(f"item-trigger-dispatch-unsupported:{listener.enchantment_id}:{issue}")
        if not does_fire:
            listeners.append(listener)
            continue
        remaining_after = (
            0
            if listener.max_uses == 0
            else listener.remaining_uses - 1
        )
        progress = listener.serializer_progress
        assert isinstance(progress, Plan2NativeSerializerProgress)
        listeners.append(
            replace(
                listener,
                remaining_uses=remaining_after,
                serializer_progress=replace(
                    progress,
                    limit_count=(
                        -1 if listener.max_uses == 0 else remaining_after
                    ),
                ),
            )
        )
        fired.append(listener.enchantment_id)
        effects.extend(listener.effects)
        if event.phase == ITEM_PHASE_TURN_START and listener.max_uses == 0:
            # The proven unlimited TurnStart command grammar emits one
            # item-bearing SeparateTrigger-start command followed by one
            # item-bearing PlayEffect command per child.  Android
            # ExecuteCommandImpl calls ProduceItemData.AddFireCount once for
            # every such command.  Finite listeners have an additional
            # materialization/spend boundary and remain outside this slice.
            fire_delta = 1 + len(listener.effects)
            source_fire_deltas[listener.source_item_id] = (
                source_fire_deltas.get(listener.source_item_id, 0)
                + fire_delta
            )
            trace.append(
                "item-source-fire-count:unlimited-turn-start:"
                f"{listener.source_item_id}:+{fire_delta}"
            )
        trace.append(
            f"item-fire:{listener.source_item_id}:{listener.enchantment_id}:"
            f"uid={listener.status_uid}:remaining={remaining_after}"
        )
    sources: list[Plan2NativeItemSource] = []
    for source in runtime.sources:
        delta = source_fire_deltas.get(source.item_id, 0)
        if delta and source.fire_count > INT32_MAX - delta:
            raise ValueError(
                "item-source-fire-count-overflow:"
                f"{source.item_id}:{source.fire_count}+{delta}"
            )
        sources.append(
            source
            if not delta
            else replace(source, fire_count=source.fire_count + delta)
        )
    after = replace(
        runtime,
        sources=tuple(sources),
        listeners=tuple(listeners),
    )
    return Plan2NativeItemDispatch(
        runtime,
        after,
        tuple(effects),
        tuple(fired),
        tuple(trace),
    )


def _parse_source(raw: object, index: int) -> tuple[Plan2NativeItemSource | None, str]:
    label = f"itemList[{index}]"
    if not isinstance(raw, Mapping) or set(raw) != set(_ITEM_FIELDS):
        return None, f"local-save-item-shape-unsupported:{label}"
    try:
        item_id = _text(raw["_id"], f"{label}._id")
        fire_count = _plain_int(raw["_fireCount"], f"{label}._fireCount")
        reaction_count = _plain_int(raw["_reactionCount"], f"{label}._reactionCount")
        item_type = _plain_int(raw["_itemType"], f"{label}._itemType")
    except (TypeError, ValueError) as error:
        return None, f"local-save-item-field-invalid:{error}"
    # ``_fireCount`` / ``_reactionCount`` are persisted usage ledgers, not
    # proof that an equipped item owns an Exam listener.  Outer-only items can
    # legitimately enter an exam with a non-zero reaction count after their
    # weekly/reward effect has already fired (for example the Initial badge).
    # Keep both counters as typed evidence and let the Master rule/status
    # reconciliation below decide whether an inner listener exists.
    if item_type != 1 or raw["_parentCustomItemIds"] != []:
        return None, f"local-save-item-source-unsupported:{item_id}"
    return Plan2NativeItemSource(item_id, fire_count, reaction_count), ""


def _raw_item_effect_matches(raw: object, effect: Plan2NativeItemEffect) -> bool:
    if not isinstance(raw, Mapping):
        return False
    expected = {
        "_id": effect.effect_id,
        "_effectType": _EFFECT_ENUM[effect.effect_type],
        "_effectValue1": effect.value1,
        "_effectValue2": effect.value2,
        "_effectCount": effect.effect_count,
        "_effectTurn": effect.effect_turn,
        "_statusEnchantId": "",
        "_chainEffectId": (
            effect.timer_child.effect_id
            if effect.effect_type == EFFECT_TIMER and effect.timer_child is not None
            else ""
        ),
    }
    return all(
        _same_json_value(raw.get(name), value)
        for name, value in expected.items()
    )


def _restore_item_serializer_progress(
    raw: Mapping[str, object],
    enchantment: ItemEnchantment,
) -> tuple[Plan2NativeSerializerProgress | None, str]:
    """Restore Android's retained item listener lifecycle without defaults.

    Item StatusEnchants are permanent and have no per-turn activation limit;
    their finite total activation counter is the one lifecycle family which
    Android deliberately retains at zero.  Immutable flags remain derived
    from those exact Master values.
    """

    master_total = -1 if enchantment.max_uses == 0 else enchantment.max_uses
    try:
        return (
            restore_plan2_native_serializer_progress(
                raw,
                master_turn=-1,
                master_total_limit=master_total,
                master_per_turn_limit=-1,
                phase_type_names=_SERIALIZED_PHASE_NAME_BY_ENUM,
                label=f"item-status:{enchantment.id}",
                lifecycle_policy=(
                    Plan2NativeSerializerLifecyclePolicy.ITEM_ACTIVE_OR_EXHAUSTED
                ),
            ),
            "",
        )
    except Plan2NativeSerializerProgressError as error:
        return None, str(error)


def _load_master_effect_raw(
    effect_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Mapping[str, object]:
    """Load the full Master row used to prove an inert unsupported child."""

    _text(effect_id, "effect_id")
    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        row = connection.execute(
            "SELECT raw_json FROM effect WHERE id = ?", (effect_id,)
        ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise KeyError(f"unknown Master effect: {effect_id}")
    value = json.loads(row[0])
    if not isinstance(value, Mapping):
        raise ValueError(f"Master effect is not an object: {effect_id}")
    return value


def _expected_serialized_master_effect(
    effect: MasterCardEffect,
    raw_loader: Callable[[str], Mapping[str, object]],
) -> tuple[dict[str, object] | None, str]:
    """Convert one exact Master row into its LocalSave child payload.

    The first implementation only admitted the scalar effect subset and
    treated every movement/search enum as an unsupported nested shape.  That
    made a finite item which had already reached ``_limitCount == 0`` poison
    the whole item runtime, even though the serializer still retained an
    exact child row.  Encode every field which the LocalSave child actually
    stores, including enum-backed values, while keeping all unknown enum
    tokens fail-closed.
    """

    try:
        master = raw_loader(effect.id)
    except (KeyError, OSError, TypeError, ValueError, sqlite3.Error) as error:
        return None, (
            "item-exhausted-master-effect-unavailable:"
            f"{effect.id}:{type(error).__name__}"
        )
    core = {
        "id": effect.id,
        "effectType": effect.effect_type,
        "effectValue1": effect.value1,
        "effectValue2": effect.value2,
        "effectCount": effect.effect_count,
        "effectTurn": effect.effect_turn,
        "produceExamStatusEnchantId": effect.status_enchant_id,
        "chainProduceExamEffectId": effect.chain_effect_id,
    }
    if any(
        not _same_json_value(master.get(key), value)
        for key, value in core.items()
    ):
        return None, f"item-exhausted-master-effect-drift:{effect.id}"
    if (
        effect.trigger_id
        or effect.hide_icon
        or effect.once
        or effect.status_enchant_rule is not None
    ):
        return None, f"item-exhausted-master-effect-nested:{effect.id}"
    effect_type = _SERIALIZED_EFFECT_ENUM.get(effect.effect_type)
    if effect_type is None:
        return None, (
            "item-exhausted-effect-enum-unsupported:"
            f"{effect.effect_type}:{effect.id}"
        )

    encoded_enums: dict[str, int] = {}
    enum_fields = (
        ("targetExamEffectType", _SERIALIZED_EFFECT_ENUM),
        ("movePositionType", _SERIALIZED_MOVE_POSITION_ENUM),
        ("pickRangeType", _SERIALIZED_PICK_RANGE_ENUM),
        ("pickRangeType2", _SERIALIZED_PICK_RANGE_ENUM),
        ("pickCountType", _SERIALIZED_PICK_COUNT_ENUM),
        ("pickCountType2", _SERIALIZED_PICK_COUNT_ENUM),
    )
    for name, enum_values in enum_fields:
        value = master.get(name)
        if type(value) is not str or value not in enum_values:
            return None, f"item-exhausted-master-effect-enum-shape:{effect.id}:{name}"
        encoded_enums[name] = enum_values[value]

    # A card-status enchantment has a different serializer graph and cannot
    # be represented by this direct child payload.  It is therefore still a
    # shape blocker, but movement/search payloads are no longer rejected just
    # because they are non-neutral.
    if master.get("produceCardStatusEnchantId") != "":
        return None, f"item-exhausted-master-effect-shape:{effect.id}"
    list_fields = (
        "chainProduceExamEffectIds",
        "produceCardGrowEffectIds",
        "effectGroupIds",
    )
    if any(
        type(master.get(name)) is not list
        or any(
            type(value) is not str or not value
            for value in master.get(name, ())
        )
        for name in list_fields
    ):
        return None, f"item-exhausted-master-effect-list-shape:{effect.id}"
    text_fields = (
        "targetProduceCardId",
        "produceCardSearchId",
        "produceCardSearchId2",
        "pickCountReferenceProduceCardSearchId",
        "pickCountReferenceProduceCardSearchId2",
    )
    if any(type(master.get(name)) is not str for name in text_fields):
        return None, f"item-exhausted-master-effect-text-shape:{effect.id}"
    int_fields = (
        "targetUpgradeCount",
        "pickCountMin",
        "pickCountMax",
        "pickCountMin2",
        "pickCountMax2",
    )
    if any(
        isinstance(master.get(name), bool)
        or not isinstance(master.get(name), int)
        for name in int_fields
    ):
        return None, f"item-exhausted-master-effect-counter-shape:{effect.id}"
    if any(master[name] < 0 for name in int_fields):
        return None, f"item-exhausted-master-effect-counter-shape:{effect.id}"
    return (
        {
            "_cardGrowEffectIdList": list(master["produceCardGrowEffectIds"]),
            "_cardSearchId": master["produceCardSearchId"],
            "_cardSearchId2": master["produceCardSearchId2"],
            "_chainEffectId": master["chainProduceExamEffectId"],
            "_chainEffectIdList": list(master["chainProduceExamEffectIds"]),
            "_effectCount": master["effectCount"],
            "_effectGroupIdList": list(master["effectGroupIds"]),
            "_effectTurn": master["effectTurn"],
            "_effectType": effect_type,
            "_effectValue1": master["effectValue1"],
            "_effectValue2": master["effectValue2"],
            "_id": master["id"],
            "_judgeTargetIndex": 0,
            "_movePositionType": encoded_enums["movePositionType"],
            "_pickCount": 0,
            "_pickCountMax": master["pickCountMax"],
            "_pickCountMax2": master["pickCountMax2"],
            "_pickCountMin": master["pickCountMin"],
            "_pickCountMin2": master["pickCountMin2"],
            "_pickCountReferenceProduceCardSearchId": master[
                "pickCountReferenceProduceCardSearchId"
            ],
            "_pickCountReferenceProduceCardSearchId2": master[
                "pickCountReferenceProduceCardSearchId2"
            ],
            "_pickCountType": encoded_enums["pickCountType"],
            "_pickCountType2": encoded_enums["pickCountType2"],
            "_pickRangeType": encoded_enums["pickRangeType"],
            "_pickRangeType2": encoded_enums["pickRangeType2"],
            "_statusEnchantId": master["produceExamStatusEnchantId"],
            "_targetExamEffectType": encoded_enums["targetExamEffectType"],
            "_targetProduceCardId": master["targetProduceCardId"],
            "_targetUpgradeCount": master["targetUpgradeCount"],
        },
        "",
    )


def _prove_exhausted_unsupported_listener(
    source: Plan2NativeItemSource,
    enchantment: ItemEnchantment,
    raw: Mapping[str, object],
    *,
    master_effect_raw_loader: Callable[[str], Mapping[str, object]],
) -> tuple[Plan2NativeProvenExhaustedItemListener | None, str]:
    if enchantment.max_uses < 1:
        return None, f"item-exhausted-listener-unlimited:{enchantment.id}"
    if set(raw) != set(_TRIGGER_STATUS_FIELDS):
        return None, f"item-exhausted-status-fields-drift:{enchantment.id}"
    progress, progress_issue = _restore_item_serializer_progress(raw, enchantment)
    if progress_issue:
        return None, (
            f"item-exhausted-status-progress-invalid:{enchantment.id}:"
            f"{progress_issue}"
        )
    assert progress is not None
    if not progress.is_finite_exhausted:
        return None, (
            f"item-exhausted-listener-not-exhausted:{enchantment.id}:"
            f"remaining={progress.limit_count!r}"
        )
    phase_values: list[int] = []
    for phase in enchantment.trigger.phase_types:
        value = _SERIALIZED_PHASE_ENUM.get(phase)
        if value is None:
            return None, f"item-exhausted-trigger-enum-unsupported:{phase}"
        phase_values.append(value)
    expected_effects: list[dict[str, object]] = []
    for effect in enchantment.effects:
        expected, issue = _expected_serialized_master_effect(
            effect, master_effect_raw_loader
        )
        if issue:
            return None, issue
        assert expected is not None
        expected_effects.append(expected)
    raw_effects = raw.get("_effectList")
    if (
        not isinstance(raw_effects, list)
        or len(raw_effects) != len(expected_effects)
        or any(
            not isinstance(value, Mapping)
            or set(value) != set(_SERIALIZED_EFFECT_FIELDS)
            or not _same_json_value(value, expected)
            for value, expected in zip(
                raw_effects, expected_effects, strict=True
            )
        )
    ):
        return None, f"item-exhausted-effect-mismatch:{enchantment.id}"
    expected_item = {
        "_fireCount": source.fire_count,
        "_id": source.item_id,
        "_itemType": 1,
        "_parentCustomItemIds": [],
        "_reactionCount": source.reaction_count,
    }
    expected_trigger = {
        "_id": enchantment.trigger.id,
        "_phaseTypeList": phase_values,
        "_phaseValueList": list(enchantment.trigger.phase_values),
    }
    expected_identity = {
        **_ITEM_STATUS_NEUTRAL_IDENTITY,
        "_statusEnchantId": enchantment.id,
        "_isItemDirectEnchant": True,
        "_trigger": expected_trigger,
        "_triggerItem": expected_item,
    }
    if any(
        not _same_json_value(raw.get(name), expected)
        for name, expected in expected_identity.items()
    ):
        return None, f"item-exhausted-status-identity-drift:{enchantment.id}"
    uid = raw.get("_uid")
    if (
        isinstance(uid, bool)
        or not isinstance(uid, int)
        or uid < 1
    ):
        return None, f"item-exhausted-status-counter-invalid:{enchantment.id}"
    return (
        Plan2NativeProvenExhaustedItemListener(
            source.item_id,
            enchantment.id,
            enchantment.trigger.id,
            enchantment.max_uses,
            uid,
            progress.is_passing_turn_start,
            progress.turn_count,
            progress,
        ),
        "",
    )


def _compile_item_rules_with_exhausted_proof(
    rules: Sequence[EquippedItemRule],
    sources: Sequence[Plan2NativeItemSource],
    active_trigger_statuses: Sequence[tuple[int, Mapping[str, object]]],
    *,
    master_effect_raw_loader: Callable[[str], Mapping[str, object]],
) -> tuple[
    Plan2NativeItemCompilation,
    Mapping[str, Plan2NativeProvenExhaustedItemListener],
]:
    raw_by_enchantment: dict[str, list[Mapping[str, object]]] = {}
    for _rid, raw in active_trigger_statuses:
        value = raw.get("_statusEnchantId")
        if isinstance(value, str) and value:
            raw_by_enchantment.setdefault(value, []).append(raw)
    retained_rules: list[EquippedItemRule] = []
    retained_sources: list[Plan2NativeItemSource] = []
    proven: dict[str, Plan2NativeProvenExhaustedItemListener] = {}
    proof_blockers: list[str] = []
    for source, rule in zip(sources, rules, strict=True):
        retained: list[ItemEnchantment] = []
        for enchantment in rule.enchantments:
            trigger, trigger_issue = _compile_trigger(
                enchantment,
                source_item_id=rule.id,
            )
            effect_results = tuple(_compile_effect(value) for value in enchantment.effects)
            executable = bool(
                trigger is not None
                and not trigger_issue
                and enchantment.effects
                and all(value is not None and not issue for value, issue in effect_results)
            )
            if executable:
                retained.append(enchantment)
                continue
            matches = raw_by_enchantment.get(enchantment.id, [])
            if len(matches) != 1:
                retained.append(enchantment)
                proof_blockers.append(
                    "item-exhausted-status-not-unique:"
                    f"{enchantment.id}:count={len(matches)}"
                )
                continue
            exhausted, issue = _prove_exhausted_unsupported_listener(
                source,
                enchantment,
                matches[0],
                master_effect_raw_loader=master_effect_raw_loader,
            )
            if issue:
                retained.append(enchantment)
                proof_blockers.append(issue)
                continue
            assert exhausted is not None
            proven[enchantment.id] = exhausted
        if retained or rule.unsupported_rules:
            retained_rules.append(replace(rule, enchantments=tuple(retained)))
            retained_sources.append(source)
    compiled = (
        compile_plan2_native_item_rules(
            tuple(retained_rules), sources=tuple(retained_sources)
        )
        if retained_rules
        else Plan2NativeItemCompilation(Plan2NativeItemRuntime())
    )
    if proof_blockers:
        return (
            Plan2NativeItemCompilation(
                None,
                tuple(dict.fromkeys((*compiled.blockers, *proof_blockers))),
            ),
            {},
        )
    return compiled, proven


def restore_plan2_native_item_runtime(
    raw_item_list: object,
    active_trigger_statuses: Sequence[tuple[int, Mapping[str, object]]],
    *,
    lesson_step_type_value: int | None = None,
    item_rule_loader: Callable[[str], EquippedItemRule] = load_item_rule,
    master_effect_raw_loader: Callable[
        [str], Mapping[str, object]
    ] = _load_master_effect_raw,
) -> Plan2NativeItemCompilation:
    """Restore equipped item uses and status UIDs from settled LocalSave data.

    ``lesson_step_type_value`` selects the native start/install gate for a
    lesson.  A statically inapplicable enchantment may be absent from the save
    without creating a runtime listener.  Unknown, applicable, or actually
    active enchantments are never filtered and therefore retain the existing
    fail-closed compilation/status checks.
    """

    if not isinstance(raw_item_list, list):
        return Plan2NativeItemCompilation(None, ("local-save-item-list-invalid",))
    sources: list[Plan2NativeItemSource] = []
    blockers: list[str] = []
    rules: list[EquippedItemRule] = []
    for index, raw in enumerate(raw_item_list):
        source, issue = _parse_source(raw, index)
        if issue:
            blockers.append(issue)
            continue
        assert source is not None
        sources.append(source)
        try:
            rules.append(item_rule_loader(source.item_id))
        except (KeyError, OSError, TypeError, ValueError) as error:
            blockers.append(
                f"local-save-item-master-unavailable:{source.item_id}:{type(error).__name__}"
            )
    if len({value.item_id for value in sources}) != len(sources):
        blockers.append("local-save-item-id-duplicate")
    if blockers:
        return Plan2NativeItemCompilation(None, tuple(dict.fromkeys(blockers)))
    if not sources:
        if active_trigger_statuses:
            return Plan2NativeItemCompilation(
                None,
                ("local-save-item-trigger-without-source",),
            )
        return Plan2NativeItemCompilation(Plan2NativeItemRuntime())
    # ProduceEffect-only items (for example the Initial badge that increases
    # lesson Present rewards) belong exclusively to the outer produce
    # boundary.  They never install an Exam listener, in either lessons or
    # auditions.  Keep their source identity in ``sources`` while excluding
    # them from inner listener compilation.
    outer_only_ids = {
        rule.id
        for rule in rules
        if not rule.enchantments
        and rule.unsupported_rules
        and all(
            value.startswith("item-effect:ProduceItemEffectType_ProduceEffect:")
            for value in rule.unsupported_rules
        )
    }
    proven_exhausted_by_id: Mapping[
        str, Plan2NativeProvenExhaustedItemListener
    ] = {}
    if lesson_step_type_value is None and outer_only_ids:
        retained = tuple(
            (source, rule)
            for source, rule in zip(sources, rules, strict=True)
            if rule.id not in outer_only_ids
        )
        if retained:
            inner_sources, inner_rules = zip(*retained, strict=True)
            compiled, proven_exhausted_by_id = (
                _compile_item_rules_with_exhausted_proof(
                    tuple(inner_rules),
                    sources=tuple(inner_sources),
                    active_trigger_statuses=active_trigger_statuses,
                    master_effect_raw_loader=master_effect_raw_loader,
                )
            )
        else:
            compiled = Plan2NativeItemCompilation(Plan2NativeItemRuntime())
    elif lesson_step_type_value is not None:
        try:
            _plain_int(
                lesson_step_type_value,
                "lesson_step_type_value",
                minimum=1,
            )
        except (TypeError, ValueError) as error:
            return Plan2NativeItemCompilation(
                None,
                (f"local-save-item-lesson-step-invalid:{error}",),
            )
        if lesson_step_type_value not in LESSON_STEP_VALUES:
            return Plan2NativeItemCompilation(
                None,
                (
                    "local-save-item-lesson-step-invalid:"
                    f"{lesson_step_type_value}",
                ),
            )

        active_enchantment_ids = {
            value
            for _, raw in active_trigger_statuses
            for value in (raw.get("_statusEnchantId"),)
            if isinstance(value, str) and value
        }
        applicable_rules: list[EquippedItemRule] = []
        applicable_sources: list[Plan2NativeItemSource] = []
        static_blockers: list[str] = []
        for source, rule in zip(sources, rules, strict=True):
            retained: list[ItemEnchantment] = []
            for enchantment in rule.enchantments:
                try:
                    installs = plan2_native_lesson_trigger_is_start_valid(
                        enchantment.trigger.lesson_type,
                        lesson_step_type_value,
                    )
                except (TypeError, ValueError):
                    # An unknown enum is not evidence that the native listener
                    # is absent.  Retain it so compilation fails closed.
                    installs = True
                if installs or enchantment.id in active_enchantment_ids:
                    retained.append(enchantment)
            if retained:
                applicable_rules.append(
                    replace(rule, enchantments=tuple(retained))
                )
                applicable_sources.append(source)
            elif (
                not rule.enchantments
                and rule.unsupported_rules
                and all(
                    value.startswith(
                        "item-effect:ProduceItemEffectType_ProduceEffect:"
                    )
                    for value in rule.unsupported_rules
                )
            ):
                # ``ProduceItemEffectType_ProduceEffect`` belongs to the
                # weekly/lesson-result boundary (for example the Initial
                # badge that increases lesson Present reward count).  It does
                # not install an Exam listener and therefore has no inner
                # runtime to restore.  Preserve the equipped source below,
                # but do not require a fabricated enchantment merely because
                # the item is present in ExamSaveData.itemList.
                continue
            else:
                static_blockers.extend(
                    f"item-rule-unsupported:{rule.id}:{value}"
                    for value in rule.unsupported_rules
                )
                if not rule.enchantments:
                    static_blockers.append(f"item-enchantment-missing:{rule.id}")
        if static_blockers:
            return Plan2NativeItemCompilation(
                None,
                tuple(dict.fromkeys(static_blockers)),
            )
        if applicable_rules:
            compiled, proven_exhausted_by_id = (
                _compile_item_rules_with_exhausted_proof(
                    applicable_rules,
                    sources=applicable_sources,
                    active_trigger_statuses=active_trigger_statuses,
                    master_effect_raw_loader=master_effect_raw_loader,
                )
            )
        else:
            compiled = Plan2NativeItemCompilation(Plan2NativeItemRuntime())
    else:
        compiled, proven_exhausted_by_id = (
            _compile_item_rules_with_exhausted_proof(
                rules,
                sources,
                active_trigger_statuses,
                master_effect_raw_loader=master_effect_raw_loader,
            )
        )
    if not compiled.supported:
        return compiled
    assert compiled.runtime is not None
    compiled_runtime = replace(compiled.runtime, sources=tuple(sources))
    by_enchantment = {
        value.enchantment_id: value for value in compiled_runtime.listeners
    }
    source_by_id = {
        value.item_id: value for value in compiled_runtime.sources
    }
    restored: list[Plan2NativeItemListener] = []
    restored_exhausted: list[Plan2NativeProvenExhaustedItemListener] = []
    observed_uids: list[int] = []
    seen: set[str] = set()
    for rid, raw in active_trigger_statuses:
        enchantment_id = raw.get("_statusEnchantId")
        if (
            isinstance(enchantment_id, str)
            and enchantment_id in proven_exhausted_by_id
        ):
            if enchantment_id in seen:
                blockers.append(
                    f"local-save-item-enchantment-duplicate:{enchantment_id}"
                )
                continue
            exhausted = proven_exhausted_by_id[enchantment_id]
            seen.add(enchantment_id)
            restored_exhausted.append(exhausted)
            observed_uids.append(exhausted.status_uid)
            continue
        if (
            not isinstance(enchantment_id, str)
            or enchantment_id not in by_enchantment
        ):
            blockers.append(f"local-save-item-enchantment-unknown:{enchantment_id!r}")
            continue
        listener = by_enchantment[enchantment_id]
        if enchantment_id in seen:
            blockers.append(f"local-save-item-enchantment-duplicate:{enchantment_id}")
            continue
        seen.add(enchantment_id)
        if set(raw) != set(_TRIGGER_STATUS_FIELDS):
            blockers.append(
                f"local-save-item-status-shape-unsupported:{enchantment_id}"
            )
            continue
        trigger = raw.get("_trigger")
        expected_phase = _PHASE_ENUM[listener.trigger.phase]
        # Generic listeners keep their scalar legacy fields neutral.  Their
        # serialized phase arguments live on the exact Master composite
        # trigger, so using ``listener.trigger.interval`` here would turn a
        # valid EndTurnInterval value (for example 2) into a bogus zero and
        # reject the otherwise exact native status shape.
        master_phase_values = (
            listener.trigger.master_trigger.phase_values
            if listener.trigger.master_trigger is not None
            else ()
        )
        expected_phase_values = (
            list(master_phase_values)
            if listener.trigger.master_trigger is not None
            else [listener.trigger.interval]
            if listener.trigger.phase
            in {ITEM_PHASE_END_TURN, ITEM_PHASE_TURN_INTERVAL}
            else [listener.trigger.minimum_status_difference]
            if (
                listener.trigger.phase == ITEM_PHASE_STATUS_CHANGE
                and listener.trigger.minimum_block > 0
            )
            else []
        )
        expected_trigger = {
            "_id": listener.trigger.trigger_id,
            "_phaseTypeList": [expected_phase],
            "_phaseValueList": expected_phase_values,
        }
        source = source_by_id.get(listener.source_item_id)
        if source is None:
            blockers.append(
                f"local-save-item-source-missing:{listener.source_item_id}"
            )
            continue
        expected_identity = {
            **_ITEM_STATUS_NEUTRAL_IDENTITY,
            "_statusEnchantId": enchantment_id,
            "_isItemDirectEnchant": True,
            "_trigger": expected_trigger,
        }
        if any(
            not _same_json_value(raw.get(name), expected)
            for name, expected in expected_identity.items()
        ):
            blockers.append(f"local-save-item-status-shape-unsupported:{enchantment_id}")
            continue
        trigger_item = raw.get("_triggerItem")
        if (
            not isinstance(trigger_item, Mapping)
            or set(trigger_item) != set(_ITEM_FIELDS)
            or not _same_json_value(trigger_item.get("_id"), source.item_id)
            or not _same_json_value(trigger_item.get("_itemType"), 1)
            or not _same_json_value(
                trigger_item.get("_parentCustomItemIds"), []
            )
        ):
            blockers.append(f"local-save-item-status-shape-unsupported:{enchantment_id}")
            continue
        try:
            _plain_int(
                trigger_item.get("_fireCount"),
                f"{enchantment_id}.triggerItem.fireCount",
            )
            _plain_int(
                trigger_item.get("_reactionCount"),
                f"{enchantment_id}.triggerItem.reactionCount",
            )
        except (TypeError, ValueError):
            blockers.append(f"local-save-item-status-shape-unsupported:{enchantment_id}")
            continue
        rule_total = -1 if listener.max_uses == 0 else listener.max_uses
        try:
            progress = restore_plan2_native_serializer_progress(
                raw,
                master_turn=-1,
                master_total_limit=rule_total,
                master_per_turn_limit=-1,
                phase_type_names=_SERIALIZED_PHASE_NAME_BY_ENUM,
                label=f"item-status:{enchantment_id}",
                lifecycle_policy=(
                    Plan2NativeSerializerLifecyclePolicy.ITEM_ACTIVE_OR_EXHAUSTED
                ),
            )
        except Plan2NativeSerializerProgressError as error:
            blockers.append(
                f"local-save-item-status-progress-invalid:{enchantment_id}:{error}"
            )
            continue
        remaining = progress.limit_count
        uid = raw.get("_uid")
        if (
            isinstance(uid, bool)
            or not isinstance(uid, int)
            or uid < 1
        ):
            blockers.append(f"local-save-item-status-counter-invalid:{enchantment_id}")
            continue
        raw_effects = raw.get("_effectList")
        if (
            not isinstance(raw_effects, list)
            or len(raw_effects) != len(listener.effects)
            or any(
                not _raw_item_effect_matches(raw_effect, effect)
                for raw_effect, effect in zip(raw_effects, listener.effects, strict=True)
            )
        ):
            blockers.append(f"local-save-item-effect-mismatch:{enchantment_id}")
            continue
        restored.append(
            replace(
                listener,
                remaining_uses=(0 if listener.max_uses == 0 else remaining),
                status_uid=uid,
                passing_turn_start=progress.is_passing_turn_start,
                turn_count=progress.turn_count,
                serializer_progress=progress,
            )
        )
        observed_uids.append(uid)
    missing = tuple(value for value in by_enchantment if value not in seen)
    blockers.extend(f"local-save-item-enchantment-missing:{value}" for value in missing)
    missing_exhausted = tuple(
        value for value in proven_exhausted_by_id if value not in seen
    )
    blockers.extend(
        f"local-save-item-exhausted-enchantment-missing:{value}"
        for value in missing_exhausted
    )
    if tuple(value.enchantment_id for value in restored) != tuple(by_enchantment):
        blockers.append("local-save-item-enchantment-order-mismatch")
    uids = tuple(observed_uids)
    if len(uids) != len(set(uids)) or uids != tuple(sorted(uids)):
        blockers.append("local-save-item-status-uid-order-invalid")
    if blockers:
        return Plan2NativeItemCompilation(None, tuple(dict.fromkeys(blockers)))
    next_uid = max(uids, default=0) + 1
    return Plan2NativeItemCompilation(
        Plan2NativeItemRuntime(
            tuple(sources),
            tuple(restored),
            next_uid,
            tuple(restored_exhausted),
        )
    )


def restore_plan2_native_item_runtime_partial(
    raw_item_list: object,
    active_trigger_statuses: Sequence[tuple[int, Mapping[str, object]]],
    *,
    lesson_step_type_value: int | None = None,
    item_rule_loader: Callable[[str], EquippedItemRule] = load_item_rule,
    master_effect_raw_loader: Callable[
        [str], Mapping[str, object]
    ] = _load_master_effect_raw,
) -> Plan2NativeItemCompilation:
    """Restore each equipped source independently while retaining blockers.

    One unsupported item listener must not erase an unrelated, fully proven
    listener from the same settled Exam root.  Failed sources remain present
    in the returned source ledger and their exact blockers remain attached;
    no unsupported listener or child effect is synthesized.
    """

    if not isinstance(raw_item_list, list):
        return Plan2NativeItemCompilation(
            None,
            ("local-save-item-list-invalid",),
        )
    if not raw_item_list:
        return restore_plan2_native_item_runtime(
            raw_item_list,
            active_trigger_statuses,
            lesson_step_type_value=lesson_step_type_value,
            item_rule_loader=item_rule_loader,
            master_effect_raw_loader=master_effect_raw_loader,
        )

    sources: list[Plan2NativeItemSource] = []
    raw_by_item_id: dict[str, object] = {}
    blockers: list[str] = []
    for index, raw in enumerate(raw_item_list):
        source, issue = _parse_source(raw, index)
        if issue:
            blockers.append(issue)
            continue
        assert source is not None
        if source.item_id in raw_by_item_id:
            blockers.append("local-save-item-id-duplicate")
            continue
        sources.append(source)
        raw_by_item_id[source.item_id] = raw
    if len(sources) != len(raw_item_list):
        return Plan2NativeItemCompilation(
            None,
            tuple(dict.fromkeys(blockers)),
        )

    statuses_by_item_id: dict[
        str, list[tuple[int, Mapping[str, object]]]
    ] = {source.item_id: [] for source in sources}
    observed_uids: list[int] = []
    attribution_failed = False
    for rid, raw in active_trigger_statuses:
        trigger_item = raw.get("_triggerItem")
        item_id = (
            trigger_item.get("_id")
            if isinstance(trigger_item, Mapping)
            else None
        )
        if not isinstance(item_id, str) or item_id not in statuses_by_item_id:
            uid = raw.get("_uid")
            enchantment_id = raw.get("_statusEnchantId")
            raw_effects = raw.get("_effectList")
            effect_ids = tuple(
                effect.get("_id")
                for effect in raw_effects
                if isinstance(effect, Mapping)
                and isinstance(effect.get("_id"), str)
                and effect.get("_id")
            ) if isinstance(raw_effects, list) else ()
            blockers.append(
                "local-save-item-trigger-without-source:"
                f"rid={rid}:uid={uid!r}:item={item_id!r}:"
                f"enchantment={enchantment_id!r}:effects={effect_ids!r}"
            )
            attribution_failed = True
            continue
        statuses_by_item_id[item_id].append((rid, raw))
        uid = raw.get("_uid")
        if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0:
            observed_uids.append(uid)

    if attribution_failed:
        return Plan2NativeItemCompilation(
            None,
            tuple(dict.fromkeys(blockers)),
        )

    listeners: list[Plan2NativeItemListener] = []
    proven_exhausted: list[Plan2NativeProvenExhaustedItemListener] = []
    for source in sources:
        compilation = restore_plan2_native_item_runtime(
            [raw_by_item_id[source.item_id]],
            tuple(statuses_by_item_id[source.item_id]),
            lesson_step_type_value=lesson_step_type_value,
            item_rule_loader=item_rule_loader,
            master_effect_raw_loader=master_effect_raw_loader,
        )
        blockers.extend(compilation.blockers)
        if compilation.runtime is None:
            continue
        listeners.extend(compilation.runtime.listeners)
        proven_exhausted.extend(compilation.runtime.proven_exhausted)

    if observed_uids and max(observed_uids) >= INT32_MAX:
        blockers.append("local-save-item-next-status-uid-overflow")
        return Plan2NativeItemCompilation(
            None,
            tuple(dict.fromkeys(blockers)),
        )
    next_uid = max(observed_uids, default=0) + 1
    try:
        runtime = Plan2NativeItemRuntime(
            sources=tuple(sources),
            listeners=tuple(sorted(listeners, key=lambda value: value.status_uid)),
            next_status_uid=next_uid,
            proven_exhausted=tuple(
                sorted(
                    proven_exhausted,
                    key=lambda value: value.status_uid,
                )
            ),
        )
    except (TypeError, ValueError) as error:
        blockers.append(
            "local-save-item-partial-runtime-invalid:"
            f"{type(error).__name__}:{error}"
        )
        return Plan2NativeItemCompilation(
            None,
            tuple(dict.fromkeys(blockers)),
        )
    return Plan2NativeItemCompilation(
        runtime,
        tuple(dict.fromkeys(blockers)),
    )


__all__ = [
    "EFFECT_AGGRESSIVE",
    "EFFECT_BLOCK",
    "EFFECT_BLOCK_DEPEND_REVIEW",
    "EFFECT_BLOCK_FIX",
    "EFFECT_STAMINA_REDUCE",
    "EFFECT_LESSON_DEPEND_CARD_PLAY_AGGRESSIVE",
    "EFFECT_LESSON_DEPEND_REVIEW",
    "EFFECT_REVIEW",
    "FIELD_REVIEW_UP",
    "ITEM_PHASE_CARD_PLAY",
    "ITEM_PHASE_CARD_PLAY_AFTER",
    "ITEM_PHASE_END_TURN",
    "ITEM_PHASE_STATUS_CHANGE",
    "ITEM_PHASE_TURN_INTERVAL",
    "ITEM_PHASE_TURN_START",
    "Plan2NativeItemCompilation",
    "Plan2NativeItemDispatch",
    "Plan2NativeItemEffect",
    "Plan2NativeItemEvent",
    "Plan2NativeItemListener",
    "Plan2NativeProvenExhaustedItemListener",
    "Plan2NativeItemRuntime",
    "Plan2NativeItemSource",
    "Plan2NativeItemTrigger",
    "advance_plan2_native_item_turn_start",
    "compile_plan2_native_item_rules",
    "dispatch_plan2_native_item_event",
    "plan2_native_lesson_trigger_is_start_valid",
    "plan2_native_lesson_trigger_is_runtime_valid",
    "restore_plan2_native_item_runtime",
    "restore_plan2_native_item_runtime_partial",
]
