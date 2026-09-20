"""Strict, Master-driven kernel for the selected Plan 3 (Anomaly) slice.

The kernel deliberately implements a small set of *effect and trigger
shapes*, not a list of card names or card IDs.  Numeric values and ordered
references come from the imported local Master database.  A rule outside the
explicit token/shape manifest blocks the whole transition and leaves the
state unchanged.

Turn boundaries are explicit: :func:`apply_plan3_card` consumes a play and
updates card zones, while :func:`end_plan3_turn` advances the round and clears
turn-only enthusiasm.  This mirrors the live controller's need to verify the
settled card result before it commits the following hand.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

import yaml

if TYPE_CHECKING:
    from .enthusiastic_runtime import EnthusiasticReceipt
    from .playable_value_add_runtime import PlayableValueAddReceipt

from .card_search import (
    ProduceCardSearchRule,
    match_plan3_card_move_search,
    validate_plan3_card_move_search,
)
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    AddBlockSettings,
    AddBlockStatus,
    AddingParameterSettings,
    AddingParameterStatus,
    IdolStatusType,
    ParameterApplicationStatus,
    ProduceParameterType,
    StaminaPaymentSettings,
    StaminaPaymentStatus,
    apply_parameter_add,
    calculate_add_block,
    calculate_adding_parameter,
    calculate_effective_stamina_cost,
    ceil_f32_to_i32,
    f32,
    get_ratio_effect_int_value,
    permille_to_f32,
)
from .plan3_start_turn_trigger import (
    TRIGGER_CONDITION_THRESHOLD_MULTIPLE_DOWN,
    TRIGGER_NO_BLOCK,
    TRIGGER_NOT_NO_STANCE,
    TRIGGER_NOT_PRESERVATION_UP,
    TRIGGER_STAMINA_UP_MULTIPLE,
    TRIGGER_TURN_PROGRESS_UP,
)


PLAN_COMMON = "ProducePlanType_Common"
PLAN3 = "ProducePlanType_Plan3"
PLAN3_ARCHETYPE = "ProduceExamEffectType_ExamConcentration"
PLAN3_HELP_ID = "idol-plan3"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
from .application_paths import master_directory
DEFAULT_MASTER_DIR = master_directory()
DEFAULT_EXAM_SETTING_ID = "p_exam_setting-1"

CATEGORY_ACTIVE = "ProduceCardCategory_ActiveSkill"
CATEGORY_MENTAL = "ProduceCardCategory_MentalSkill"
CATEGORY_TROUBLE = "ProduceCardCategory_Trouble"
COST_STAMINA = "ExamCostType_Unknown"
COST_FULL_POWER_POINT = "ExamCostType_ExamFullPowerPoint"
MOVE_GRAVE = "ProduceCardMovePositionType_Grave"
MOVE_LOST = "ProduceCardMovePositionType_Lost"
MOVE_HOLD = "ProduceCardMovePositionType_Hold"
MOVE_HAND = "ProduceCardMovePositionType_Hand"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"

EFFECT_PRESERVATION = "ProduceExamEffectType_ExamPreservation"
EFFECT_CONCENTRATION = "ProduceExamEffectType_ExamConcentration"
EFFECT_LESSON = "ProduceExamEffectType_ExamLesson"
EFFECT_BLOCK = "ProduceExamEffectType_ExamBlock"
EFFECT_BLOCK_RESTRICTION = "ProduceExamEffectType_ExamBlockRestriction"
EFFECT_ENTHUSIASTIC_ADDITIVE = (
    "ProduceExamEffectType_ExamEnthusiasticAdditive"
)
EFFECT_ENTHUSIASTIC_MULTIPLE = (
    "ProduceExamEffectType_ExamEnthusiasticMultiple"
)
EFFECT_FULL_POWER_POINT = "ProduceExamEffectType_ExamFullPowerPoint"
EFFECT_FULL_POWER_POINT_ADDITIVE = (
    "ProduceExamEffectType_ExamFullPowerPointAdditive"
)
EFFECT_FULL_POWER_POINT_REDUCE = (
    "ProduceExamEffectType_ExamFullPowerPointReduce"
)
EFFECT_LESSON_FULL_POWER_POINT = (
    "ProduceExamEffectType_ExamLessonFullPowerPoint"
)
EFFECT_BLOCK_FIX = "ProduceExamEffectType_ExamBlockFix"
EFFECT_STAMINA_RECOVER_FIX = "ProduceExamEffectType_ExamStaminaRecoverFix"
EFFECT_STAMINA_RECOVER_MULTIPLE = (
    "ProduceExamEffectType_ExamStaminaRecoverMultiple"
)
EFFECT_STAMINA_REDUCE_FIX = "ProduceExamEffectType_ExamStaminaReduceFix"
EFFECT_STAMINA_REDUCE = "ProduceExamEffectType_ExamStaminaReduce"
EFFECT_STAMINA_CONSUMPTION_DOWN = (
    "ProduceExamEffectType_ExamStaminaConsumptionDown"
)
EFFECT_STAMINA_CONSUMPTION_DOWN_FIX = (
    "ProduceExamEffectType_ExamStaminaConsumptionDownFix"
)
EFFECT_PLAYABLE_VALUE_ADD = "ProduceExamEffectType_ExamPlayableValueAdd"
EFFECT_STATUS_ENCHANT = "ProduceExamEffectType_ExamStatusEnchant"
EFFECT_CARD_MOVE = "ProduceExamEffectType_ExamCardMove"
EFFECT_ADD_GROW = "ProduceExamEffectType_ExamAddGrowEffect"
EFFECT_CARD_DRAW = "ProduceExamEffectType_ExamCardDraw"
EFFECT_SEARCH_PLAY_CARD_STAMINA_CHANGE = (
    "ProduceExamEffectType_ExamSearchPlayCardStaminaConsumptionChange"
)
EFFECT_HAND_GRAVE_COUNT_CARD_DRAW = (
    "ProduceExamEffectType_ExamHandGraveCountCardDraw"
)
EFFECT_CARD_UPGRADE = "ProduceExamEffectType_ExamCardUpgrade"
EFFECT_CARD_CREATE_ID = "ProduceExamEffectType_ExamCardCreateId"
EFFECT_CARD_CREATE_SEARCH = "ProduceExamEffectType_ExamCardCreateSearch"
EFFECT_ANTI_DEBUFF = "ProduceExamEffectType_ExamAntiDebuff"
EFFECT_TIMER = "ProduceExamEffectType_ExamEffectTimer"
EFFECT_STANCE_LOCK = "ProduceExamEffectType_StanceLock"
EFFECT_OVER_PRESERVATION = (
    "ProduceExamEffectType_ExamOverPreservation"
)
EFFECT_MULTIPLE_ENTHUSIASTIC_LESSON = (
    "ProduceExamEffectType_ExamMultipleEnthusiasticLesson"
)
EFFECT_STATUS_ENCHANT_ENCORE = (
    "ProduceExamEffectType_ExamStatusEnchantEncore"
)
EFFECT_FORCE_PLAY_CARD_SEARCH = (
    "ProduceExamEffectType_ExamForcePlayCardSearch"
)
EFFECT_CARD_SEARCH_PLAY_COUNT_BUFF = (
    "ProduceExamEffectType_ExamCardSearchEffectPlayCountBuff"
)
EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST = (
    "ProduceExamEffectType_ExamForcePlayCardSearchWithCost"
)
EFFECT_EXTRA_TURN = "ProduceExamEffectType_ExamExtraTurn"
EFFECT_LESSON_VALUE_MULTIPLE = (
    "ProduceExamEffectType_ExamLessonValueMultiple"
)
EXACT_ENCORE_CARD_ID = "p_card-03-ido-3_197"
EXACT_ENCORE_EFFECT_ID = (
    "e_effect-exam_status_enchant_encore-0001-03-inf-"
    "enchant-p_card-03-ido-3_197-enc01"
)
EXACT_ENCORE_STATUS_ID = "enchant-p_card-03-ido-3_197-enc01"
EXACT_ENCORE_TRIGGER_ID = "e_trigger-exam_end_turn-remaining_turn-3"
EXACT_ENCORE_FORCE_PLAY_EFFECT_ID = (
    "e_effect-exam_force_play_card_search-p_card_search-target_is_self-"
    "exam_status_enchant_encore-all-1_1"
)

_SEARCH_PLAY_CARD_STAMINA_CHANGE_IDS = frozenset(
    {
        "e_effect-exam_search_play_card_stamina_consumption_change-01-inf-"
        "p_card_search-deck_all-all-0_0",
        "e_effect-exam_search_play_card_stamina_consumption_change-02-inf-"
        "p_card_search-deck_all-all-0_0",
        "e_effect-exam_search_play_card_stamina_consumption_change-05-inf-"
        "p_card_search-deck_all-p_card-03-ido-3_135-all-0_0",
    }
)
_HAND_GRAVE_COUNT_CARD_DRAW_ID = (
    "e_effect-exam_hand_grave_count_card_draw"
)

SEARCH_PLAYING_SELF = "p_card_search-playing-is_self"
PICK_RANGE_ALL = "ProducePickRangeType_All"
PICK_RANGE_SELECT = "ProducePickRangeType_Select"
PICK_RANGE_RANDOM = "ProducePickRangeType_Random"
PICK_RANGE_UNKNOWN = "ProducePickRangeType_Unknown"
PICK_COUNT_UNKNOWN = "ProducePickCountType_Unknown"
EFFECT_UNKNOWN = "ProduceExamEffectType_Unknown"

PHASE_NONE = "ProduceExamPhaseType_None"
PHASE_START_TURN = "ProduceExamPhaseType_ExamStartTurn"
PHASE_START_PLAY = "ProduceExamPhaseType_StartPlay"
PHASE_CARD_PLAY = "ProduceExamPhaseType_ExamCardPlay"
PHASE_CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"
PHASE_PLAY_COUNT_INTERVAL = "ProduceExamPhaseType_ExamPlayCountInterval"
PHASE_STATUS_CHANGE = "ProduceExamPhaseType_ExamStatusChange"
PHASE_END_TURN = "ProduceExamPhaseType_ExamEndTurn"
PHASE_TURN_TIMER = "ProduceExamPhaseType_ExamTurnTimer"
PHASE_STANCE_CHANGE_CONCENTRATION = (
    "ProduceExamPhaseType_ExamStanceChangeConcentration"
)
PHASE_STANCE_CHANGE_FROM_FULL_POWER = (
    "ProduceExamPhaseType_ExamStanceChangeFromFullPower"
)
PHASE_STANCE_CHANGE_FULL_POWER = (
    "ProduceExamPhaseType_ExamStanceChangeFullPower"
)
FIELD_STANCE_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_StanceChangeCountUp"
)
FIELD_PRESERVATION_UP = "ProduceExamFieldStatusType_PreservationUp"
FIELD_NO_BLOCK = "ProduceExamFieldStatusType_NoBlock"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
LESSON_GENERAL = "ProduceStepLessonType_Lesson"
LESSON_NORMAL = "ProduceStepLessonType_LessonNormal"
LESSON_SP = "ProduceStepLessonType_LessonSp"
LESSON_HARD = "ProduceStepLessonType_LessonHard"
LESSON_VOCAL = "ProduceStepLessonType_LessonVocal"
LESSON_DANCE = "ProduceStepLessonType_LessonDance"
LESSON_VISUAL = "ProduceStepLessonType_LessonVisual"
SEARCH_MENTAL_SKILL_TARGET = "p_card_search-mental_skill-target"
SEARCH_PLAYING_FULL_POWER_EFFECT = (
    "p_card_search-playing-effect_group-visible-exam_full_power-000"
)
PLAN3_MID1_DANCE_GIMMICK_ID = (
    "p_exam_gimmick-produce-002-exam_concentration_01-lesson_hard-before_mid"
)
PLAN3_AFTER_MID_GIMMICK_ID = (
    "p_exam_gimmick-produce-002-exam_concentration_01-lesson_hard-after_mid"
)
FIELD_UNKNOWN = "ProduceExamFieldStatusType_Unknown"
FIELD_CONCENTRATION_UP = "ProduceExamFieldStatusType_ConcentrationUp"
FIELD_CONCENTRATION_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_ConcentrationChangeCountUp"
)
FIELD_PRESERVATION_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_PreservationChangeCountUp"
)
FIELD_CARD_SEARCH_COUNT_UP = (
    "ProduceExamFieldStatusType_CardSearchCountUp"
)
FIELD_FULL_POWER_UP = "ProduceExamFieldStatusType_FullPowerUp"
FIELD_FULL_POWER_POINT_UP = "ProduceExamFieldStatusType_FullPowerPointUp"
FIELD_STAMINA_UP_MULTIPLE = (
    "ProduceExamFieldStatusType_StaminaUpMultiple"
)
FIELD_STAMINA_LESS_MULTIPLE = (
    "ProduceExamFieldStatusType_StaminaLessMultiple"
)
FIELD_FULL_POWER_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_FullPowerChangeCountUp"
)
FIELD_FULL_POWER_POINT_GET_SUM_UP = (
    "ProduceExamFieldStatusType_FullPowerPointGetSumUp"
)
FIELD_CONDITION_THRESHOLD_MULTIPLE_DOWN = (
    "ProduceExamFieldStatusType_ConditionThresholdMultipleDown"
)
FIELD_TURN_PROGRESS_UP = "ProduceExamFieldStatusType_TurnProgressUp"
FIELD_REMAINING_TURN = "ProduceExamFieldStatusType_RemainingTurn"
FIELD_NO_STANCE = "ProduceExamFieldStatusType_NoStance"
CHECK_UNKNOWN = "ProduceExamTriggerCheckType_Unknown"
CHECK_NOT = "ProduceExamTriggerCheckType_Not"

SEARCH_ACTIVE_SKILL_TARGET = "p_card_search-active_skill-target"
SEARCH_MENTAL_SKILL_PLAYING = "p_card_search-mental_skill-playing"
SEARCH_PLAYING_CONCENTRATION_EFFECT = (
    "p_card_search-playing-effect_group-visible-exam_concentration-000"
)
SEARCH_TARGET_CONCENTRATION_EFFECT = (
    "p_card_search-target-effect_group-visible-exam_concentration-000"
)
TRIGGER_CARD_PLAY_AFTER_TARGET_CONCENTRATION = (
    "e_trigger-exam_card_play_after-p_card_search-target-"
    "effect_group-visible-exam_concentration-000-0_1"
)
TRIGGER_NONE_NOT_PRESERVATION_UP = "e_trigger-none-not-preservation_up"
TRIGGER_START_PLAY_PRESERVATION_UP = (
    "e_trigger-start_play-preservation_up"
)

_EXACT_START_TURN_CARD_PLAY_GATES = frozenset(
    {
        TRIGGER_CONDITION_THRESHOLD_MULTIPLE_DOWN,
        TRIGGER_NO_BLOCK,
        TRIGGER_STAMINA_UP_MULTIPLE,
        TRIGGER_TURN_PROGRESS_UP,
    }
)

_FULL_POWER_POINT_ADDITIVE_TURNS_BY_ID = {
    "e_effect-exam_full_power_point_additive-0500-03": 3,
    "e_effect-exam_full_power_point_additive-0500-04": 4,
}
_FINITE_ENTHUSIASM_ADDITIVE_IDS = frozenset(
    {
        "e_effect-exam_enthusiastic_additive-0002-02",
        "e_effect-exam_enthusiastic_additive-0005-02",
        "e_effect-exam_enthusiastic_additive-0008-02",
    }
)
EFFECT_GROUP_CONCENTRATION = "effect_group-visible-exam_concentration-000"
SEARCH_DECK_ALL = "p_card_search-deck_all"
SEARCH_HAND = "p_card_search-hand"
SEARCH_ACTIVE_SKILL_DECK_ALL = "p_card_search-active_skill-deck_all"
SEARCH_TROUBLE_NOT_LOST = "p_card_search-trouble-not_lost"

ITEM_EFFECT_STATUS_ENCHANT = "ProduceItemEffectType_ExamStatusEnchant"

STANCE_NEUTRAL = "neutral"
STANCE_CONCENTRATION = "concentration"
STANCE_PRESERVATION = "preservation"
STANCE_FULL_POWER = "full_power"

SUPPORTED_PLAN_TYPES = frozenset({PLAN_COMMON, PLAN3})
SUPPORTED_CARD_CATEGORIES = frozenset(
    {CATEGORY_ACTIVE, CATEGORY_MENTAL, CATEGORY_TROUBLE}
)
SUPPORTED_COST_TYPES = frozenset({COST_STAMINA, COST_FULL_POWER_POINT})
SUPPORTED_MOVE_POSITION_TYPES = frozenset({MOVE_GRAVE, MOVE_LOST})
SUPPORTED_EFFECT_TYPES = frozenset(
    {
        EFFECT_PRESERVATION,
        EFFECT_CONCENTRATION,
        EFFECT_LESSON,
        EFFECT_BLOCK,
        EFFECT_BLOCK_RESTRICTION,
        EFFECT_ENTHUSIASTIC_ADDITIVE,
        EFFECT_ENTHUSIASTIC_MULTIPLE,
        EFFECT_FULL_POWER_POINT,
        EFFECT_FULL_POWER_POINT_ADDITIVE,
        EFFECT_FULL_POWER_POINT_REDUCE,
        EFFECT_LESSON_FULL_POWER_POINT,
        EFFECT_BLOCK_FIX,
        EFFECT_STAMINA_RECOVER_FIX,
        EFFECT_STAMINA_RECOVER_MULTIPLE,
        EFFECT_STAMINA_REDUCE_FIX,
        EFFECT_STAMINA_REDUCE,
        EFFECT_STAMINA_CONSUMPTION_DOWN,
        EFFECT_STAMINA_CONSUMPTION_DOWN_FIX,
        EFFECT_PLAYABLE_VALUE_ADD,
        EFFECT_STATUS_ENCHANT,
        EFFECT_CARD_MOVE,
        EFFECT_ADD_GROW,
        EFFECT_CARD_DRAW,
        EFFECT_SEARCH_PLAY_CARD_STAMINA_CHANGE,
        EFFECT_HAND_GRAVE_COUNT_CARD_DRAW,
        EFFECT_CARD_UPGRADE,
        EFFECT_CARD_CREATE_ID,
        EFFECT_CARD_CREATE_SEARCH,
        EFFECT_ANTI_DEBUFF,
        EFFECT_TIMER,
        EFFECT_STANCE_LOCK,
        EFFECT_OVER_PRESERVATION,
        EFFECT_MULTIPLE_ENTHUSIASTIC_LESSON,
        EFFECT_STATUS_ENCHANT_ENCORE,
        EFFECT_FORCE_PLAY_CARD_SEARCH,
        EFFECT_CARD_SEARCH_PLAY_COUNT_BUFF,
        EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST,
        EFFECT_EXTRA_TURN,
        EFFECT_LESSON_VALUE_MULTIPLE,
    }
)
SUPPORTED_TRIGGER_PHASE_TYPES = frozenset(
    {
        PHASE_NONE,
        PHASE_START_TURN,
        PHASE_START_PLAY,
        PHASE_CARD_PLAY,
        PHASE_CARD_PLAY_AFTER,
        PHASE_PLAY_COUNT_INTERVAL,
        PHASE_STATUS_CHANGE,
        PHASE_END_TURN,
        PHASE_TURN_TIMER,
        PHASE_STANCE_CHANGE_CONCENTRATION,
        PHASE_STANCE_CHANGE_FROM_FULL_POWER,
        PHASE_STANCE_CHANGE_FULL_POWER,
    }
)
SUPPORTED_TRIGGER_FIELD_TYPES = frozenset(
    {
        FIELD_STANCE_CHANGE_COUNT_UP,
        FIELD_PRESERVATION_UP,
        FIELD_NO_BLOCK,
        FIELD_CONCENTRATION_UP,
        FIELD_FULL_POWER_UP,
        FIELD_FULL_POWER_POINT_UP,
        FIELD_NO_STANCE,
        FIELD_CARD_SEARCH_COUNT_UP,
        FIELD_STAMINA_UP_MULTIPLE,
        FIELD_STAMINA_LESS_MULTIPLE,
        FIELD_CONCENTRATION_CHANGE_COUNT_UP,
        FIELD_PRESERVATION_CHANGE_COUNT_UP,
        FIELD_FULL_POWER_CHANGE_COUNT_UP,
        FIELD_FULL_POWER_POINT_GET_SUM_UP,
        FIELD_CONDITION_THRESHOLD_MULTIPLE_DOWN,
        FIELD_TURN_PROGRESS_UP,
        FIELD_REMAINING_TURN,
    }
)

# Android v3.2.3 ``ProduceStepLessonType`` values.  The numeric helpers below
# intentionally model the whole native 0..16 x 1..9 domain, while triggers
# retain their exact Master enum strings for serialization and auditability.
LESSON_TYPE_VALUES = {
    LESSON_UNKNOWN: 0,
    LESSON_GENERAL: 1,
    LESSON_NORMAL: 2,
    LESSON_SP: 3,
    LESSON_HARD: 4,
    LESSON_VOCAL: 5,
    LESSON_DANCE: 6,
    LESSON_VISUAL: 7,
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


def _validate_lesson_gate_values(
    lesson_type_value: int, step_type_value: int
) -> None:
    if (
        not isinstance(lesson_type_value, int)
        or isinstance(lesson_type_value, bool)
        or lesson_type_value not in range(17)
    ):
        raise ValueError("lesson_type_value must be an integer from 0 through 16")
    if (
        not isinstance(step_type_value, int)
        or isinstance(step_type_value, bool)
        or step_type_value not in {*range(1, 10), *range(29, 35)}
    ):
        raise ValueError("step_type_value must be an ordinary or Legend lesson step")


def android_v323_lesson_is_valid(
    lesson_type_value: int, step_type_value: int
) -> bool:
    """Exact Android v3.2.3 ``ExamExtensions.IsValid`` lesson gate."""

    _validate_lesson_gate_values(lesson_type_value, step_type_value)
    # APK IsValid masks include Legend 29..34 in the same axis/Normal/SP
    # predicate cells. Preserve the caller's actual step everywhere else.
    from .runtime_lesson_context import lesson_step_rule
    step_type_value = lesson_step_rule(step_type_value).predicate_equivalent_step
    if lesson_type_value in (0, 1):
        return True
    step_kind = (step_type_value - 1) % 3
    if lesson_type_value in (2, 3, 4):
        return step_kind == lesson_type_value - 2
    if lesson_type_value in (5, 6, 7):
        return (step_type_value - 1) // 3 == lesson_type_value - 5
    return step_type_value == lesson_type_value - 7


def android_v323_lesson_is_start_valid(
    lesson_type_value: int, step_type_value: int
) -> bool:
    """Exact Android v3.2.3 ``ExamExtensions.IsStartValid`` install gate."""

    if android_v323_lesson_is_valid(lesson_type_value, step_type_value):
        return True
    if step_type_value not in (3, 6, 9):
        return False
    # Hard steps widen installation except for Normal and SP variants.
    return lesson_type_value not in {2, 3, 8, 9, 11, 12, 14, 15}


def android_v323_lesson_runtime_field_valid(
    lesson_type_value: int,
    step_type_value: int,
    *,
    is_clear: bool,
) -> bool:
    """Native runtime lesson gate used by IsEffectTriggerFieldValid."""

    if not isinstance(is_clear, bool):
        raise ValueError("is_clear must be a boolean")
    _validate_lesson_gate_values(lesson_type_value, step_type_value)
    if is_clear and step_type_value in (3, 6, 9) and lesson_type_value in (5, 6, 7):
        return True
    return android_v323_lesson_is_valid(lesson_type_value, step_type_value)


def android_v323_lesson_is_battle_turn_valid(
    lesson_type_value: int, current_parameter_type: int
) -> bool:
    """Exact battle StartTurn gate for a lesson-typed status listener."""

    if (
        not isinstance(lesson_type_value, int)
        or isinstance(lesson_type_value, bool)
        or lesson_type_value not in range(17)
    ):
        raise ValueError("lesson_type_value must be an integer from 0 through 16")
    if (
        not isinstance(current_parameter_type, int)
        or isinstance(current_parameter_type, bool)
        or current_parameter_type not in range(4)
    ):
        raise ValueError(
            "current_parameter_type must be Unknown/Vocal/Dance/Visual"
        )
    expected = {
        LESSON_TYPE_VALUES[LESSON_VOCAL]: int(ProduceParameterType.VOCAL),
        LESSON_TYPE_VALUES[LESSON_DANCE]: int(ProduceParameterType.DANCE),
        LESSON_TYPE_VALUES[LESSON_VISUAL]: int(ProduceParameterType.VISUAL),
    }.get(lesson_type_value)
    return expected is not None and current_parameter_type == expected


SUPPORTED_ITEM_EFFECT_TYPES = frozenset({ITEM_EFFECT_STATUS_ENCHANT})
SUPPORTED_STANCES = frozenset(
    {
        STANCE_NEUTRAL,
        STANCE_CONCENTRATION,
        STANCE_PRESERVATION,
        STANCE_FULL_POWER,
    }
)

@dataclass(frozen=True, slots=True)
class Plan3ExamSettings:
    """The Plan 3 stance constants resolved from one local ExamSetting row."""

    id: str
    full_power_playable_value_add: int
    hold_limit: int
    hand_limit: int
    turn_start_distribute: int
    turn_end_stamina_recovery: int
    concentration_lesson_permille: tuple[int, int]
    preservation_lesson_permille: tuple[int, int]
    over_preservation_lesson_permille: int
    full_power_lesson_permille: int
    concentration_stamina_permille: tuple[int, int]
    preservation_stamina_permille: tuple[int, int]
    over_preservation_stamina_permille: int
    stamina_consumption_down_permille: int
    stamina_consumption_down_add_permille: int
    concentration_stamina_penetration: tuple[int, int]
    preservation_release_plays: tuple[int, int]
    preservation_release_block: tuple[int, int]
    preservation_release_enthusiasm: tuple[int, int]
    over_preservation_release_plays: int
    over_preservation_release_block: int
    over_preservation_release_enthusiasm: int


@dataclass(frozen=True, slots=True)
class Plan3InitialDeck:
    id: str
    produce_id: str
    exam_effect_type: str
    cards: tuple[Plan3CardRef, ...]


def _json_list(raw: str, label: str) -> list[Any]:
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array")
    return value


def _str_tuple(raw: str, label: str) -> tuple[str, ...]:
    value = _json_list(raw, label)
    if not all(isinstance(entry, str) and entry for entry in value):
        raise ValueError(f"{label} entries must be non-empty strings")
    return tuple(value)


def _int_tuple(raw: str, label: str) -> tuple[int, ...]:
    value = _json_list(raw, label)
    if not all(isinstance(entry, int) and not isinstance(entry, bool) for entry in value):
        raise ValueError(f"{label} entries must be integers")
    return tuple(value)


def _unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class Plan3CardRef:
    card_id: str
    upgrade: int = 0

    def __post_init__(self) -> None:
        if not self.card_id:
            raise ValueError("card_id must not be empty")
        if self.upgrade < 0:
            raise ValueError("upgrade must be non-negative")


@lru_cache(maxsize=8)
def load_plan3_help_detail_url(
    *, master_dir: Path = DEFAULT_MASTER_DIR
) -> str:
    """Return the official Plan 3 help URL referenced by local Master."""

    path = master_dir / "HelpContent.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"local Master file not found: {path}")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    matches = [
        row
        for row in payload
        if isinstance(row, dict) and row.get("id") == PLAN3_HELP_ID
    ] if isinstance(payload, list) else []
    if len(matches) != 1:
        raise KeyError(f"HelpContent must resolve exactly once: {PLAN3_HELP_ID}")
    detail_url = matches[0].get("detailUrl")
    if not isinstance(detail_url, str) or not detail_url.startswith("https://"):
        raise ValueError(f"invalid HelpContent detailUrl: {PLAN3_HELP_ID}")
    return detail_url


@lru_cache(maxsize=8)
def load_plan3_exam_settings(
    setting_id: str = DEFAULT_EXAM_SETTING_ID,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan3ExamSettings:
    """Load stance constants that are not part of the imported SQLite schema."""

    path = master_dir / "ExamSetting.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"local Master file not found: {path}")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list):
        raise ValueError("ExamSetting.yaml must contain an array")
    matches = [
        row
        for row in payload
        if isinstance(row, dict) and row.get("id") == setting_id
    ]
    if len(matches) != 1:
        raise KeyError(f"ExamSetting must resolve exactly once: {setting_id}")
    row = matches[0]

    def number(name: str) -> int:
        value = row.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"invalid ExamSetting field: {setting_id}:{name}")
        return value

    return Plan3ExamSettings(
        id=setting_id,
        full_power_playable_value_add=number("fullPowerPlayableValueAdd"),
        hold_limit=number("holdLimit"),
        hand_limit=number("handLimit"),
        turn_start_distribute=number("turnStartDistribute"),
        turn_end_stamina_recovery=number("examTurnEndRecoveryStamina"),
        concentration_lesson_permille=(
            number("examConcentrationLessonValueMultiplePermil1"),
            number("examConcentrationLessonValueMultiplePermil2"),
        ),
        preservation_lesson_permille=(
            number("examPreservationLessonValueMultiplePermil1"),
            number("examPreservationLessonValueMultiplePermil2"),
        ),
        over_preservation_lesson_permille=number(
            "examOverPreservationLessonValueMultiplePermil"
        ),
        full_power_lesson_permille=number(
            "examFullPowerLessonValueMultiplePermil"
        ),
        concentration_stamina_permille=(
            number("examConcentrationStaminaMultiplePermil1"),
            number("examConcentrationStaminaMultiplePermil2"),
        ),
        preservation_stamina_permille=(
            number("examPreservationStaminaMultiplePermil1"),
            number("examPreservationStaminaMultiplePermil2"),
        ),
        over_preservation_stamina_permille=number(
            "examOverPreservationStaminaMultiplePermil"
        ),
        stamina_consumption_down_permille=number(
            "examStaminaConsumptionDownPermil"
        ),
        stamina_consumption_down_add_permille=number(
            "examStaminaConsumptionDownAddPermil"
        ),
        concentration_stamina_penetration=(
            number("examConcentrationStaminaPenetrateReduce1"),
            number("examConcentrationStaminaPenetrateReduce2"),
        ),
        preservation_release_plays=(
            number("preservationReleasePlayableValueAdd1"),
            number("preservationReleasePlayableValueAdd2"),
        ),
        preservation_release_block=(
            number("preservationReleaseBlockAdd1"),
            number("preservationReleaseBlockAdd2"),
        ),
        preservation_release_enthusiasm=(
            number("preservationReleaseEnthusiastic1"),
            number("preservationReleaseEnthusiastic2"),
        ),
        over_preservation_release_plays=number(
            "overPreservationReleasePlayableValueAdd"
        ),
        over_preservation_release_block=number(
            "overPreservationReleaseBlockAdd"
        ),
        over_preservation_release_enthusiasm=number(
            "overPreservationReleaseEnthusiastic"
        ),
    )


def load_plan3_initial_deck(
    *,
    produce_id: str = "produce-002",
    exam_effect_type: str = PLAN3_ARCHETYPE,
    database: Path = DEFAULT_DATABASE,
) -> Plan3InitialDeck:
    """Resolve an exact mode/archetype deck from imported Master mappings."""

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT m.exam_initial_deck_id, d.card_ids_json,
                   d.upgrade_counts_json
              FROM initial_deck_map AS m
              JOIN initial_deck AS d ON d.id = m.exam_initial_deck_id
             WHERE m.produce_id = ? AND m.exam_effect_type = ?
            """,
            (produce_id, exam_effect_type),
        ).fetchone()
    if row is None:
        raise KeyError(f"initial deck not found: {produce_id}:{exam_effect_type}")
    card_ids = _str_tuple(row["card_ids_json"], "initial-deck:cards")
    upgrades = _int_tuple(row["upgrade_counts_json"], "initial-deck:upgrades")
    if upgrades and len(upgrades) != len(card_ids):
        raise ValueError("initial deck card and upgrade counts differ")
    if not upgrades:
        upgrades = (0,) * len(card_ids)
    return Plan3InitialDeck(
        id=str(row["exam_initial_deck_id"]),
        produce_id=produce_id,
        exam_effect_type=exam_effect_type,
        cards=tuple(
            Plan3CardRef(card_id, upgrade)
            for card_id, upgrade in zip(card_ids, upgrades, strict=True)
        ),
    )


@dataclass(frozen=True, slots=True)
class Plan3Trigger:
    id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...] = ()
    field_check_types: tuple[str, ...] = ()
    field_types: tuple[str, ...] = ()
    field_values: tuple[int, ...] = ()
    field_card_search_ids: tuple[str, ...] = ()
    produce_card_search_id: str = ""
    upper_search_count: int = 0
    lower_search_count: int = 0
    card_move_position_type: str = MOVE_UNKNOWN
    effect_types: tuple[str, ...] = ()
    lesson_type: str = LESSON_UNKNOWN


@dataclass(frozen=True, slots=True)
class Plan3CardMoveRule:
    """Native target and selection shape for one ``ExamCardMove`` effect."""

    target_card_id: str = ""
    target_upgrade: int = 0
    target_effect_type: str = EFFECT_UNKNOWN
    search_id: str = ""
    destination: str = MOVE_UNKNOWN
    pick_range_type: str = PICK_RANGE_UNKNOWN
    pick_reference_search_id: str = ""
    pick_count_type: str = PICK_COUNT_UNKNOWN
    pick_count_min: int = 0
    pick_count_max: int = 0
    second_search_id: str = ""
    second_pick_range_type: str = PICK_RANGE_UNKNOWN
    second_pick_reference_search_id: str = ""
    second_pick_count_type: str = PICK_COUNT_UNKNOWN
    second_pick_count_min: int = 0
    second_pick_count_max: int = 0
    chain_effect_ids: tuple[str, ...] = ()
    card_status_enchant_id: str = ""
    card_grow_effect_ids: tuple[str, ...] = ()
    effect_group_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan3CardMoveCandidate:
    """One scalar projection of a GUID-native CardMove search result."""

    card_ref: Plan3CardRef
    source_zone: str


_CARD_MOVE_DESTINATIONS = frozenset(
    {MOVE_HAND, MOVE_GRAVE, MOVE_LOST, MOVE_HOLD}
)


def _is_general_card_move_rule(rule: Plan3CardMoveRule) -> bool:
    """Accept the neutral Android collection-move grammar, fail closed."""

    common = (
        not rule.target_card_id
        and rule.target_upgrade == 0
        and rule.target_effect_type == EFFECT_UNKNOWN
        and bool(rule.search_id)
        and rule.destination in _CARD_MOVE_DESTINATIONS
        and not rule.pick_reference_search_id
        and rule.pick_count_type == PICK_COUNT_UNKNOWN
        and not rule.second_search_id
        and rule.second_pick_range_type == PICK_RANGE_UNKNOWN
        and not rule.second_pick_reference_search_id
        and rule.second_pick_count_type == PICK_COUNT_UNKNOWN
        and rule.second_pick_count_min == 0
        and rule.second_pick_count_max == 0
        and not rule.chain_effect_ids
        and not rule.card_status_enchant_id
        and not rule.card_grow_effect_ids
        and not rule.effect_group_ids
    )
    if not common:
        return False
    if rule.pick_range_type == PICK_RANGE_ALL:
        return rule.pick_count_min == 0 and rule.pick_count_max == 0
    if rule.pick_range_type in {PICK_RANGE_SELECT, PICK_RANGE_RANDOM}:
        return 0 <= rule.pick_count_min <= rule.pick_count_max
    return False


def _is_playing_self_hold_rule(rule: Plan3CardMoveRule) -> bool:
    return rule == Plan3CardMoveRule(
        search_id=SEARCH_PLAYING_SELF,
        destination=MOVE_HOLD,
        pick_range_type=PICK_RANGE_ALL,
    )


def _is_hand_select_hold_rule(rule: Plan3CardMoveRule) -> bool:
    """Exact Android v3.2.3 Hand -> Hold Select-1/Select-2 family."""

    return (
        not rule.target_card_id
        and rule.target_upgrade == 0
        and rule.target_effect_type == EFFECT_UNKNOWN
        and rule.search_id == SEARCH_HAND
        and rule.destination == MOVE_HOLD
        and rule.pick_range_type == PICK_RANGE_SELECT
        and not rule.pick_reference_search_id
        and rule.pick_count_type == PICK_COUNT_UNKNOWN
        and rule.pick_count_min == rule.pick_count_max
        and rule.pick_count_min in (1, 2)
        and not rule.second_search_id
        and rule.second_pick_range_type == PICK_RANGE_UNKNOWN
        and not rule.second_pick_reference_search_id
        and rule.second_pick_count_type == PICK_COUNT_UNKNOWN
        and rule.second_pick_count_min == 0
        and rule.second_pick_count_max == 0
        and not rule.chain_effect_ids
        and not rule.card_status_enchant_id
        and not rule.card_grow_effect_ids
        and not rule.effect_group_ids
    )


@dataclass(frozen=True, slots=True)
class Plan3Effect:
    id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str = ""
    status_enchant: Plan3StatusEnchantRule | None = None
    chain_effect_id: str = ""
    chain_effect_ids: tuple[str, ...] = ()
    chain_effect: Plan3Effect | None = None
    trigger: Plan3Trigger | None = None
    once: bool = False
    card_move_rule: Plan3CardMoveRule | None = None


@dataclass(frozen=True, slots=True)
class Plan3Card:
    id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    stamina_cost: int
    force_stamina_cost: int
    cost_type: str
    cost_value: int
    play_trigger: Plan3Trigger | None
    move_position_type: str
    effects: tuple[Plan3Effect, ...]
    effect_group_ids: tuple[str, ...] = ()

    @property
    def ref(self) -> Plan3CardRef:
        return Plan3CardRef(self.id, self.upgrade)


@dataclass(frozen=True, slots=True)
class Plan3StatusEnchantRule:
    id: str
    trigger: Plan3Trigger
    effects: tuple[Plan3Effect, ...]


@dataclass(frozen=True, slots=True)
class ActivePlan3StatusEnchant:
    instance_id: str
    source_id: str
    rule: Plan3StatusEnchantRule
    max_uses: int
    uses: int = 0
    max_uses_per_turn: int = 0
    uses_this_turn: int = 0
    remaining_turns: int = -1
    passing_turn_start: bool = False
    is_item_direct: bool = True
    turn_count: int = 0
    # Native TriggerEffectStatusEffect owns a phase-count dictionary per
    # listener instance.  Only audited phases are stored; absent means zero.
    phase_counts: tuple[tuple[str, int], ...] = ()
    # Native TriggerEffectStatusEffect identity.  Zero/empty remain the
    # compatibility sentinel for older item/memory listeners; the exact
    # Encore path always carries a positive UID and captured playing GUID.
    native_uid: int = 0
    captured_card_guid: str = ""
    is_encore_enchant: bool = False

    def phase_count(self, phase: str) -> int:
        return next(
            (count for stored_phase, count in self.phase_counts if stored_phase == phase),
            0,
        )


@dataclass(frozen=True, slots=True)
class Plan3EnthusiasmMultipleStatus:
    """One native ``EnthusiasticMultipleStatusEffect`` layer.

    Android v3.2.3 stores the permille value at ``+0x28`` and the remaining
    turn count at ``+0x14``.  A negative Master turn creates an unlimited
    layer; finite layers are spent once at the turn boundary.
    """

    value_permille: int
    turns: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value_permille, int)
            or isinstance(self.value_permille, bool)
            or self.value_permille <= 0
        ):
            raise ValueError("enthusiasm multiplier must be a positive permille")
        if (
            not isinstance(self.turns, int)
            or isinstance(self.turns, bool)
            or (self.turns != -1 and self.turns <= 0)
        ):
            raise ValueError(
                "enthusiasm multiplier turns must be -1 or a positive integer"
            )


@dataclass(frozen=True, slots=True)
class Plan3EnthusiasmAdditiveStatus:
    """One native additive layer retained behind the aggregate scalar."""

    value: int
    turns: int
    source_effect_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, int)
            or isinstance(self.value, bool)
            or self.value <= 0
        ):
            raise ValueError("enthusiasm additive value must be positive")
        if (
            not isinstance(self.turns, int)
            or isinstance(self.turns, bool)
            or (self.turns != -1 and self.turns <= 0)
        ):
            raise ValueError(
                "enthusiasm additive turns must be -1 or a positive integer"
            )
        sources = tuple(self.source_effect_ids)
        if any(not isinstance(source, str) or not source for source in sources):
            raise ValueError(
                "enthusiasm additive source effect IDs must be non-empty text"
            )
        object.__setattr__(self, "source_effect_ids", sources)


@dataclass(frozen=True, slots=True)
class Plan3StaminaConsumptionDownFixStatus:
    """One native ``StaminaConsumptionDownFixStatusEffect`` layer.

    Android stores the fixed reduction and its own turn counter on each
    status object.  Keeping layers separate is required because two effects
    can contribute different values and expire on different turn boundaries.
    ``fresh`` mirrors the native passing-turn-start bit: a status created by a
    card is not spent at that card's closing boundary.
    """

    value: int
    turns: int
    fresh: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, int)
            or isinstance(self.value, bool)
            or self.value <= 0
        ):
            raise ValueError(
                "stamina consumption-down fixed value must be positive"
            )
        if (
            not isinstance(self.turns, int)
            or isinstance(self.turns, bool)
            or (self.turns != -1 and self.turns <= 0)
        ):
            raise ValueError(
                "stamina consumption-down fixed turns must be -1 or positive"
            )
        if not isinstance(self.fresh, bool):
            raise ValueError(
                "stamina consumption-down fixed fresh must be a boolean"
            )


@dataclass(frozen=True, slots=True)
class Plan3ItemRule:
    id: str
    name: str
    plan_type: str
    fire_limit: int
    fire_interval: int
    enchantments: tuple[tuple[Plan3StatusEnchantRule, int], ...]
    unsupported_rules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan3GimmickStep:
    priority: int
    start_turn: int
    remaining_turn_permille: int
    field_status_type: str
    field_status_value: int
    field_status_check_type: str
    effect: Plan3Effect


@dataclass(frozen=True, slots=True)
class Plan3GimmickProfile:
    id: str
    steps: tuple[Plan3GimmickStep, ...]


@dataclass(frozen=True, slots=True)
class Plan3State:
    turns_remaining: int
    stamina: int
    # Android ExtraTurnEffectExecutor commits both counters immediately;
    # the ordinary scheduler spends turns_remaining only at the later end
    # boundary.
    extra_turn: int = 0
    score: int = 0
    block: int = 0
    round_number: int = 1
    plays_remaining: int = 1
    stance: str = STANCE_NEUTRAL
    stance_level: int = 0
    stance_change_count: int = 0
    concentration_change_count: int = 0
    preservation_change_count: int = 0
    full_power_change_count: int = 0
    enthusiasm: int = 0
    # Zero-or-one native EnthusiasticStatusEffect sidecar.  The scalar
    # aggregate remains decision-facing; this runtime owns UID/value/turn and
    # passing identity for exact Preservation-family release transitions.
    enthusiastic_runtime: object | None = None
    enthusiasm_additive: int = 0
    # The scalar remains the compatibility projection.  These per-turn
    # layers are authoritative for finite native status lifetime.
    enthusiasm_additive_statuses: tuple[
        Plan3EnthusiasmAdditiveStatus, ...
    ] = ()
    enthusiasm_gain_bonus_percent: int = 0
    enthusiasm_multiple_statuses: tuple[Plan3EnthusiasmMultipleStatus, ...] = ()
    parameter_bonus_percent: int = 0
    limit_border: int = -1
    clear_border: int = -1
    current_turn_total_add_parameter: int = 0
    slump: bool = False
    is_battle: bool = False
    current_parameter_type: int = int(ProduceParameterType.UNKNOWN)
    battle_bonus_permille_vocal: int = 1000
    battle_bonus_permille_dance: int = 1000
    battle_bonus_permille_visual: int = 1000
    judge_parameter_vocal: int = 0
    judge_parameter_dance: int = 0
    judge_parameter_visual: int = 0
    full_power_points: int = 0
    full_power_points_total: int = 0
    # Lazily normalized to Plan3FullPowerPointAdditiveRuntime in
    # ``__post_init__`` to keep the standalone evidence module cycle-free.
    full_power_point_additive_runtime: object | None = None
    # Ordered native LessonParameterMultipleStatusEffect collection.  It is
    # normalized lazily to keep the general Plan 3 module import-light.
    lesson_parameter_multiple_state: object | None = None
    # Native generic stance lock collection projection.  Kept as an object
    # annotation here so the standalone evidence module remains cycle-free;
    # ``__post_init__`` always replaces it with StanceLockRuntime.
    stance_lock_runtime: object | None = None
    lesson_type: str = LESSON_UNKNOWN
    # Exact LocalSave ProduceStepType numeric value.  Zero means the source
    # did not provide it; lesson-gated installation then fails closed.
    step_type_value: int = 0
    stamina_consumption_down_turns: int = 0
    stamina_consumption_down_fresh: bool = False
    # The scalar is retained as a convenient aggregate and for checkpoint
    # compatibility.  ``stamina_consumption_down_fix_statuses`` is the
    # authoritative per-layer representation used for duration spending.
    stamina_consumption_down_fixed: int = 0
    stamina_consumption_down_fix_statuses: tuple[
        Plan3StaminaConsumptionDownFixStatus, ...
    ] = ()
    block_restriction_turns: int = 0
    # Direct-card status creation is not spent at its first following turn
    # boundary.  Merging into an active layer preserves that layer's native
    # passing/fresh marker.
    block_restriction_fresh: bool = False
    # Kept typed at runtime without importing the standalone native evidence
    # module at module import time.
    anti_debuff_runtime: object | None = None
    # Zero-or-one native PlayableValueAddStatusEffect sidecar.  The scalar
    # ``plays_remaining`` stays as the decision-facing aggregate while this
    # typed runtime owns UID/value/turn/passing identity.
    playable_value_add_runtime: object | None = None
    # Ordered native PlayCountBuffStatusEffect layers.  Kept import-light at
    # the state boundary and normalized to PlayCountBuffRuntime below.
    play_count_buff_runtime: object | None = None
    awaiting_turn_start: bool = False
    hand: tuple[Plan3CardRef, ...] = ()
    draw_pile: tuple[Plan3CardRef, ...] = ()
    discard_pile: tuple[Plan3CardRef, ...] = ()
    lost_pile: tuple[Plan3CardRef, ...] = ()
    hold_pile: tuple[Plan3CardRef, ...] = ()
    active_status_enchants: tuple[ActivePlan3StatusEnchant, ...] = ()
    # Monotonic native ExamStatusEffect UID allocator.  LocalSave projection
    # restores ``status._effectCreateCount + 1``; newly created statuses spend
    # this value without scanning one status family in isolation.
    next_status_uid: int = 1
    # False means an executed status-producing family changed the native
    # create/merge lifecycle without a proven cursor receipt. Scalar play may
    # continue, but no later exact status allocation may use this cursor.
    status_uid_cursor_exact: bool = True
    # Recovery is only exact when the settled HUD/LocalSave supplied the cap.
    # ``None`` deliberately keeps recovery cards fail-closed.
    max_stamina: int | None = None
    # None is unknown history, not an empty list. A projected native history
    # owns the StartPlay last-successful-card predicate and future updates.
    play_history: object | None = None

    def __post_init__(self) -> None:
        if self.play_history is not None:
            from .plan3_play_history import Plan3PlayHistory
            history = self.play_history
            if isinstance(history, Mapping):
                history = Plan3PlayHistory.from_dict(history)
            if not isinstance(history, Plan3PlayHistory):
                raise TypeError("play_history must be a typed native history or None")
            object.__setattr__(self, "play_history", history)
        for field_name in (
            "hand",
            "draw_pile",
            "discard_pile",
            "lost_pile",
            "hold_pile",
        ):
            normalized: list[Plan3CardRef] = []
            for raw in getattr(self, field_name):
                if isinstance(raw, Plan3CardRef):
                    normalized.append(raw)
                elif isinstance(raw, Mapping):
                    normalized.append(
                        Plan3CardRef(
                            card_id=str(raw.get("card_id", "")),
                            upgrade=int(raw.get("upgrade", 0)),
                        )
                    )
                else:
                    raise TypeError(f"{field_name} entries must be card references")
            object.__setattr__(self, field_name, tuple(normalized))

        normalized_enchants: list[ActivePlan3StatusEnchant] = []
        for raw in self.active_status_enchants:
            if isinstance(raw, ActivePlan3StatusEnchant):
                normalized_enchants.append(raw)
            elif isinstance(raw, Mapping):
                normalized_enchants.append(_active_enchant_from_dict(raw))
            else:
                raise TypeError(
                    "active_status_enchants entries must be enchantment mappings"
                )
        object.__setattr__(
            self, "active_status_enchants", tuple(normalized_enchants)
        )

        normalized_multipliers: list[Plan3EnthusiasmMultipleStatus] = []
        for raw in self.enthusiasm_multiple_statuses:
            if isinstance(raw, Plan3EnthusiasmMultipleStatus):
                normalized_multipliers.append(raw)
            elif isinstance(raw, Mapping):
                value_permille = raw.get("value_permille", 0)
                turns = raw.get("turns", 0)
                if (
                    not isinstance(value_permille, int)
                    or isinstance(value_permille, bool)
                    or not isinstance(turns, int)
                    or isinstance(turns, bool)
                ):
                    raise TypeError(
                        "enthusiasm multiplier mapping values must be integers"
                    )
                normalized_multipliers.append(
                    Plan3EnthusiasmMultipleStatus(
                        value_permille=value_permille,
                        turns=turns,
                    )
                )
            elif isinstance(raw, (list, tuple)) and len(raw) == 2:
                if (
                    not isinstance(raw[0], int)
                    or isinstance(raw[0], bool)
                    or not isinstance(raw[1], int)
                    or isinstance(raw[1], bool)
                ):
                    raise TypeError(
                        "enthusiasm multiplier sequence values must be integers"
                    )
                normalized_multipliers.append(
                    Plan3EnthusiasmMultipleStatus(
                        value_permille=raw[0],
                        turns=raw[1],
                    )
                )
            else:
                raise TypeError(
                    "enthusiasm_multiple_statuses entries must be status mappings"
                )
        object.__setattr__(
            self,
            "enthusiasm_multiple_statuses",
            tuple(normalized_multipliers),
        )

        normalized_additive_statuses: list[Plan3EnthusiasmAdditiveStatus] = []
        for raw in self.enthusiasm_additive_statuses:
            if isinstance(raw, Plan3EnthusiasmAdditiveStatus):
                normalized_additive_statuses.append(raw)
            elif isinstance(raw, Mapping):
                raw_sources = raw.get("source_effect_ids", ())
                if not isinstance(raw_sources, (list, tuple)):
                    raise TypeError(
                        "enthusiasm additive source_effect_ids must be a sequence"
                    )
                normalized_additive_statuses.append(
                    Plan3EnthusiasmAdditiveStatus(
                        value=raw.get("value", 0),
                        turns=raw.get("turns", 0),
                        source_effect_ids=tuple(raw_sources),
                    )
                )
            elif isinstance(raw, (list, tuple)) and len(raw) in (2, 3):
                normalized_additive_statuses.append(
                    Plan3EnthusiasmAdditiveStatus(
                        value=raw[0],
                        turns=raw[1],
                        source_effect_ids=(
                            tuple(raw[2]) if len(raw) == 3 else ()
                        ),
                    )
                )
            else:
                raise TypeError(
                    "enthusiasm_additive_statuses entries must be statuses"
                )
        additive_total = self.enthusiasm_additive
        if (
            not isinstance(additive_total, int)
            or isinstance(additive_total, bool)
            or additive_total < 0
        ):
            raise ValueError(
                "enthusiasm_additive must be a non-negative integer"
            )
        layer_total = sum(status.value for status in normalized_additive_statuses)
        if additive_total == 0 and layer_total:
            additive_total = layer_total
        elif additive_total and not normalized_additive_statuses:
            # A legacy scalar checkpoint is an unlimited native layer.  This
            # retains compatibility without inventing an expiry boundary.
            normalized_additive_statuses.append(
                Plan3EnthusiasmAdditiveStatus(
                    value=additive_total,
                    turns=-1,
                )
            )
        elif additive_total != layer_total:
            raise ValueError(
                "enthusiasm additive aggregate does not match layers"
            )
        object.__setattr__(
            self,
            "enthusiasm_additive_statuses",
            tuple(normalized_additive_statuses),
        )
        object.__setattr__(self, "enthusiasm_additive", additive_total)

        from .plan3_full_power_point_additive import (
            Plan3FullPowerPointAdditiveLayer,
            Plan3FullPowerPointAdditiveRuntime,
        )

        additive_runtime = self.full_power_point_additive_runtime
        if additive_runtime is None:
            additive_runtime = Plan3FullPowerPointAdditiveRuntime()
        elif isinstance(additive_runtime, Mapping):
            raw_layers = additive_runtime.get("layers", ())
            if not isinstance(raw_layers, (list, tuple)):
                raise TypeError(
                    "full power point additive layers must be a sequence"
                )
            additive_runtime = Plan3FullPowerPointAdditiveRuntime(
                tuple(
                    layer
                    if isinstance(layer, Plan3FullPowerPointAdditiveLayer)
                    else Plan3FullPowerPointAdditiveLayer(
                        value_permille=layer.get("value_permille", 0),
                        remaining_turns=layer.get("remaining_turns", 0),
                        source_effect_ids=tuple(
                            layer.get("source_effect_ids", ())
                        ),
                    )
                    if isinstance(layer, Mapping)
                    else layer
                    for layer in raw_layers
                )
            )
        if not isinstance(
            additive_runtime, Plan3FullPowerPointAdditiveRuntime
        ):
            raise TypeError(
                "full_power_point_additive_runtime must be a typed runtime"
            )
        object.__setattr__(
            self, "full_power_point_additive_runtime", additive_runtime
        )

        from .nia_lesson_value_multiple import (
            LessonParameterMultipleState,
            LessonParameterMultipleStatus,
        )

        lesson_multiple = self.lesson_parameter_multiple_state
        if lesson_multiple is None:
            lesson_multiple = LessonParameterMultipleState()
        elif isinstance(lesson_multiple, Mapping):
            raw_statuses = lesson_multiple.get("statuses", ())
            if not isinstance(raw_statuses, (list, tuple)):
                raise TypeError(
                    "lesson parameter multiple statuses must be a sequence"
                )
            lesson_multiple = LessonParameterMultipleState(
                tuple(
                    raw
                    if isinstance(raw, LessonParameterMultipleStatus)
                    else LessonParameterMultipleStatus(
                        permil=raw.get("permil", 0),
                        turn=raw.get("turn", 0),
                        passing_turn_start=raw.get(
                            "passing_turn_start", False
                        ),
                        is_turn_limited=raw.get("is_turn_limited"),
                        uid=raw.get("uid"),
                    )
                    if isinstance(raw, Mapping)
                    else raw
                    for raw in raw_statuses
                )
            )
        if not isinstance(lesson_multiple, LessonParameterMultipleState):
            raise TypeError(
                "lesson_parameter_multiple_state must be a typed runtime"
            )
        object.__setattr__(
            self, "lesson_parameter_multiple_state", lesson_multiple
        )

        from .plan3_stance_lock import StanceLockRuntime, StanceLockStatus

        stance_lock_runtime = self.stance_lock_runtime
        if stance_lock_runtime is None:
            stance_lock_runtime = StanceLockRuntime()
        elif isinstance(stance_lock_runtime, Mapping):
            raw_status = stance_lock_runtime.get("status")
            if raw_status is None:
                status = None
            elif isinstance(raw_status, StanceLockStatus):
                status = raw_status
            elif isinstance(raw_status, Mapping):
                status = StanceLockStatus(
                    uid=raw_status.get("uid", 0),
                    turn=raw_status.get("turn", 0),
                    is_passing_turn_start=raw_status.get(
                        "is_passing_turn_start", False
                    ),
                )
            else:
                raise TypeError("stance lock status must be a mapping")
            stance_lock_runtime = StanceLockRuntime(
                status=status,
                effect_create_count=stance_lock_runtime.get(
                    "effect_create_count", 0
                ),
                recently_used_uids=frozenset(
                    stance_lock_runtime.get("recently_used_uids", ())
                ),
            )
        if not isinstance(stance_lock_runtime, StanceLockRuntime):
            raise TypeError("stance_lock_runtime must be a typed runtime")
        object.__setattr__(
            self, "stance_lock_runtime", stance_lock_runtime
        )

        normalized_stamina_fix_statuses: list[
            Plan3StaminaConsumptionDownFixStatus
        ] = []
        for raw in self.stamina_consumption_down_fix_statuses:
            if isinstance(raw, Plan3StaminaConsumptionDownFixStatus):
                normalized_stamina_fix_statuses.append(raw)
            elif isinstance(raw, Mapping):
                normalized_stamina_fix_statuses.append(
                    Plan3StaminaConsumptionDownFixStatus(
                        value=raw.get("value", 0),
                        turns=raw.get("turns", 0),
                        fresh=raw.get("fresh", False),
                    )
                )
            elif isinstance(raw, (list, tuple)) and len(raw) in (2, 3):
                normalized_stamina_fix_statuses.append(
                    Plan3StaminaConsumptionDownFixStatus(
                        value=raw[0],
                        turns=raw[1],
                        fresh=(raw[2] if len(raw) == 3 else False),
                    )
                )
            else:
                raise TypeError(
                    "stamina consumption-down fixed entries must be statuses"
                )

        fixed_total = self.stamina_consumption_down_fixed
        if (
            not isinstance(fixed_total, int)
            or isinstance(fixed_total, bool)
            or fixed_total < 0
        ):
            raise ValueError(
                "stamina_consumption_down_fixed must be a non-negative integer"
            )
        layer_total = sum(status.value for status in normalized_stamina_fix_statuses)
        if fixed_total == 0 and layer_total:
            fixed_total = layer_total
        elif fixed_total and not normalized_stamina_fix_statuses:
            # Older scalar checkpoints have no layer metadata.  Preserve them
            # as an unlimited native status instead of silently dropping the
            # reduction or inventing a finite lifetime.
            normalized_stamina_fix_statuses.append(
                Plan3StaminaConsumptionDownFixStatus(
                    value=fixed_total,
                    turns=-1,
                    fresh=False,
                )
            )
        elif fixed_total != layer_total:
            raise ValueError(
                "stamina consumption-down fixed aggregate does not match layers"
            )
        object.__setattr__(
            self,
            "stamina_consumption_down_fix_statuses",
            tuple(normalized_stamina_fix_statuses),
        )
        object.__setattr__(self, "stamina_consumption_down_fixed", fixed_total)

        from .plan3_anti_debuff import AntiDebuffRuntime

        anti_debuff_runtime = self.anti_debuff_runtime
        if anti_debuff_runtime is None:
            anti_debuff_runtime = AntiDebuffRuntime()
        elif isinstance(anti_debuff_runtime, Mapping):
            anti_debuff_runtime = AntiDebuffRuntime(
                count=anti_debuff_runtime.get("count", 0),
                uid=anti_debuff_runtime.get("uid"),
                passing_turn_start=anti_debuff_runtime.get(
                    "passing_turn_start", False
                ),
            )
        if not isinstance(anti_debuff_runtime, AntiDebuffRuntime):
            raise TypeError("anti_debuff_runtime must be a typed runtime")
        object.__setattr__(
            self, "anti_debuff_runtime", anti_debuff_runtime
        )

        from .enthusiastic_runtime import (
            EnthusiasticRuntime,
            EnthusiasticStatus,
        )

        enthusiastic_runtime = self.enthusiastic_runtime
        if enthusiastic_runtime is None:
            enthusiastic_runtime = EnthusiasticRuntime()
        elif isinstance(enthusiastic_runtime, Mapping):
            raw_status = enthusiastic_runtime.get("status")
            if raw_status is None:
                enthusiastic_runtime = EnthusiasticRuntime()
            elif isinstance(raw_status, EnthusiasticStatus):
                enthusiastic_runtime = EnthusiasticRuntime(raw_status)
            elif isinstance(raw_status, Mapping):
                enthusiastic_runtime = EnthusiasticRuntime(
                    EnthusiasticStatus(
                        uid=raw_status.get("uid", 0),
                        value=raw_status.get("value", 0),
                        turn=raw_status.get("turn", 1),
                        passing_turn_start=raw_status.get(
                            "passing_turn_start", True
                        ),
                    )
                )
            else:
                raise TypeError(
                    "enthusiastic_runtime status must be a mapping"
                )
        if not isinstance(enthusiastic_runtime, EnthusiasticRuntime):
            raise TypeError("enthusiastic_runtime must be a typed runtime")
        object.__setattr__(
            self,
            "enthusiastic_runtime",
            enthusiastic_runtime,
        )

        from .playable_value_add_runtime import (
            PlayableValueAddRuntime,
            PlayableValueAddStatus,
        )

        playable_runtime = self.playable_value_add_runtime
        if playable_runtime is None:
            playable_runtime = PlayableValueAddRuntime()
        elif isinstance(playable_runtime, Mapping):
            raw_status = playable_runtime.get("status")
            if raw_status is None:
                playable_runtime = PlayableValueAddRuntime()
            elif isinstance(raw_status, PlayableValueAddStatus):
                playable_runtime = PlayableValueAddRuntime(raw_status)
            elif isinstance(raw_status, Mapping):
                playable_runtime = PlayableValueAddRuntime(
                    PlayableValueAddStatus(
                        uid=raw_status.get("uid", 0),
                        value=raw_status.get("value", 0),
                        turn=raw_status.get("turn", 1),
                        passing_turn_start=raw_status.get(
                            "passing_turn_start", False
                        ),
                    )
                )
            else:
                raise TypeError(
                    "playable_value_add_runtime status must be a mapping"
                )
        if not isinstance(playable_runtime, PlayableValueAddRuntime):
            raise TypeError(
                "playable_value_add_runtime must be a typed runtime"
            )
        object.__setattr__(
            self,
            "playable_value_add_runtime",
            playable_runtime,
        )

        from .plan3_card_search_effect_play_count_buff import (
            PlayCountBuffRuntime,
            PlayCountBuffStatus,
        )

        play_count_buff_runtime = self.play_count_buff_runtime
        if play_count_buff_runtime is None:
            play_count_buff_runtime = PlayCountBuffRuntime()
        elif isinstance(play_count_buff_runtime, Mapping):
            raw_statuses = play_count_buff_runtime.get("statuses", ())
            if not isinstance(raw_statuses, (list, tuple)):
                raise TypeError("play count buff statuses must be a sequence")
            play_count_buff_runtime = PlayCountBuffRuntime(
                tuple(
                    raw
                    if isinstance(raw, PlayCountBuffStatus)
                    else PlayCountBuffStatus(
                        limit=raw.get("limit", 0),
                        remaining_count=raw.get("remaining_count", 0),
                        search_id=raw.get(
                            "search_id",
                            "p_card_search-n-r-sr-ssr-playing",
                        ),
                        turn=raw.get("turn", -1),
                        native_uid=raw.get("native_uid", 0),
                    )
                    if isinstance(raw, Mapping)
                    else raw
                    for raw in raw_statuses
                )
            )
        if not isinstance(play_count_buff_runtime, PlayCountBuffRuntime):
            raise TypeError(
                "play_count_buff_runtime must be a typed runtime"
            )
        object.__setattr__(
            self, "play_count_buff_runtime", play_count_buff_runtime
        )
        self.validate(allow_completed=True)

    def validate(self, *, allow_completed: bool = False) -> None:
        nonnegative = {
            "turns_remaining": self.turns_remaining,
            "stamina": self.stamina,
            "extra_turn": self.extra_turn,
            "score": self.score,
            "block": self.block,
            "plays_remaining": self.plays_remaining,
            "stance_change_count": self.stance_change_count,
            "concentration_change_count": self.concentration_change_count,
            "preservation_change_count": self.preservation_change_count,
            "full_power_change_count": self.full_power_change_count,
            "enthusiasm": self.enthusiasm,
            "enthusiasm_additive": self.enthusiasm_additive,
            "enthusiasm_gain_bonus_percent": self.enthusiasm_gain_bonus_percent,
            "parameter_bonus_percent": self.parameter_bonus_percent,
            "full_power_points": self.full_power_points,
            "full_power_points_total": self.full_power_points_total,
            "stamina_consumption_down_turns": (
                self.stamina_consumption_down_turns
            ),
            "stamina_consumption_down_fixed": self.stamina_consumption_down_fixed,
            "block_restriction_turns": self.block_restriction_turns,
            "next_status_uid": self.next_status_uid,
        }
        for name, value in nonnegative.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not 1 <= self.next_status_uid <= 2**31 - 1:
            raise ValueError("next_status_uid must be a positive Int32")
        if (
            self.playable_value_add_runtime.uid is not None
            and self.next_status_uid <= self.playable_value_add_runtime.uid
        ):
            raise ValueError(
                "next_status_uid must exceed active playable status UID"
            )
        if (
            self.enthusiastic_runtime.uid is not None
            and self.next_status_uid <= self.enthusiastic_runtime.uid
        ):
            raise ValueError(
                "next_status_uid must exceed active enthusiastic status UID"
            )
        if (
            self.enthusiastic_runtime.status_present
            and self.enthusiasm != self.enthusiastic_runtime.value
        ):
            raise ValueError(
                "enthusiasm aggregate must match typed enthusiastic runtime"
            )
        if type(self.status_uid_cursor_exact) is not bool:
            raise ValueError("status_uid_cursor_exact must be boolean")
        if self.max_stamina is not None:
            if (
                not isinstance(self.max_stamina, int)
                or isinstance(self.max_stamina, bool)
                or self.max_stamina < 0
            ):
                raise ValueError("max_stamina must be a non-negative integer or None")
            if self.stamina > self.max_stamina:
                raise ValueError("stamina must not exceed max_stamina")
        if self.score > 2**31 - 1:
            raise ValueError("score must not exceed Int32 max")
        signed_i32 = {
            "limit_border": self.limit_border,
            "clear_border": self.clear_border,
            "current_turn_total_add_parameter": (
                self.current_turn_total_add_parameter
            ),
            "battle_bonus_permille_vocal": self.battle_bonus_permille_vocal,
            "battle_bonus_permille_dance": self.battle_bonus_permille_dance,
            "battle_bonus_permille_visual": self.battle_bonus_permille_visual,
            "judge_parameter_vocal": self.judge_parameter_vocal,
            "judge_parameter_dance": self.judge_parameter_dance,
            "judge_parameter_visual": self.judge_parameter_visual,
        }
        for name, value in signed_i32.items():
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not -(2**31) <= value <= 2**31 - 1
            ):
                raise ValueError(f"{name} must be an Int32")
        if not isinstance(self.slump, bool):
            raise ValueError("slump must be a boolean")
        if not isinstance(self.is_battle, bool):
            raise ValueError("is_battle must be a boolean")
        if not isinstance(self.stamina_consumption_down_fresh, bool):
            raise ValueError("stamina_consumption_down_fresh must be a boolean")
        if not isinstance(self.block_restriction_fresh, bool):
            raise ValueError("block_restriction_fresh must be a boolean")
        if self.stamina_consumption_down_fixed != sum(
            status.value for status in self.stamina_consumption_down_fix_statuses
        ):
            raise ValueError(
                "stamina consumption-down fixed aggregate does not match layers"
            )
        if self.enthusiasm_additive != sum(
            status.value for status in self.enthusiasm_additive_statuses
        ):
            raise ValueError(
                "enthusiasm additive aggregate does not match layers"
            )
        if not isinstance(self.lesson_type, str) or not self.lesson_type:
            raise ValueError("lesson_type must be a non-empty Master enum")
        if (
            not isinstance(self.step_type_value, int)
            or isinstance(self.step_type_value, bool)
            or self.step_type_value not in {0, *range(1, 10), *range(29, 35),
                                           *((16, 17, 18) if self.is_battle else ())}
        ):
            raise ValueError("step_type_value must be 0, a lesson step, or battle audition 16/17/18")
        if self.current_parameter_type not in {
            int(ProduceParameterType.UNKNOWN),
            int(ProduceParameterType.VOCAL),
            int(ProduceParameterType.DANCE),
            int(ProduceParameterType.VISUAL),
        }:
            raise ValueError("current_parameter_type is unsupported")
        if self.round_number < 1:
            raise ValueError("round_number must be positive")
        if not isinstance(self.awaiting_turn_start, bool):
            raise ValueError("awaiting_turn_start must be a boolean")
        # A settled LocalSave can retain the just-ended turn's active
        # PlayableValueAdd value while ``isTurnCardPlayEnd`` already marks the
        # TurnStart boundary. ``start_plan3_turn`` rebuilds the next turn's
        # base playable value instead of carrying this projection forward.
        if not allow_completed and self.turns_remaining == 0:
            raise ValueError("completed state cannot accept a card")
        if self.stance not in SUPPORTED_STANCES:
            raise ValueError(f"unknown stance: {self.stance}")
        if self.stance == STANCE_NEUTRAL:
            if self.stance_level != 0:
                raise ValueError("neutral stance must have level 0")
        elif self.stance == STANCE_FULL_POWER:
            if self.stance_level != 1:
                raise ValueError("full_power stance must have level 1")
        elif self.stance_level not in (1, 2, 3):
            raise ValueError("active stance level must be 1, 2, or 3")
        instance_ids: set[str] = set()
        native_status_uids: set[int] = set()
        for enchant in self.active_status_enchants:
            if not enchant.instance_id or enchant.instance_id in instance_ids:
                raise ValueError("status enchant instance IDs must be unique")
            instance_ids.add(enchant.instance_id)
            if enchant.max_uses < 0:
                raise ValueError("status enchant max_uses must be non-negative")
            if enchant.uses < 0 or (
                enchant.max_uses > 0 and enchant.uses > enchant.max_uses
            ):
                raise ValueError("status enchant uses are out of range")
            if enchant.max_uses_per_turn < 0:
                raise ValueError(
                    "status enchant max_uses_per_turn must be non-negative"
                )
            if enchant.uses_this_turn < 0 or (
                enchant.max_uses_per_turn > 0
                and enchant.uses_this_turn > enchant.max_uses_per_turn
            ):
                raise ValueError(
                    "status enchant uses_this_turn is out of range"
                )
            if enchant.remaining_turns < -1:
                raise ValueError(
                    "status enchant remaining_turns must be -1 or non-negative"
                )
            if not isinstance(enchant.passing_turn_start, bool):
                raise ValueError(
                    "status enchant passing_turn_start must be a boolean"
                )
            if not isinstance(enchant.is_item_direct, bool):
                raise ValueError("status enchant is_item_direct must be a boolean")
            if (
                not isinstance(enchant.turn_count, int)
                or isinstance(enchant.turn_count, bool)
                or not 0 <= enchant.turn_count <= 2**31 - 1
            ):
                raise ValueError(
                    "status enchant turn_count must be a non-negative Int32"
                )
            phase_keys: set[str] = set()
            for phase, count in enchant.phase_counts:
                if not isinstance(phase, str) or not phase or phase in phase_keys:
                    raise ValueError(
                        "status enchant phase-count keys must be unique text"
                    )
                phase_keys.add(phase)
                if (
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or not 0 <= count <= 2**31 - 1
                ):
                    raise ValueError(
                        "status enchant phase counts must be non-negative Int32"
                    )
            if (
                not isinstance(enchant.native_uid, int)
                or isinstance(enchant.native_uid, bool)
                or enchant.native_uid < 0
            ):
                raise ValueError("status enchant native_uid must be non-negative")
            if enchant.native_uid:
                if enchant.native_uid in native_status_uids:
                    raise ValueError("status enchant native UIDs must be unique")
                native_status_uids.add(enchant.native_uid)
            if not isinstance(enchant.captured_card_guid, str):
                raise ValueError("status enchant captured GUID must be text")
            if not isinstance(enchant.is_encore_enchant, bool):
                raise ValueError("status enchant is_encore_enchant must be boolean")
            if enchant.is_encore_enchant and (
                enchant.native_uid <= 0 or not enchant.captured_card_guid
            ):
                raise ValueError(
                    "Encore status requires positive UID and captured GUID"
                )
        multiplier_turns: set[int] = set()
        for status in self.enthusiasm_multiple_statuses:
            if status.turns in multiplier_turns:
                raise ValueError(
                    "enthusiasm multiplier turn layers must be unique"
                )
            multiplier_turns.add(status.turns)
        additive_turns: set[int] = set()
        for status in self.enthusiasm_additive_statuses:
            if status.turns in additive_turns:
                raise ValueError(
                    "enthusiasm additive turn layers must be unique"
                )
            additive_turns.add(status.turns)

    @property
    def is_clear(self) -> bool:
        """Whether the exact current score has reached the configured border."""

        return self.clear_border >= 0 and self.score >= self.clear_border

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["stance_lock_runtime"] = self.stance_lock_runtime.to_json()
        payload["anti_debuff_runtime"] = self.anti_debuff_runtime.to_json()
        payload["enthusiastic_runtime"] = self.enthusiastic_runtime.to_dict()
        payload["playable_value_add_runtime"] = (
            self.playable_value_add_runtime.to_dict()
        )
        return payload

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Plan3State":
        return cls(**dict(payload))

    @classmethod
    def from_json(cls, payload: str) -> "Plan3State":
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("Plan3State JSON must contain an object")
        return cls.from_dict(value)


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    supported: bool
    fires: bool
    unsupported_rules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan3ItemInstall:
    before: Plan3State
    after: Plan3State
    item: Plan3ItemRule
    supported: bool
    unsupported_rules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan3RuntimeEffectEvent:
    """One ordered runtime effect slot selected at a native phase boundary."""

    sequence_index: int
    phase: str
    source_enchant_instance_id: str
    source_rule_id: str
    source_id: str
    effect: Plan3Effect
    before: Plan3State
    after: Plan3State
    requested_count: int | None = None
    # For effects inserted after one direct card command, retain its exact
    # slot (including repeated effects). None denotes another phase owner.
    source_direct_effect_index: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sequence_index, int)
            or isinstance(self.sequence_index, bool)
            or self.sequence_index < 0
        ):
            raise ValueError("runtime effect sequence_index must be non-negative")
        for name in (
            "phase",
            "source_enchant_instance_id",
            "source_rule_id",
            "source_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"runtime effect {name} must be non-empty text")
        if not isinstance(self.effect, Plan3Effect):
            raise TypeError("runtime effect must be Plan3Effect")
        if not isinstance(self.before, Plan3State) or not isinstance(
            self.after, Plan3State
        ):
            raise TypeError("runtime effect before/after must be Plan3State")
        expected_request = (
            self.effect.value1
            if self.effect.effect_type == EFFECT_CARD_DRAW
            else None
        )
        if self.requested_count != expected_request:
            raise ValueError("runtime effect requested_count does not match effect")
        if self.source_direct_effect_index is not None and (
            type(self.source_direct_effect_index) is not int
            or self.source_direct_effect_index < 0
        ):
            raise ValueError("runtime effect source_direct_effect_index must be non-negative")

    @property
    def effect_id(self) -> str:
        return self.effect.id

    @property
    def effect_type(self) -> str:
        return self.effect.effect_type


@dataclass(frozen=True, slots=True)
class Plan3TurnStart:
    before: Plan3State
    after: Plan3State
    supported: bool
    unsupported_rules: tuple[str, ...] = ()
    entered_full_power: bool = False
    full_power_points_consumed: int = 0
    held_cards_returned: tuple[Plan3CardRef, ...] = ()
    drawn_cards: tuple[Plan3CardRef, ...] = ()
    used_observed_hand: bool = False
    fired_gimmick_effect_ids: tuple[str, ...] = ()
    gimmick_stamina_paid: int = 0
    gimmick_stamina_recovered: int = 0
    fired_status_enchant_ids: tuple[str, ...] = ()
    fired_status_effect_ids: tuple[str, ...] = ()
    runtime_effect_events: tuple[Plan3RuntimeEffectEvent, ...] = ()
    # The stance owner creates/merges this native status during FullPower
    # entry before draw. Preserve its proof for the native runtime carrier.
    enthusiastic_receipts: tuple["EnthusiasticReceipt", ...] = ()


@dataclass(frozen=True, slots=True)
class Plan3PreDirectTransformResult:
    """Pure external phase result inserted before ordered card effects."""

    state: Plan3State
    unsupported_rules: tuple[str, ...] = ()
    parameter_pre_fix_values: tuple[int, ...] = ()
    parameter_actual_values: tuple[int, ...] = ()
    parameter_clear_changed: bool = False
    parameter_perfect_changed: bool = False
    fired_status_enchant_ids: tuple[str, ...] = ()
    fired_effect_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, Plan3State):
            raise TypeError("pre-direct transform state must be Plan3State")
        for values, label in (
            (self.unsupported_rules, "unsupported_rules"),
            (
                self.fired_status_enchant_ids,
                "fired_status_enchant_ids",
            ),
            (self.fired_effect_ids, "fired_effect_ids"),
        ):
            normalized = tuple(values)
            if any(not isinstance(value, str) or not value for value in normalized):
                raise TypeError(f"{label} must contain non-empty text")
            object.__setattr__(self, label, normalized)
        for values, label in (
            (self.parameter_pre_fix_values, "parameter_pre_fix_values"),
            (self.parameter_actual_values, "parameter_actual_values"),
        ):
            normalized = tuple(values)
            if any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in normalized
            ):
                raise TypeError(f"{label} must contain integers")
            object.__setattr__(self, label, normalized)
        if type(self.parameter_clear_changed) is not bool:
            raise TypeError("parameter_clear_changed must be boolean")
        if type(self.parameter_perfect_changed) is not bool:
            raise TypeError("parameter_perfect_changed must be boolean")


Plan3PreDirectTransform = Callable[
    [Plan3State, tuple[Plan3RuntimeEffectEvent, ...]],
    Plan3PreDirectTransformResult,
]


@dataclass(frozen=True, slots=True)
class Plan3Transition:
    before: Plan3State
    after: Plan3State
    card: Plan3Card
    supported: bool
    legal: bool
    unsupported_rules: tuple[str, ...] = ()
    block_paid: int = 0
    stamina_paid: int = 0
    force_stamina_paid: int = 0
    stamina_penetration_paid: int = 0
    stamina_direct_paid: int = 0
    raw_stamina_cost: int = 0
    effective_stamina_cost: int = 0
    raw_force_stamina_cost: int = 0
    effective_force_stamina_cost: int = 0
    score_gain: int = 0
    parameter_pre_fix_values: tuple[int, ...] = ()
    parameter_actual_values: tuple[int, ...] = ()
    parameter_clear_changed: bool = False
    parameter_perfect_changed: bool = False
    enthusiasm_released: int = 0
    extra_plays: int = 0
    extra_turns_added: int = 0
    full_power_points_gained: int = 0
    full_power_points_reduced: int = 0
    stamina_recovered: int = 0
    draw_count: int = 0
    fired_status_enchant_ids: tuple[str, ...] = ()
    fired_status_effect_ids: tuple[str, ...] = ()
    moved_to: str = ""
    card_move_selected_refs: tuple[Plan3CardRef, ...] = ()
    unverified_rules: tuple[str, ...] = ()
    native_add_grow_effects: tuple[Plan3Effect, ...] = ()
    native_deferred_effects: tuple[Plan3Effect, ...] = ()
    runtime_effect_events: tuple[Plan3RuntimeEffectEvent, ...] = ()
    # Ordered native status operations emitted by a Preservation-family
    # release.  The order is PlayableValueAdd first, then Enthusiastic.
    release_status_receipts: tuple[object, ...] = ()
    direct_command_snapshots: tuple[object, ...] = ()

    @property
    def fully_supported(self) -> bool:
        return self.supported and not self.unsupported_rules

    @property
    def autonomous_safe(self) -> bool:
        return self.fully_supported and not self.unverified_rules


def _trigger_from_row(row: sqlite3.Row) -> Plan3Trigger:
    return Plan3Trigger(
        id=str(row["id"]),
        phase_types=_str_tuple(row["phase_types_json"], f"trigger:{row['id']}:phases"),
        phase_values=_int_tuple(row["phase_values_json"], f"trigger:{row['id']}:phase-values"),
        field_check_types=_str_tuple(
            row["field_status_check_types_json"], f"trigger:{row['id']}:checks"
        ),
        field_types=_str_tuple(
            row["field_status_types_json"], f"trigger:{row['id']}:fields"
        ),
        field_values=_int_tuple(
            row["field_status_values_json"], f"trigger:{row['id']}:field-values"
        ),
        field_card_search_ids=_str_tuple(
            row["field_status_produce_card_search_ids_json"],
            f"trigger:{row['id']}:field-card-search-ids",
        ),
        produce_card_search_id=str(row["produce_card_search_id"]),
        upper_search_count=int(row["upper_search_count"]),
        lower_search_count=int(row["lower_search_count"]),
        card_move_position_type=str(row["card_move_position_type"]),
        effect_types=_str_tuple(
            row["effect_types_json"], f"trigger:{row['id']}:effect-types"
        ),
        lesson_type=str(row["lesson_type"]),
    )


def _load_trigger(
    connection: sqlite3.Connection, trigger_id: str
) -> Plan3Trigger | None:
    if not trigger_id:
        return None
    row = connection.execute(
        "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Master trigger not found: {trigger_id}")
    return _trigger_from_row(row)


def _card_move_rule_from_master(
    raw: Mapping[str, Any], effect_id: str
) -> Plan3CardMoveRule:
    required = {
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
        "chainProduceExamEffectIds",
        "produceCardStatusEnchantId",
        "produceCardGrowEffectIds",
        "effectGroupIds",
    }
    missing = sorted(required.difference(raw))
    if missing:
        raise ValueError(
            f"card-move Master fields missing: {effect_id}:{','.join(missing)}"
        )

    def text(key: str) -> str:
        value = raw[key]
        if not isinstance(value, str):
            raise ValueError(f"card-move Master text is invalid: {effect_id}:{key}")
        return value

    def integer(key: str) -> int:
        value = raw[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                f"card-move Master integer is invalid: {effect_id}:{key}"
            )
        return value

    def texts(key: str) -> tuple[str, ...]:
        value = raw[key]
        if not isinstance(value, list) or not all(
            isinstance(entry, str) and entry for entry in value
        ):
            raise ValueError(
                f"card-move Master string array is invalid: {effect_id}:{key}"
            )
        return tuple(value)

    return Plan3CardMoveRule(
        target_card_id=text("targetProduceCardId"),
        target_upgrade=integer("targetUpgradeCount"),
        target_effect_type=text("targetExamEffectType"),
        search_id=text("produceCardSearchId"),
        destination=text("movePositionType"),
        pick_range_type=text("pickRangeType"),
        pick_reference_search_id=text(
            "pickCountReferenceProduceCardSearchId"
        ),
        pick_count_type=text("pickCountType"),
        pick_count_min=integer("pickCountMin"),
        pick_count_max=integer("pickCountMax"),
        second_search_id=text("produceCardSearchId2"),
        second_pick_range_type=text("pickRangeType2"),
        second_pick_reference_search_id=text(
            "pickCountReferenceProduceCardSearchId2"
        ),
        second_pick_count_type=text("pickCountType2"),
        second_pick_count_min=integer("pickCountMin2"),
        second_pick_count_max=integer("pickCountMax2"),
        chain_effect_ids=texts("chainProduceExamEffectIds"),
        card_status_enchant_id=text("produceCardStatusEnchantId"),
        card_grow_effect_ids=texts("produceCardGrowEffectIds"),
        effect_group_ids=texts("effectGroupIds"),
    )


def _effect_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    trigger_id: str = "",
    once: bool = False,
    resolve_timer_chain: bool = True,
) -> Plan3Effect:
    status_enchant_id = str(row["status_enchant_id"])
    effect_type = str(row["effect_type"])
    raw = json.loads(str(row["raw_json"]))
    if not isinstance(raw, dict):
        raise ValueError(f"effect Master payload must be an object: {row['id']}")
    raw_chain_effect_ids = raw.get("chainProduceExamEffectIds", [])
    if not isinstance(raw_chain_effect_ids, list) or not all(
        isinstance(value, str) and value for value in raw_chain_effect_ids
    ):
        raise ValueError(
            f"effect Master chain list is invalid: {row['id']}"
        )
    chain_effect_id = str(row["chain_effect_id"])
    chain_effect: Plan3Effect | None = None
    if effect_type == EFFECT_TIMER:
        raw_chain_effect_id = raw.get("chainProduceExamEffectId", "")
        if not isinstance(raw_chain_effect_id, str):
            raise ValueError(
                f"effect Master chain ID is invalid: {row['id']}"
            )
        if raw_chain_effect_id != chain_effect_id:
            raise ValueError(
                f"effect Master chain ID differs from imported row: {row['id']}"
            )
        if resolve_timer_chain and chain_effect_id:
            try:
                # Current reachable EffectTimer rows carry exactly one direct
                # child.  Loading only that child keeps a nested/cyclic timer
                # representable so strict validation can reject it atomically.
                chain_effect = _load_effect(
                    connection,
                    chain_effect_id,
                    resolve_timer_chain=False,
                )
            except KeyError:
                chain_effect = None
    return Plan3Effect(
        id=str(row["id"]),
        effect_type=effect_type,
        value1=int(row["value1"]),
        value2=int(row["value2"]),
        effect_count=int(row["effect_count"]),
        effect_turn=int(row["effect_turn"]),
        status_enchant_id=status_enchant_id,
        status_enchant=(
            _load_status_enchant(connection, status_enchant_id)
            if status_enchant_id
            else None
        ),
        chain_effect_id=chain_effect_id,
        chain_effect_ids=tuple(raw_chain_effect_ids),
        chain_effect=chain_effect,
        trigger=_load_trigger(connection, trigger_id),
        once=once,
        card_move_rule=(
            _card_move_rule_from_master(raw, str(row["id"]))
            if effect_type in {EFFECT_CARD_MOVE, EFFECT_ADD_GROW}
            else None
        ),
    )


def _load_effect(
    connection: sqlite3.Connection,
    effect_id: str,
    *,
    trigger_id: str = "",
    once: bool = False,
    resolve_timer_chain: bool = True,
) -> Plan3Effect:
    row = connection.execute(
        """
        SELECT id, effect_type, value1, value2, effect_count, effect_turn,
               status_enchant_id, chain_effect_id, raw_json
          FROM effect
         WHERE id = ?
        """,
        (effect_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Master effect not found: {effect_id}")
    return _effect_from_row(
        connection,
        row,
        trigger_id=trigger_id,
        once=once,
        resolve_timer_chain=resolve_timer_chain,
    )


def load_plan3_effect(
    effect_id: str, database: Path = DEFAULT_DATABASE
) -> Plan3Effect:
    """Load one standalone Master effect for status/lifecycle composition."""

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return _load_effect(connection, effect_id)


def load_plan3_status_enchant(
    enchant_id: str, database: Path = DEFAULT_DATABASE
) -> Plan3StatusEnchantRule:
    """Load one exact status-enchant trigger/effect chain from Master."""

    if not isinstance(enchant_id, str) or not enchant_id:
        raise ValueError("enchant_id must be non-empty text")
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return _load_status_enchant(connection, enchant_id)


@lru_cache(maxsize=8)
def load_plan3_gimmick_profile(
    group_id: str = PLAN3_MID1_DANCE_GIMMICK_ID,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
    database: Path = DEFAULT_DATABASE,
) -> Plan3GimmickProfile:
    """Load one exact repeated-ID turn schedule from static Master."""

    path = master_dir / "ProduceExamGimmickEffectGroup.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"local Master file not found: {path}")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list):
        raise ValueError("ProduceExamGimmickEffectGroup must contain an array")
    rows = [
        row
        for row in payload
        if isinstance(row, dict) and row.get("id") == group_id
    ]
    if not rows:
        raise KeyError(f"gimmick group not found: {group_id}")
    steps: list[Plan3GimmickStep] = []
    for row in rows:
        effect_id = row.get("produceExamEffectId")
        if not isinstance(effect_id, str) or not effect_id:
            raise ValueError(f"gimmick effect ID is invalid: {group_id}")
        steps.append(
            Plan3GimmickStep(
                priority=int(row.get("priority", 0)),
                start_turn=int(row.get("startTurn", 0)),
                remaining_turn_permille=int(
                    row.get("remainingTurnPermil", 0)
                ),
                field_status_type=str(row.get("fieldStatusType", "")),
                field_status_value=int(row.get("fieldStatusValue", 0)),
                field_status_check_type=str(
                    row.get("fieldStatusCheckType", "")
                ),
                effect=load_plan3_effect(effect_id, database),
            )
        )
    steps.sort(key=lambda step: step.priority)
    return Plan3GimmickProfile(group_id, tuple(steps))


def load_plan3_card(
    card_id: str,
    upgrade: int = 0,
    database: Path = DEFAULT_DATABASE,
) -> Plan3Card:
    """Load one card and its ordered Master effect references."""

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM card WHERE id = ? AND upgrade_count = ?",
            (card_id, upgrade),
        ).fetchone()
        if row is None:
            raise KeyError(f"Master card not found: {card_id} +{upgrade}")
        play_effects = _json_list(
            row["play_effects_json"], f"card:{card_id}+{upgrade}:effects"
        )
        effects: list[Plan3Effect] = []
        for index, entry in enumerate(play_effects):
            if not isinstance(entry, dict):
                raise ValueError(f"card effect #{index} must be an object")
            effect_id = entry.get("produceExamEffectId")
            if not isinstance(effect_id, str) or not effect_id:
                raise ValueError(f"card effect #{index} has no effect ID")
            trigger_id = entry.get("produceExamTriggerId", "")
            if not isinstance(trigger_id, str):
                raise ValueError(f"card effect #{index} trigger ID is invalid")
            effects.append(
                _load_effect(
                    connection,
                    effect_id,
                    trigger_id=trigger_id,
                    once=bool(entry.get("isOncePlayEffect", False)),
                )
            )
        raw = json.loads(row["raw_json"])
        if not isinstance(raw, dict):
            raise ValueError(f"card:{card_id}+{upgrade}:raw_json must be an object")
        raw_effect_group_ids = raw.get("effectGroupIds", [])
        if not isinstance(raw_effect_group_ids, list) or not all(
            isinstance(value, str) and value
            for value in raw_effect_group_ids
        ):
            raise ValueError(
                f"card:{card_id}+{upgrade}:effectGroupIds must be a string array"
            )
        return Plan3Card(
            id=str(row["id"]),
            upgrade=int(row["upgrade_count"]),
            name=str(row["name"]),
            plan_type=str(row["plan_type"]),
            category=str(row["category"]),
            stamina_cost=int(row["stamina"]),
            force_stamina_cost=int(raw.get("forceStamina", 0)),
            cost_type=str(row["cost_type"]),
            cost_value=int(row["cost_value"]),
            play_trigger=_load_trigger(connection, str(row["play_trigger_id"])),
            move_position_type=str(row["move_position_type"]),
            effects=tuple(effects),
            effect_group_ids=tuple(raw_effect_group_ids),
        )


def _load_status_enchant(
    connection: sqlite3.Connection, enchant_id: str
) -> Plan3StatusEnchantRule:
    row = connection.execute(
        "SELECT * FROM produce_exam_status_enchant WHERE id = ?", (enchant_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Master status enchant not found: {enchant_id}")
    trigger = _load_trigger(connection, str(row["produce_exam_trigger_id"]))
    if trigger is None:
        raise ValueError(f"status enchant has no trigger: {enchant_id}")
    effects = tuple(
        _load_effect(connection, effect_id)
        for effect_id in _str_tuple(
            row["produce_exam_effect_ids_json"],
            f"status-enchant:{enchant_id}:effects",
        )
    )
    return Plan3StatusEnchantRule(enchant_id, trigger, effects)


def load_plan3_item(
    item_id: str, database: Path = DEFAULT_DATABASE
) -> Plan3ItemRule:
    """Load the item's persistent exam enchantments from local Master."""

    unsupported: list[str] = []
    enchantments: list[tuple[Plan3StatusEnchantRule, int]] = []
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        item = connection.execute(
            "SELECT * FROM produce_item WHERE id = ?", (item_id,)
        ).fetchone()
        if item is None:
            raise KeyError(f"Master item not found: {item_id}")
        for item_effect_id in _str_tuple(
            item["produce_item_effect_ids_json"], f"item:{item_id}:effects"
        ):
            effect = connection.execute(
                "SELECT * FROM produce_item_effect WHERE id = ?",
                (item_effect_id,),
            ).fetchone()
            if effect is None:
                raise KeyError(f"Master item effect not found: {item_effect_id}")
            effect_type = str(effect["effect_type"])
            if effect_type not in SUPPORTED_ITEM_EFFECT_TYPES:
                unsupported.append(f"item-effect-type:{effect_type}:{item_effect_id}")
                continue
            effect_turn = int(effect["effect_turn"])
            effect_count = int(effect["effect_count"])
            if effect_turn != -1:
                unsupported.append(f"item-effect-turn:{effect_turn}:{item_effect_id}")
            # Native status-enchant count zero is unlimited, not invalid.
            if effect_count < 0:
                unsupported.append(f"item-effect-count:{effect_count}:{item_effect_id}")
            if str(effect["produce_effect_id"]):
                unsupported.append(
                    f"item-produce-effect:{effect['produce_effect_id']}:{item_effect_id}"
                )
            enchant_id = str(effect["produce_exam_status_enchant_id"])
            if not enchant_id:
                unsupported.append(f"item-status-enchant-missing:{item_effect_id}")
                continue
            enchantments.append(
                (_load_status_enchant(connection, enchant_id), effect_count)
            )
        return Plan3ItemRule(
            id=str(item["id"]),
            name=str(item["name"]),
            plan_type=str(item["plan_type"]),
            fire_limit=int(item["fire_limit"]),
            fire_interval=int(item["fire_interval"]),
            enchantments=tuple(enchantments),
            unsupported_rules=_unique(unsupported),
        )


def _matches_full_power_entry_point_trigger(trigger: Plan3Trigger) -> bool:
    """Phase 39, current gauge >= a Master threshold, after entry consumes 10.

    Android field 44 dispatches to GetFullPowerPoint then signed GE. This
    accepts the data shape, not a particular item/trigger name.
    """
    return (
        trigger.phase_types == (PHASE_STANCE_CHANGE_FULL_POWER,)
        and trigger.field_types == (FIELD_FULL_POWER_POINT_UP,)
        and len(trigger.field_values) == 1
        and type(trigger.field_values[0]) is int
        and trigger.field_values[0] >= 0
        and not trigger.phase_values and not trigger.field_check_types
        and not trigger.field_card_search_ids and not trigger.produce_card_search_id
        and trigger.upper_search_count == trigger.lower_search_count == 0
        and trigger.card_move_position_type == MOVE_UNKNOWN
        and not trigger.effect_types and trigger.lesson_type == LESSON_UNKNOWN
    )


def _trigger_shape_errors(trigger: Plan3Trigger) -> tuple[str, ...]:
    errors: list[str] = []
    from .plan3_stance_change_from_full_power_trigger import (
        matches_trigger_contract as matches_full_power_exit_contract,
    )

    exact_full_power_exit = matches_full_power_exit_contract(trigger)
    from .plan3_hold_card_play_after import matches_trigger_contract as matches_hold_after
    exact_hold_after = matches_hold_after(trigger)
    from .plan3_play_history import matches_trigger_contract as matches_play_history, FIELD as HISTORY_FIELD
    exact_play_history = matches_play_history(trigger)
    from .plan3_play_count_interval_trigger import (
        matches_unfiltered_interval_trigger,
    )
    from .plan3_status_change_full_power_point_trigger import (
        matches_status_change_full_power_point_trigger,
    )
    from .plan3_fullpower_point_gimmick import matches_trigger_contract as matches_point_gimmick
    from .plan3_men100016 import matches_full_power_trigger

    exact_unfiltered_interval = matches_unfiltered_interval_trigger(trigger)
    exact_status_change_full_power_point = (
        matches_status_change_full_power_point_trigger(trigger)
        or matches_point_gimmick(trigger)
    )
    exact_men100016_full_power = matches_full_power_trigger(trigger)
    trouble_not_lost_count = (
        trigger.field_types == (FIELD_CARD_SEARCH_COUNT_UP,)
        and len(trigger.field_values) == 1
        and trigger.field_values[0] > 0
        and trigger.field_card_search_ids == (SEARCH_TROUBLE_NOT_LOST,)
        and not trigger.field_check_types
    )
    if len(trigger.phase_types) != 1:
        errors.append(f"trigger-phase-shape:{trigger.id}")
    for phase in trigger.phase_types:
        if phase not in SUPPORTED_TRIGGER_PHASE_TYPES:
            errors.append(f"trigger-phase:{phase}:{trigger.id}")
    for field in trigger.field_types:
        if field not in SUPPORTED_TRIGGER_FIELD_TYPES and not (field == HISTORY_FIELD and exact_play_history):
            errors.append(f"trigger-field:{field}:{trigger.id}")
    if trigger.field_check_types not in ((), (CHECK_NOT,)):
        errors.append(f"trigger-field-check:{trigger.id}")
    if trigger.field_check_types and len(trigger.field_types) != 1:
        errors.append(f"trigger-field-check-shape:{trigger.id}")
    if trigger.field_card_search_ids and not (
        trouble_not_lost_count or exact_full_power_exit or exact_hold_after
    ):
        errors.append(f"trigger-card-search:{trigger.id}")
    if trigger.card_move_position_type != MOVE_UNKNOWN:
        errors.append(
            f"trigger-move-position:{trigger.card_move_position_type}:{trigger.id}"
        )
    if trigger.effect_types and not exact_status_change_full_power_point:
        errors.append(f"trigger-effect-filter:{trigger.id}")
    if trigger.phase_types == (PHASE_NONE,):
        from .plan3_none_stamina_trigger import (
            matches_trigger_contract as matches_none_stamina_contract,
        )

        none_stamina = matches_none_stamina_contract(trigger)
        if trigger.produce_card_search_id:
            errors.append(f"trigger-card-search:{trigger.id}")
        if trigger.upper_search_count or trigger.lower_search_count:
            errors.append(f"trigger-search-count:{trigger.id}")
        if trigger.lesson_type != LESSON_UNKNOWN:
            errors.append(f"trigger-lesson-type:{trigger.lesson_type}:{trigger.id}")
        if trigger.phase_values:
            errors.append(f"trigger-phase-values:{trigger.id}")
        no_stance_not = (
            trigger.field_types == (FIELD_NO_STANCE,)
            and trigger.field_check_types == (CHECK_NOT,)
            and not trigger.field_values
        )
        not_full_power = (
            trigger.field_types == (FIELD_FULL_POWER_UP,)
            and trigger.field_check_types == (CHECK_NOT,)
            and not trigger.field_values
        )
        not_preservation = (
            trigger.id == TRIGGER_NONE_NOT_PRESERVATION_UP
            and trigger.field_types == (FIELD_PRESERVATION_UP,)
            and trigger.field_check_types == (CHECK_NOT,)
            and not trigger.field_values
        )
        from .plan3_counter_trigger import resolve_plan3_counter_trigger

        counter_trigger = resolve_plan3_counter_trigger(trigger).supported
        if trigger.field_check_types and not (
            no_stance_not
            or not_full_power
            or not_preservation
            or counter_trigger
            or none_stamina
        ):
            errors.append(f"trigger-none-field-check:{trigger.id}")
        if trigger.field_types == (FIELD_STANCE_CHANGE_COUNT_UP,):
            if len(trigger.field_values) != 1 or trigger.field_values[0] <= 0:
                errors.append(f"trigger-stance-count-value:{trigger.id}")
        elif trigger.field_types in (
            (FIELD_PRESERVATION_UP,),
            (FIELD_CONCENTRATION_UP,),
            (FIELD_FULL_POWER_UP,),
        ):
            exact_supernova_trigger = (
                trigger.id == "e_trigger-none-concentration_up-2"
                and trigger.field_types == (FIELD_CONCENTRATION_UP,)
                and trigger.field_values == (2,)
                and not trigger.field_check_types
            )
            if trigger.field_values and not exact_supernova_trigger:
                errors.append(f"trigger-stance-values:{trigger.id}")
        elif trigger.field_types == (FIELD_FULL_POWER_POINT_UP,):
            if len(trigger.field_values) != 1 or trigger.field_values[0] <= 0:
                errors.append(f"trigger-full-power-point-value:{trigger.id}")
        elif trouble_not_lost_count:
            pass
        elif counter_trigger:
            pass
        elif none_stamina:
            pass
        elif no_stance_not or not_full_power or not_preservation:
            pass
        else:
            errors.append(f"trigger-none-shape:{trigger.id}")
    elif trigger.phase_types == (PHASE_START_TURN,):
        from .plan3_stamina_multiple_trigger import STAMINA_MULTIPLE_CATALOG
        from .plan3_start_turn_trigger import (
            TriggerResolution as StartTurnResolution,
            matches_trigger_contract as matches_start_turn_contract,
            resolve_trigger as resolve_start_turn_trigger,
        )

        stamina_row = STAMINA_MULTIPLE_CATALOG.get(trigger.id)
        stamina = (
            stamina_row is not None
            and trigger.phase_types == stamina_row.phase_types
            and trigger.phase_values == stamina_row.phase_values
            and trigger.field_check_types == stamina_row.field_check_types
            and trigger.field_types == stamina_row.field_types
            and trigger.field_values == stamina_row.field_values
            and trigger.field_card_search_ids
            == stamina_row.field_card_search_ids
            and trigger.produce_card_search_id
            == stamina_row.produce_card_search_id
            and trigger.upper_search_count == stamina_row.upper_search_count
            and trigger.lower_search_count == stamina_row.lower_search_count
            and trigger.card_move_position_type
            == stamina_row.card_move_position_type
            and trigger.effect_types == stamina_row.effect_types
            and trigger.lesson_type == stamina_row.lesson_type
        )
        exact_start_row = resolve_start_turn_trigger(trigger.id)
        exact_start = (
            exact_start_row is not None
            and exact_start_row.resolution
            is StartTurnResolution.INDEPENDENT_RESOLVED
            and matches_start_turn_contract(exact_start_row, trigger)
        )
        plain = not trigger.field_types and not trigger.field_values
        full_power = (
            trigger.field_types == (FIELD_FULL_POWER_UP,)
            and not trigger.field_values
            and not trigger.field_check_types
        )
        not_concentration = (
            trigger.field_types == (FIELD_CONCENTRATION_UP,)
            and not trigger.field_values
            and trigger.field_check_types == (CHECK_NOT,)
        )
        no_block = (
            trigger.id == TRIGGER_NO_BLOCK
            and trigger.field_types == (FIELD_NO_BLOCK,)
            and not trigger.field_values
            and not trigger.field_check_types
        )
        total_full_power_points = (
            trigger.field_types == (FIELD_FULL_POWER_POINT_GET_SUM_UP,)
            and len(trigger.field_values) == 1
            and type(trigger.field_values[0]) is int
            and trigger.field_values[0] >= 0
            and not trigger.field_card_search_ids
        )
        if not (
            not trigger.phase_values
            and (
                plain
                or full_power
                or not_concentration
                or no_block
                or stamina
                or exact_start
                or total_full_power_points
            )
            and not trigger.produce_card_search_id
            and trigger.upper_search_count == 0
            and trigger.lower_search_count == 0
            and trigger.lesson_type in LESSON_TYPE_VALUES
        ):
            errors.append(f"trigger-start-turn-shape:{trigger.id}")
    elif trigger.phase_types == (PHASE_START_PLAY,):
        neutral = (
            not trigger.phase_values and not trigger.field_types
            and not trigger.field_values and not trigger.field_check_types
            and not trigger.field_card_search_ids and not trigger.produce_card_search_id
            and trigger.upper_search_count == trigger.lower_search_count == 0
            and trigger.card_move_position_type == MOVE_UNKNOWN
            and not trigger.effect_types and trigger.lesson_type == LESSON_UNKNOWN
        )
        preservation = (
            trigger.id == TRIGGER_START_PLAY_PRESERVATION_UP
            and trigger.field_types == (FIELD_PRESERVATION_UP,)
            and not trigger.field_values
            and not trigger.field_check_types
        )
        if not (
            not trigger.phase_values
            and (
                (
                    trigger.field_types == (FIELD_FULL_POWER_UP,)
                    and not trigger.field_values
                    and not trigger.field_check_types
                )
                or preservation
                or exact_play_history
                or neutral
            )
            and not trigger.produce_card_search_id
            and trigger.upper_search_count == 0
            and trigger.lower_search_count == 0
            and trigger.lesson_type == LESSON_UNKNOWN
        ):
            errors.append(f"trigger-start-play-shape:{trigger.id}")
    elif trigger.phase_types == (PHASE_TURN_TIMER,):
        if not (
            len(trigger.phase_values) == 1
            and trigger.phase_values[0] > 0
            and not trigger.field_types
            and not trigger.field_values
            and not trigger.field_check_types
            and not trigger.produce_card_search_id
            and trigger.upper_search_count == 0
            and trigger.lower_search_count == 0
            and trigger.lesson_type == LESSON_UNKNOWN
        ):
            errors.append(f"trigger-turn-timer-shape:{trigger.id}")
    elif trigger.phase_types == (PHASE_CARD_PLAY,):
        from .plan3_stamina_multiple_trigger import STAMINA_MULTIPLE_CATALOG

        stamina_row = STAMINA_MULTIPLE_CATALOG.get(trigger.id)
        stamina = (
            stamina_row is not None
            and stamina_row.predicate_executable
            and trigger.phase_types == stamina_row.phase_types
            and trigger.phase_values == stamina_row.phase_values
            and trigger.field_check_types == stamina_row.field_check_types
            and trigger.field_types == stamina_row.field_types
            and trigger.field_values == stamina_row.field_values
            and trigger.field_card_search_ids
            == stamina_row.field_card_search_ids
            and trigger.produce_card_search_id
            == stamina_row.produce_card_search_id
            and trigger.upper_search_count == stamina_row.upper_search_count
            and trigger.lower_search_count == stamina_row.lower_search_count
            and trigger.card_move_position_type
            == stamina_row.card_move_position_type
            and trigger.effect_types == stamina_row.effect_types
            and trigger.lesson_type == stamina_row.lesson_type
        )
        full_power = (
            trigger.field_types == (FIELD_FULL_POWER_UP,)
            and not trigger.field_values
            and not trigger.field_check_types
            and not trigger.produce_card_search_id
            and trigger.lower_search_count == 1
        )
        mental_not_full_power = (
            trigger.field_types == (FIELD_FULL_POWER_UP,)
            and not trigger.field_values
            and trigger.field_check_types == (CHECK_NOT,)
            and trigger.produce_card_search_id == SEARCH_MENTAL_SKILL_PLAYING
            and trigger.lower_search_count == 1
        )
        played_full_power_effect = (
            not trigger.field_types
            and not trigger.field_values
            and not trigger.field_check_types
            and trigger.produce_card_search_id
            == SEARCH_PLAYING_FULL_POWER_EFFECT
            and trigger.lower_search_count == 1
        )
        mental_playing_concentration = (
            trigger.field_types == (FIELD_CONCENTRATION_UP,)
            and trigger.field_values == (2,)
            and not trigger.field_check_types
            and trigger.produce_card_search_id == SEARCH_MENTAL_SKILL_PLAYING
            and trigger.lower_search_count == 1
        )
        played_concentration_effect = (
            not trigger.field_types
            and not trigger.field_values
            and not trigger.field_check_types
            and trigger.produce_card_search_id
            == SEARCH_PLAYING_CONCENTRATION_EFFECT
            and trigger.lower_search_count == 1
        )
        if not (
            not trigger.phase_values
            and (
                full_power
                or mental_not_full_power
                or played_full_power_effect
                or mental_playing_concentration
                or played_concentration_effect
                or stamina
            )
            and trigger.upper_search_count == 0
            and trigger.lesson_type == LESSON_UNKNOWN
        ):
            errors.append(f"trigger-card-play-shape:{trigger.id}")
    elif trigger.phase_types == (PHASE_STANCE_CHANGE_CONCENTRATION,):
        plain = (
            not trigger.phase_values
            and not trigger.field_types
            and not trigger.field_values
            and not trigger.produce_card_search_id
            and trigger.upper_search_count == 0
            and trigger.lower_search_count == 0
            and trigger.lesson_type == LESSON_UNKNOWN
        )
        dance_no_block = (
            not trigger.phase_values
            and trigger.field_types == (FIELD_NO_BLOCK,)
            and not trigger.field_values
            and not trigger.produce_card_search_id
            and trigger.upper_search_count == 0
            and trigger.lower_search_count == 0
            and trigger.lesson_type == LESSON_DANCE
        )
        if not (plain or dance_no_block):
            errors.append(f"trigger-stance-change-shape:{trigger.id}")
    elif trigger.phase_types == (PHASE_STANCE_CHANGE_FROM_FULL_POWER,):
        # Phase 47 is selected by the completed difference's old FullPower
        # stance.  Admit only the five complete Master contracts audited by
        # the independent scheduler; lookalikes remain fail-closed.
        if not exact_full_power_exit:
            errors.append(
                f"trigger-stance-change-from-full-power-shape:{trigger.id}"
            )
    elif trigger.phase_types == (PHASE_STANCE_CHANGE_FULL_POWER,):
        # Both gates read the committed state at the same phase boundary.
        if not (exact_men100016_full_power or _matches_full_power_entry_point_trigger(trigger)):
            errors.append(
                f"trigger-stance-change-full-power-shape:{trigger.id}"
            )
    elif trigger.phase_types == (PHASE_CARD_PLAY_AFTER,):
        concentration_active = (
            trigger.field_types == (FIELD_CONCENTRATION_UP,)
            and len(trigger.field_values) <= 1
            and (
                not trigger.field_values or trigger.field_values[0] > 0
            )
            and not trigger.field_check_types
            and trigger.produce_card_search_id == SEARCH_ACTIVE_SKILL_TARGET
        )
        preservation_mental = (
            trigger.field_types == (FIELD_PRESERVATION_UP,)
            and len(trigger.field_values) <= 1
            and (
                not trigger.field_values or trigger.field_values[0] > 0
            )
            and not trigger.field_check_types
            and trigger.produce_card_search_id == SEARCH_MENTAL_SKILL_TARGET
        )
        target_concentration_effect = (
            trigger.id == TRIGGER_CARD_PLAY_AFTER_TARGET_CONCENTRATION
            and not trigger.field_types
            and not trigger.field_values
            and not trigger.field_check_types
            and trigger.produce_card_search_id
            == SEARCH_TARGET_CONCENTRATION_EFFECT
        )
        if not (
            not trigger.phase_values
            and (
                concentration_active
                or preservation_mental
                or target_concentration_effect
                or exact_hold_after
            )
            and trigger.upper_search_count == 0
            and trigger.lower_search_count == 1
            and trigger.lesson_type == LESSON_UNKNOWN
        ):
            errors.append(f"trigger-card-play-after-shape:{trigger.id}")
    elif trigger.phase_types == (PHASE_PLAY_COUNT_INTERVAL,):
        if not exact_unfiltered_interval:
            errors.append(f"trigger-play-count-interval-shape:{trigger.id}")
    elif trigger.phase_types == (PHASE_STATUS_CHANGE,):
        if not exact_status_change_full_power_point:
            errors.append(f"trigger-status-change-shape:{trigger.id}")
    elif trigger.phase_types == (PHASE_END_TURN,):
        full_power = (
            trigger.field_types == (FIELD_FULL_POWER_UP,)
            and not trigger.field_values
            and not trigger.field_check_types
        )
        not_full_power_points = (
            trigger.field_types == (FIELD_FULL_POWER_POINT_UP,)
            and len(trigger.field_values) == 1
            and trigger.field_values[0] > 0
            and trigger.field_check_types == (CHECK_NOT,)
        )
        exact_encore_remaining_turn = (
            trigger.id == EXACT_ENCORE_TRIGGER_ID
            and trigger.field_types == (FIELD_REMAINING_TURN,)
            and trigger.field_values == (3,)
            and not trigger.field_check_types
        )
        if not (
            not trigger.phase_values
            and (
                full_power
                or not_full_power_points
                or exact_encore_remaining_turn
            )
            and not trigger.produce_card_search_id
            and trigger.upper_search_count == 0
            and trigger.lower_search_count == 0
            and trigger.lesson_type == LESSON_UNKNOWN
        ):
            errors.append(f"trigger-end-turn-shape:{trigger.id}")
    else:
        errors.append(f"trigger-phase-shape:{trigger.id}")
    return _unique(errors)


def _trigger_field_matches(
    trigger: Plan3Trigger,
    state: Plan3State,
    card_search_counts: Mapping[str, int] | None = None,
) -> bool:
    if not trigger.field_types:
        matches = True
    else:
        field = trigger.field_types[0]
        if field == FIELD_STANCE_CHANGE_COUNT_UP:
            matches = state.stance_change_count >= trigger.field_values[0]
        elif field == FIELD_PRESERVATION_UP:
            matches = state.stance == STANCE_PRESERVATION and (
                not trigger.field_values
                or state.stance_level >= trigger.field_values[0]
            )
        elif field == FIELD_CONCENTRATION_UP:
            matches = state.stance == STANCE_CONCENTRATION and (
                not trigger.field_values
                or state.stance_level >= trigger.field_values[0]
            )
        elif field == FIELD_FULL_POWER_UP:
            matches = state.stance == STANCE_FULL_POWER
        elif field == FIELD_FULL_POWER_POINT_UP:
            matches = state.full_power_points >= trigger.field_values[0]
        elif field == FIELD_FULL_POWER_POINT_GET_SUM_UP:
            # Native cumulative acquisition count remains after spending points
            # or entering FullPower. Never substitute the current point balance.
            matches = state.full_power_points_total >= trigger.field_values[0]
        elif field == FIELD_NO_BLOCK:
            matches = state.block == 0
        elif field == FIELD_NO_STANCE:
            matches = state.stance == STANCE_NEUTRAL
        elif field == FIELD_CARD_SEARCH_COUNT_UP:
            assert card_search_counts is not None
            search_id = trigger.field_card_search_ids[0]
            matches = (
                card_search_counts[search_id] >= trigger.field_values[0]
            )
        elif field == FIELD_REMAINING_TURN:
            matches = state.turns_remaining <= trigger.field_values[0]
        else:
            return False
    if trigger.field_check_types == (CHECK_NOT,):
        return not matches
    return matches


def evaluate_plan3_trigger(
    trigger: Plan3Trigger,
    state: Plan3State,
    *,
    event_phase: str | None = None,
    event_card: Plan3Card | None = None,
    card_search_counts: Mapping[str, int] | None = None,
    turn_timer_count: int | None = None,
    phase_count: int | None = None,
    event_effect_type: str | None = None,
    effect_result_trigger_enabled: bool = True,
) -> TriggerDecision:
    """Evaluate either a card eligibility trigger or one runtime phase."""

    errors = _trigger_shape_errors(trigger)
    if errors:
        return TriggerDecision(False, False, errors)
    from .plan3_play_history import matches_trigger_contract as matches_play_history, PHASE as HISTORY_PHASE
    if matches_play_history(trigger):
        if event_phase != HISTORY_PHASE:
            return TriggerDecision(True, False)
        if state.play_history is None:
            return TriggerDecision(False, False, (f"trigger-play-history-unavailable:{trigger.id}",))
        return TriggerDecision(True, state.play_history.play_card_lesson())
    if not isinstance(effect_result_trigger_enabled, bool):
        return TriggerDecision(
            False,
            False,
            (f"trigger-effect-source-gate-invalid:{trigger.id}",),
        )
    if trigger.phase_types == (PHASE_PLAY_COUNT_INTERVAL,):
        if (
            phase_count is None
            or not isinstance(phase_count, int)
            or isinstance(phase_count, bool)
            or phase_count < 0
        ):
            return TriggerDecision(
                False,
                False,
                (f"trigger-phase-count-unavailable:{trigger.id}",),
            )
        interval = trigger.phase_values[0]
        return TriggerDecision(
            True,
            event_phase == PHASE_PLAY_COUNT_INTERVAL
            and phase_count >= 1
            and phase_count % interval == 0,
        )
    if trigger.phase_types == (PHASE_STATUS_CHANGE,):
        return TriggerDecision(
            True,
            effect_result_trigger_enabled
            and event_phase == PHASE_STATUS_CHANGE
            and event_effect_type == EFFECT_FULL_POWER_POINT
            and _trigger_field_matches(trigger, state),
        )
    if trigger.field_types in {
        (FIELD_STAMINA_UP_MULTIPLE,),
        (FIELD_STAMINA_LESS_MULTIPLE,),
    }:
        if trigger.phase_types == (PHASE_NONE,):
            from .plan3_none_stamina_trigger import (
                evaluate as evaluate_none_stamina,
            )
            from .plan3_stamina_multiple_trigger import StaminaSnapshot

            effective_phase = PHASE_NONE if event_phase is None else event_phase
            if effective_phase != PHASE_NONE:
                return TriggerDecision(True, False)
            if state.max_stamina is None:
                return TriggerDecision(
                    False, False, ("state:max-stamina-unknown",)
                )
            formula = evaluate_none_stamina(
                trigger,
                StaminaSnapshot(state.stamina, state.max_stamina),
                event_phase=effective_phase,
            )
            if formula is None or formula.fires is None:
                reasons = (
                    ("exact-none-stamina-contract-mismatch",)
                    if formula is None
                    else formula.reasons
                )
                return TriggerDecision(
                    False,
                    False,
                    tuple(
                        f"trigger-stamina:{reason}:{trigger.id}"
                        for reason in reasons
                    ),
                )
            return TriggerDecision(True, formula.fires)

        from .plan3_stamina_multiple_trigger import (
            STAMINA_MULTIPLE_CATALOG,
            StaminaSnapshot,
            evaluate_stamina_multiple,
        )

        row = STAMINA_MULTIPLE_CATALOG.get(trigger.id)
        assert row is not None
        if row.produce_card_search_id or row.field_card_search_ids:
            return TriggerDecision(
                False,
                False,
                (f"trigger-stamina-search-wrapper-unprojected:{trigger.id}",),
            )
        if event_phase != row.phase:
            return TriggerDecision(True, False)
        if state.max_stamina is None:
            return TriggerDecision(
                False, False, ("state:max-stamina-unknown",)
            )
        evaluation = evaluate_stamina_multiple(
            row.field,
            row.threshold,
            StaminaSnapshot(state.stamina, state.max_stamina),
        )
        if evaluation.fires is None:
            return TriggerDecision(
                False,
                False,
                tuple(
                    f"trigger-stamina:{reason}:{trigger.id}"
                    for reason in evaluation.reasons
                ),
            )
        fires = evaluation.fires
        lesson_type_value = LESSON_TYPE_VALUES.get(trigger.lesson_type)
        if lesson_type_value is None:
            return TriggerDecision(
                False,
                False,
                (f"trigger-lesson-type:{trigger.lesson_type}:{trigger.id}",),
            )
        if fires and lesson_type_value != 0:
            if state.is_battle:
                fires = android_v323_lesson_is_battle_turn_valid(
                    lesson_type_value,
                    state.current_parameter_type,
                )
            elif state.step_type_value == 0:
                return TriggerDecision(
                    False, False, ("state:step-type-unknown",)
                )
            else:
                fires = android_v323_lesson_runtime_field_valid(
                    lesson_type_value,
                    state.step_type_value,
                    is_clear=state.is_clear,
                )
        return TriggerDecision(True, bool(fires))
    if trigger.phase_types == (PHASE_NONE,) and trigger.field_types in {
        (FIELD_CONCENTRATION_CHANGE_COUNT_UP,),
        (FIELD_PRESERVATION_CHANGE_COUNT_UP,),
        (FIELD_FULL_POWER_CHANGE_COUNT_UP,),
        (FIELD_FULL_POWER_POINT_GET_SUM_UP,),
    }:
        from .plan3_counter_trigger import (
            PHASE_NONE as COUNTER_PHASE_NONE,
            Plan3CounterState,
            evaluate_plan3_counter_trigger,
        )

        effective_phase = (
            COUNTER_PHASE_NONE if event_phase is None else event_phase
        )
        if effective_phase != COUNTER_PHASE_NONE:
            return TriggerDecision(True, False)
        decision = evaluate_plan3_counter_trigger(
            trigger,
            Plan3CounterState.from_plan3_state(state),
            event_phase=effective_phase,
        )
        if not decision.supported or decision.fires is None:
            return TriggerDecision(
                False, False, decision.unresolved
            )
        return TriggerDecision(True, decision.fires)
    if trigger.phase_types == (PHASE_STANCE_CHANGE_FROM_FULL_POWER,):
        from .plan3_stance_change_from_full_power_trigger import (
            NativeStance,
            PostCommitState,
            SEARCH_LOST_IDOL_CARD,
            StanceDifference,
            TRIGGER_BY_ID as FULL_POWER_EXIT_TRIGGER_BY_ID,
            evaluate_trigger as evaluate_full_power_exit_trigger,
        )

        if event_phase != PHASE_STANCE_CHANGE_FROM_FULL_POWER:
            return TriggerDecision(True, False)
        native_stance = {
            STANCE_NEUTRAL: NativeStance.UNKNOWN,
            STANCE_CONCENTRATION: NativeStance.CONCENTRATION,
            STANCE_PRESERVATION: (
                NativeStance.OVER_PRESERVATION
                if state.stance_level == 3
                else NativeStance.PRESERVATION
            ),
            STANCE_FULL_POWER: NativeStance.FULL_POWER,
        }[state.stance]
        if card_search_counts is None:
            phase_search_counts: Mapping[str, int] = {
                SEARCH_LOST_IDOL_CARD: sum(
                    card.card_id == "p_card-03-ido-3_144"
                    for card in state.lost_pile
                )
            }
        else:
            phase_search_counts = card_search_counts
        evaluation = evaluate_full_power_exit_trigger(
            FULL_POWER_EXIT_TRIGGER_BY_ID[trigger.id],
            PostCommitState(
                native_stance,
                state.full_power_points,
                phase_search_counts,
            ),
            StanceDifference.full_power_expiry(
                before_step=1,
                stance_change_count=state.stance_change_count,
                full_power_points=state.full_power_points,
            ),
            event_phase=event_phase,
        )
        if evaluation.fires is None:
            return TriggerDecision(
                False,
                False,
                tuple(
                    f"trigger-stance-change-from-full-power:{reason}:{trigger.id}"
                    for reason in evaluation.reasons
                ),
            )
        return TriggerDecision(True, evaluation.fires)
    if trigger.phase_types == (PHASE_STANCE_CHANGE_FULL_POWER,):
        if _matches_full_power_entry_point_trigger(trigger):
            return TriggerDecision(True, event_phase == PHASE_STANCE_CHANGE_FULL_POWER
                and state.full_power_points >= trigger.field_values[0])
        from .plan3_men100016 import (
            matches_full_power_trigger,
            signed_remaining_turn_gate,
        )

        if not matches_full_power_trigger(trigger):
            return TriggerDecision(
                False,
                False,
                (f"trigger-stance-change-full-power-shape:{trigger.id}",),
            )
        return TriggerDecision(
            True,
            event_phase == PHASE_STANCE_CHANGE_FULL_POWER
            and signed_remaining_turn_gate(state.turns_remaining),
        )
    from .plan3_hold_card_play_after import matches_trigger_contract as matches_hold_after, fires as fires_hold_after
    if matches_hold_after(trigger):
        fires = fires_hold_after(trigger, state, event_phase=event_phase, event_card=event_card)
        return TriggerDecision(fires is not None, bool(fires),
            () if fires is not None else (f"trigger-hold-count-unavailable:{trigger.id}",))
    if trigger.field_types == (FIELD_CARD_SEARCH_COUNT_UP,):
        search_id = trigger.field_card_search_ids[0]
        if card_search_counts is None or search_id not in card_search_counts:
            return TriggerDecision(
                False,
                False,
                (f"trigger-card-search-count-unavailable:{trigger.id}",),
            )
        count = card_search_counts[search_id]
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            return TriggerDecision(
                False,
                False,
                (f"trigger-card-search-count-invalid:{trigger.id}",),
            )
    if trigger.phase_types == (PHASE_STANCE_CHANGE_CONCENTRATION,):
        fires = event_phase == PHASE_STANCE_CHANGE_CONCENTRATION
        if trigger.field_types == (FIELD_NO_BLOCK,):
            if state.is_battle:
                lesson_valid = android_v323_lesson_is_battle_turn_valid(
                    LESSON_TYPE_VALUES[trigger.lesson_type],
                    state.current_parameter_type,
                )
            else:
                lesson_valid = state.lesson_type == trigger.lesson_type
            fires = fires and state.block == 0 and lesson_valid
        return TriggerDecision(True, fires)
    if trigger.phase_types == (PHASE_START_TURN,):
        from .plan3_start_turn_trigger import (
            TriggerResolution as StartTurnResolution,
            fires as evaluate_start_turn_trigger,
            matches_trigger_contract as matches_start_turn_contract,
            resolve_trigger as resolve_start_turn_trigger,
        )

        exact_start_row = resolve_start_turn_trigger(trigger.id)
        if (
            exact_start_row is not None
            and exact_start_row.resolution
            is StartTurnResolution.INDEPENDENT_RESOLVED
            and matches_start_turn_contract(exact_start_row, trigger)
        ):
            exact_decision = evaluate_start_turn_trigger(
                exact_start_row,
                state,
                event_phase=(event_phase or ""),
            )
            if exact_decision.fires is None:
                return TriggerDecision(
                    False,
                    False,
                    tuple(
                        f"trigger-start-turn:{reason}:{trigger.id}"
                        for reason in exact_decision.reasons
                    ),
                )
            fires = exact_decision.fires
        else:
            fires = (
                event_phase == PHASE_START_TURN
                and _trigger_field_matches(trigger, state)
            )
        lesson_type_value = LESSON_TYPE_VALUES.get(trigger.lesson_type)
        if lesson_type_value is None:
            return TriggerDecision(
                False,
                False,
                (f"trigger-lesson-type:{trigger.lesson_type}:{trigger.id}",),
            )
        if lesson_type_value != 0:
            if state.is_battle:
                fires = fires and android_v323_lesson_is_battle_turn_valid(
                    lesson_type_value,
                    state.current_parameter_type,
                )
            elif state.step_type_value == 0:
                return TriggerDecision(
                    False, False, ("state:step-type-unknown",)
                )
            else:
                fires = fires and android_v323_lesson_runtime_field_valid(
                    lesson_type_value,
                    state.step_type_value,
                    is_clear=state.is_clear,
                )
        return TriggerDecision(True, fires)
    if trigger.phase_types == (PHASE_TURN_TIMER,):
        if turn_timer_count is not None and (
            not isinstance(turn_timer_count, int)
            or isinstance(turn_timer_count, bool)
            or turn_timer_count < 0
        ):
            return TriggerDecision(
                False,
                False,
                (f"trigger-turn-timer-count:{trigger.id}",),
            )
        timer_count = (
            state.round_number
            if turn_timer_count is None
            else turn_timer_count
        )
        return TriggerDecision(
            True,
            event_phase == PHASE_TURN_TIMER
            and timer_count in trigger.phase_values,
        )
    phase = trigger.phase_types[0]
    if phase == PHASE_NONE:
        return TriggerDecision(
            True,
            _trigger_field_matches(trigger, state, card_search_counts),
        )
    search_matches = True
    if trigger.produce_card_search_id in (
        SEARCH_ACTIVE_SKILL_TARGET,
    ):
        search_matches = (
            event_card is not None and event_card.category == CATEGORY_ACTIVE
        )
    elif trigger.produce_card_search_id in (
        SEARCH_MENTAL_SKILL_TARGET,
        SEARCH_MENTAL_SKILL_PLAYING,
    ):
        search_matches = (
            event_card is not None and event_card.category == CATEGORY_MENTAL
        )
    elif trigger.produce_card_search_id == SEARCH_PLAYING_FULL_POWER_EFFECT:
        search_matches = event_card is not None and any(
            effect.effect_type
            in {
                EFFECT_FULL_POWER_POINT,
                EFFECT_FULL_POWER_POINT_REDUCE,
            }
            for effect in event_card.effects
        )
    elif trigger.produce_card_search_id == SEARCH_PLAYING_CONCENTRATION_EFFECT:
        search_matches = (
            event_card is not None
            and EFFECT_GROUP_CONCENTRATION in event_card.effect_group_ids
        )
    elif trigger.produce_card_search_id == SEARCH_TARGET_CONCENTRATION_EFFECT:
        search_matches = (
            event_card is not None
            and EFFECT_GROUP_CONCENTRATION in event_card.effect_group_ids
        )
    if phase in (PHASE_CARD_PLAY, PHASE_CARD_PLAY_AFTER) and event_card is None:
        return TriggerDecision(True, False)
    return TriggerDecision(
        True,
        event_phase == phase
        and _trigger_field_matches(trigger, state, card_search_counts)
        and search_matches,
    )


def _exact_encore_rule_errors(rule: Plan3StatusEnchantRule) -> tuple[str, ...]:
    """Validate only the 197 Encore listener chain proved on Android v3.2.3."""

    trigger = rule.trigger
    if not (
        rule.id == EXACT_ENCORE_STATUS_ID
        and trigger.id == EXACT_ENCORE_TRIGGER_ID
        and trigger.phase_types == (PHASE_END_TURN,)
        and not trigger.phase_values
        and not trigger.field_check_types
        and trigger.field_types == (FIELD_REMAINING_TURN,)
        and trigger.field_values == (3,)
        and not trigger.field_card_search_ids
        and not trigger.produce_card_search_id
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type == MOVE_UNKNOWN
        and not trigger.effect_types
        and trigger.lesson_type == LESSON_UNKNOWN
    ):
        return (f"encore-trigger-shape:{rule.id}",)
    if len(rule.effects) != 1:
        return (f"encore-effect-count:{rule.id}",)
    nested = rule.effects[0]
    if not (
        nested.id == EXACT_ENCORE_FORCE_PLAY_EFFECT_ID
        and nested.effect_type == EFFECT_FORCE_PLAY_CARD_SEARCH
        and nested.value1 == 0
        and nested.value2 == 0
        and nested.effect_count == 0
        and nested.effect_turn == 0
        and not nested.status_enchant_id
        and nested.status_enchant is None
        and not nested.chain_effect_id
        and not nested.chain_effect_ids
        and nested.chain_effect is None
        and nested.trigger is None
        and not nested.once
        and nested.card_move_rule is None
    ):
        return (f"encore-force-play-shape:{rule.id}",)
    return ()


def _exact_encore_effect_errors(effect: Plan3Effect) -> tuple[str, ...]:
    rule = effect.status_enchant
    if not (
        effect.id == EXACT_ENCORE_EFFECT_ID
        and effect.effect_type == EFFECT_STATUS_ENCHANT_ENCORE
        and effect.value1 == 1
        and effect.value2 == 0
        and effect.effect_count == 3
        and effect.effect_turn == -1
        and effect.status_enchant_id == EXACT_ENCORE_STATUS_ID
        and rule is not None
        and not effect.chain_effect_id
        and not effect.chain_effect_ids
        and effect.chain_effect is None
        and effect.trigger is None
        and effect.card_move_rule is None
    ):
        return (f"effect-encore-shape:{effect.id}",)
    return _exact_encore_rule_errors(rule)


def _effect_shape_errors(effect: Plan3Effect) -> tuple[str, ...]:
    errors: list[str] = []
    if effect.effect_type not in SUPPORTED_EFFECT_TYPES:
        errors.append(f"effect-type:{effect.effect_type}:{effect.id}")
    if effect.effect_type == EFFECT_STATUS_ENCHANT:
        if not effect.status_enchant_id:
            errors.append(f"effect-status-enchant-missing:{effect.id}")
        if effect.status_enchant is None:
            errors.append(f"effect-status-enchant-unresolved:{effect.id}")
        elif effect.status_enchant.id != effect.status_enchant_id:
            errors.append(f"effect-status-enchant-mismatch:{effect.id}")
        else:
            errors.extend(_status_rule_shape_errors(effect.status_enchant))
            from .plan3_play_count_interval_trigger import (
                STATUS_ENCHANT_ID as INTERVAL_FIVE_STATUS_ID,
                matches_interval_five_installer,
            )
            from .plan3_status_change_full_power_point_trigger import (
                STATUS_ENCHANT_ID as STATUS_CHANGE_FULL_POWER_POINT_STATUS_ID,
                matches_status_change_full_power_point_installer,
            )
            from .plan3_men100016 import (
                STATUS_ENCHANT_ID as MEN100016_STATUS_ID,
                matches_installer as matches_men100016_installer,
            )
            from .plan3_use_pool import (
                WITH_COST_STATUS_ID,
                matches_with_cost_installer,
            )

            if (
                effect.status_enchant.id == INTERVAL_FIVE_STATUS_ID
                and not matches_interval_five_installer(effect)
            ):
                errors.append(f"effect-interval-five-installer-shape:{effect.id}")
            if (
                effect.status_enchant.id
                == STATUS_CHANGE_FULL_POWER_POINT_STATUS_ID
                and not matches_status_change_full_power_point_installer(effect)
            ):
                errors.append(
                    f"effect-status-change-full-power-point-installer-shape:{effect.id}"
                )
            if (
                effect.status_enchant.id == MEN100016_STATUS_ID
                and not matches_men100016_installer(effect)
            ):
                errors.append(f"effect-men100016-installer-shape:{effect.id}")
            if (
                effect.status_enchant.id == WITH_COST_STATUS_ID
                and not matches_with_cost_installer(effect)
            ):
                errors.append(
                    f"effect-force-play-with-cost-installer-shape:{effect.id}"
                )
    elif effect.effect_type == EFFECT_STATUS_ENCHANT_ENCORE:
        errors.extend(_exact_encore_effect_errors(effect))
    elif effect.status_enchant_id or effect.status_enchant is not None:
        errors.append(f"effect-status-enchant-unexpected:{effect.id}")
    if (
        effect.effect_type not in {EFFECT_CARD_MOVE, EFFECT_ADD_GROW}
        and effect.card_move_rule is not None
    ):
        errors.append(f"effect-card-move-unexpected:{effect.id}")
    if effect.effect_type != EFFECT_TIMER and (
        effect.chain_effect_id
        or effect.chain_effect_ids
        or effect.chain_effect is not None
    ):
        chain_id = effect.chain_effect_id or ",".join(effect.chain_effect_ids)
        errors.append(f"effect-chain:{chain_id}:{effect.id}")
    if effect.trigger is not None:
        errors.extend(_trigger_shape_errors(effect.trigger))
    if effect.effect_type in (EFFECT_PRESERVATION, EFFECT_CONCENTRATION):
        maximum = 3 if effect.effect_type == EFFECT_PRESERVATION else 2
        if effect.value1 < 1 or effect.value1 > maximum:
            errors.append(f"effect-stance-level:{effect.value1}:{effect.id}")
        if effect.value2 or effect.effect_count or effect.effect_turn:
            errors.append(f"effect-stance-shape:{effect.id}")
    elif effect.effect_type == EFFECT_MULTIPLE_ENTHUSIASTIC_LESSON:
        try:
            from .plan3_encore_enthusiastic_lesson import (
                resolve_multiple_enthusiastic_lesson,
            )

            resolve_multiple_enthusiastic_lesson(effect)
        except (TypeError, ValueError) as error:
            errors.append(
                f"effect-multiple-enthusiastic-lesson-shape:{effect.id}:{error}"
            )
    elif effect.effect_type == EFFECT_LESSON:
        if effect.value1 < 0 or effect.value2 or effect.effect_count <= 0 or effect.effect_turn:
            errors.append(f"effect-lesson-shape:{effect.id}")
    elif effect.effect_type == EFFECT_LESSON_FULL_POWER_POINT:
        if (
            effect.value1 < 0
            or effect.value2 <= 0
            or effect.effect_count <= 0
            or effect.effect_turn
        ):
            errors.append(f"effect-lesson-full-power-point-shape:{effect.id}")
    elif effect.effect_type in (EFFECT_BLOCK, EFFECT_BLOCK_FIX):
        if effect.value1 < 0 or effect.value2 or effect.effect_count or effect.effect_turn:
            errors.append(f"effect-block-shape:{effect.id}")
    elif effect.effect_type == EFFECT_BLOCK_RESTRICTION:
        from .plan3_block_restriction import (
            EffectContext as BlockRestrictionContext,
            matches_effect_contract as matches_block_restriction,
        )

        if not (
            matches_block_restriction(
                effect, BlockRestrictionContext.DIRECT_CARD
            )
            or matches_block_restriction(
                effect, BlockRestrictionContext.TURN_START_GIMMICK
            )
        ):
            errors.append(f"effect-block-restriction-shape:{effect.id}")
    elif effect.effect_type in (EFFECT_STAMINA_RECOVER_FIX, EFFECT_STAMINA_REDUCE_FIX):
        if effect.value1 <= 0 or effect.value2 or effect.effect_count or effect.effect_turn:
            errors.append(f"effect-stamina-fix-shape:{effect.id}")
    elif effect.effect_type == EFFECT_STAMINA_RECOVER_MULTIPLE:
        from .exam_stamina_recover_multiple import (
            MultipleEffectContract,
            ReviewStaminaContractError,
        )

        try:
            MultipleEffectContract(
                effect_id=effect.id,
                effect_value1=effect.value1,
                effect_value2=effect.value2,
                effect_count=effect.effect_count,
                effect_turn=effect.effect_turn,
                status_enchant_id=effect.status_enchant_id,
                chain_effect_id=effect.chain_effect_id,
            )
        except (TypeError, ValueError, ReviewStaminaContractError):
            errors.append(f"effect-stamina-recover-multiple-shape:{effect.id}")
    elif effect.effect_type == EFFECT_STAMINA_REDUCE:
        if not (
            effect.id == "e_effect-exam_stamina_reduce-1000"
            and effect.value1 == 1000
            and effect.value2 == effect.effect_count == effect.effect_turn == 0
        ):
            errors.append(f"effect-stamina-reduce-shape:{effect.id}")
    elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN:
        if effect.value1 or effect.value2 or effect.effect_count or effect.effect_turn <= 0:
            errors.append(f"effect-stamina-consumption-down-shape:{effect.id}")
    elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN_FIX:
        if (
            effect.value1 <= 0
            or effect.value2
            or effect.effect_count
            or (effect.effect_turn != -1 and effect.effect_turn <= 0)
        ):
            errors.append(
                f"effect-stamina-consumption-down-fix-shape:{effect.id}"
            )
    elif effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
        if effect.value1 or effect.value2 or effect.effect_count <= 0 or effect.effect_turn:
            errors.append(f"effect-playable-value-add-shape:{effect.id}")
    elif effect.effect_type == EFFECT_CARD_DRAW:
        if effect.value1 <= 0 or effect.value2 or effect.effect_count or effect.effect_turn:
            errors.append(f"effect-card-draw-shape:{effect.id}")
    elif effect.effect_type == EFFECT_ENTHUSIASTIC_ADDITIVE:
        finite_target = (
            effect.id in _FINITE_ENTHUSIASM_ADDITIVE_IDS
            and effect.effect_turn == 2
        )
        if (
            effect.value1 < 0
            or effect.value2
            or effect.effect_count
            or (effect.effect_turn != -1 and not finite_target)
        ):
            errors.append(f"effect-enthusiasm-additive-shape:{effect.id}")
    elif effect.effect_type == EFFECT_ENTHUSIASTIC_MULTIPLE:
        if (
            effect.value1 <= 0
            or effect.value2
            or effect.effect_count
            or (effect.effect_turn != -1 and effect.effect_turn <= 0)
        ):
            errors.append(f"effect-enthusiasm-multiple-shape:{effect.id}")
    elif effect.effect_type in (
        EFFECT_FULL_POWER_POINT,
        EFFECT_FULL_POWER_POINT_REDUCE,
    ):
        if effect.value1 <= 0 or effect.value2 or effect.effect_count or effect.effect_turn:
            errors.append(f"effect-full-power-point-shape:{effect.id}")
    elif effect.effect_type == EFFECT_FULL_POWER_POINT_ADDITIVE:
        if not (
            effect.id in _FULL_POWER_POINT_ADDITIVE_TURNS_BY_ID
            and effect.value1 == 500
            and effect.value2 == 0
            and effect.effect_count == 0
            and effect.effect_turn
            == _FULL_POWER_POINT_ADDITIVE_TURNS_BY_ID[effect.id]
        ):
            errors.append(
                f"effect-full-power-point-additive-shape:{effect.id}"
            )
    elif effect.effect_type == EFFECT_CARD_CREATE_SEARCH:
        if (
            effect.value1 < 0
            or effect.value2
            or effect.effect_count
            or effect.effect_turn
        ):
            errors.append(f"effect-card-create-search-shape:{effect.id}")
    elif effect.effect_type == EFFECT_STATUS_ENCHANT:
        if (
            effect.value1 < 0
            or effect.value2
            or effect.effect_count < 0
            or (effect.effect_turn != -1 and effect.effect_turn <= 0)
        ):
            errors.append(f"effect-status-enchant-shape:{effect.id}")
    elif effect.effect_type == EFFECT_STATUS_ENCHANT_ENCORE:
        # The full exact shape, including its nested listener, was validated
        # before the ordinary type-specific ladder.
        pass
    elif effect.effect_type == EFFECT_FORCE_PLAY_CARD_SEARCH:
        from .plan3_use_pool import matches_ordinary_force_effect
        from .plan3_force_play_card_search import HOLD_ALL_EFFECT_ID

        if (
            effect.id != EXACT_ENCORE_FORCE_PLAY_EFFECT_ID
            and effect.id != HOLD_ALL_EFFECT_ID
            and not matches_ordinary_force_effect(effect)
        ):
            errors.append(f"effect-force-play-shape:{effect.id}")
    elif effect.effect_type == EFFECT_CARD_SEARCH_PLAY_COUNT_BUFF:
        from .plan3_use_pool import matches_play_count_buff_effect

        if not matches_play_count_buff_effect(effect):
            errors.append(f"effect-play-count-buff-shape:{effect.id}")
    elif effect.effect_type == EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST:
        from .plan3_use_pool import matches_with_cost_effect

        if not matches_with_cost_effect(effect):
            errors.append(f"effect-force-play-with-cost-shape:{effect.id}")
    elif effect.effect_type == EFFECT_EXTRA_TURN:
        from .plan3_men100016 import matches_extra_turn_effect

        if not matches_extra_turn_effect(effect):
            errors.append(f"effect-extra-turn-shape:{effect.id}")
    elif effect.effect_type == EFFECT_LESSON_VALUE_MULTIPLE:
        from .nia_lesson_value_multiple import (
            matches_lesson_value_multiple_effect_shape,
        )

        if not matches_lesson_value_multiple_effect_shape(effect):
            errors.append(f"effect-lesson-value-multiple-shape:{effect.id}")
    elif effect.effect_type == EFFECT_CARD_MOVE:
        if effect.value1 or effect.value2 or effect.effect_count or effect.effect_turn:
            errors.append(
                f"effect-card-move-values:{EFFECT_CARD_MOVE}:{effect.id}"
            )
        if effect.card_move_rule is None:
            errors.append(
                f"effect-card-move-missing:{EFFECT_CARD_MOVE}:{effect.id}"
            )
        elif not _is_general_card_move_rule(effect.card_move_rule):
            errors.append(
                f"effect-card-move-shape:{EFFECT_CARD_MOVE}:{effect.id}"
            )
        trigger = effect.trigger
        preservation_trigger = trigger is not None and (
            trigger.phase_types == (PHASE_NONE,)
            and not trigger.phase_values
            and not trigger.field_check_types
            and trigger.field_types == (FIELD_PRESERVATION_UP,)
            and not trigger.field_values
            and not trigger.field_card_search_ids
            and not trigger.produce_card_search_id
            and trigger.upper_search_count == 0
            and trigger.lower_search_count == 0
            and trigger.card_move_position_type == MOVE_UNKNOWN
            and not trigger.effect_types
            and trigger.lesson_type == LESSON_UNKNOWN
        )
        if (
            effect.card_move_rule is not None
            and _is_playing_self_hold_rule(effect.card_move_rule)
            and trigger is not None
            and not preservation_trigger
        ):
            errors.append(
                f"effect-card-move-trigger:{EFFECT_CARD_MOVE}:{effect.id}"
            )
    elif effect.effect_type == EFFECT_ADD_GROW:
        rule = effect.card_move_rule
        if effect.value1 or effect.value2 or effect.effect_count or effect.effect_turn:
            errors.append(f"effect-add-grow-values:{effect.id}")
        if rule is None:
            errors.append(f"effect-add-grow-missing:{effect.id}")
        elif not (
            not rule.target_card_id
            and rule.target_upgrade == 0
            and rule.target_effect_type == EFFECT_UNKNOWN
            and bool(rule.search_id)
            and rule.destination == MOVE_UNKNOWN
            and rule.pick_range_type == PICK_RANGE_ALL
            and not rule.pick_reference_search_id
            and rule.pick_count_type == PICK_COUNT_UNKNOWN
            and rule.pick_count_min == 0
            and rule.pick_count_max == 0
            and not rule.second_search_id
            and rule.second_pick_range_type == PICK_RANGE_UNKNOWN
            and not rule.second_pick_reference_search_id
            and rule.second_pick_count_type == PICK_COUNT_UNKNOWN
            and rule.second_pick_count_min == 0
            and rule.second_pick_count_max == 0
            and not rule.chain_effect_ids
            and not rule.card_status_enchant_id
            and bool(rule.card_grow_effect_ids)
        ):
            errors.append(f"effect-add-grow-shape:{effect.id}")
        if effect.trigger is not None:
            errors.append(f"effect-add-grow-trigger:{effect.id}")
    elif effect.effect_type == EFFECT_SEARCH_PLAY_CARD_STAMINA_CHANGE:
        if effect.id not in _SEARCH_PLAY_CARD_STAMINA_CHANGE_IDS:
            errors.append(f"effect-search-stamina-shape:{effect.id}")
    elif effect.effect_type == EFFECT_HAND_GRAVE_COUNT_CARD_DRAW:
        if effect.id != _HAND_GRAVE_COUNT_CARD_DRAW_ID:
            errors.append(f"effect-hand-grave-draw-shape:{effect.id}")
    elif effect.effect_type in {EFFECT_CARD_UPGRADE, EFFECT_CARD_CREATE_ID}:
        # Raw search/pick validation belongs to the GUID-native resolver.
        if effect.value1 or effect.value2 or effect.effect_count or effect.effect_turn:
            label = (
                "card-upgrade"
                if effect.effect_type == EFFECT_CARD_UPGRADE
                else "card-create-id"
            )
            errors.append(f"effect-{label}-values:{effect.id}")
    elif effect.effect_type == EFFECT_ANTI_DEBUFF:
        if (
            effect.id
            not in {
                "e_effect-exam_anti_debuff-01",
                "e_effect-exam_anti_debuff-02",
                "e_effect-exam_anti_debuff-03",
                "e_effect-exam_anti_debuff-05",
            }
            or effect.value1
            or effect.value2
            or effect.effect_count not in {1, 2, 3, 5}
            or effect.effect_turn
            or effect.id.rsplit("-", 1)[-1]
            != f"{effect.effect_count:02d}"
        ):
            errors.append(f"effect-anti-debuff-shape:{effect.id}")
    elif effect.effect_type == EFFECT_TIMER:
        if (
            effect.value1 <= 0
            or effect.value2
            or effect.effect_count != 1
            or effect.effect_turn
        ):
            errors.append(f"effect-timer-shape:{effect.id}")
        if effect.chain_effect_ids:
            errors.append(f"effect-timer-chain-list:{effect.id}")
        if not effect.chain_effect_id:
            errors.append(f"effect-timer-chain-missing:{effect.id}")
        elif effect.chain_effect is None:
            errors.append(
                f"effect-timer-chain-unresolved:{effect.chain_effect_id}:{effect.id}"
            )
        elif effect.chain_effect.id != effect.chain_effect_id:
            errors.append(
                f"effect-timer-chain-mismatch:{effect.chain_effect_id}:{effect.id}"
            )
        else:
            if effect.chain_effect.effect_type not in RUNTIME_STATUS_EFFECT_TYPES:
                errors.append(
                    "runtime-effect-type:"
                    f"{effect.chain_effect.effect_type}:{effect.chain_effect.id}"
                )
            errors.extend(_effect_shape_errors(effect.chain_effect))
    elif effect.effect_type == EFFECT_STANCE_LOCK:
        from .plan3_stance_lock import resolve_plan3_stance_lock

        resolution = resolve_plan3_stance_lock(effect)
        if not resolution.resolved:
            errors.append(f"effect-stance-lock-shape:{effect.id}")
    elif effect.effect_type == EFFECT_OVER_PRESERVATION:
        if not (
            effect.id == "e_effect-exam_over_preservation"
            and effect.value1 == 0
            and effect.value2 == 0
            and effect.effect_count == 0
            and effect.effect_turn == 0
        ):
            errors.append(f"effect-over-preservation-shape:{effect.id}")
    return _unique(errors)


RUNTIME_STATUS_EFFECT_TYPES = frozenset(
    {
        EFFECT_PRESERVATION,
        EFFECT_CONCENTRATION,
        EFFECT_LESSON,
        EFFECT_LESSON_FULL_POWER_POINT,
        EFFECT_BLOCK,
        EFFECT_BLOCK_FIX,
        EFFECT_ENTHUSIASTIC_ADDITIVE,
        EFFECT_ENTHUSIASTIC_MULTIPLE,
        EFFECT_FULL_POWER_POINT,
        EFFECT_FULL_POWER_POINT_REDUCE,
        EFFECT_STAMINA_RECOVER_FIX,
        EFFECT_STAMINA_RECOVER_MULTIPLE,
        EFFECT_STAMINA_REDUCE_FIX,
        EFFECT_STAMINA_CONSUMPTION_DOWN,
        EFFECT_STAMINA_CONSUMPTION_DOWN_FIX,
        EFFECT_PLAYABLE_VALUE_ADD,
        EFFECT_CARD_DRAW,
        EFFECT_ADD_GROW,
        EFFECT_CARD_UPGRADE,
        EFFECT_STANCE_LOCK,
        EFFECT_OVER_PRESERVATION,
        EFFECT_FORCE_PLAY_CARD_SEARCH,
        EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST,
        EFFECT_LESSON_VALUE_MULTIPLE,
    }
)


def _status_rule_shape_errors(
    rule: Plan3StatusEnchantRule,
    *,
    allow_native_grow: bool = False,
) -> tuple[str, ...]:
    if rule.id == EXACT_ENCORE_STATUS_ID:
        return _unique(
            (*_exact_encore_rule_errors(rule),)
        )
    errors = list(_trigger_shape_errors(rule.trigger))
    if not rule.effects:
        errors.append(f"status-effects-empty:{rule.id}")
    for effect in rule.effects:
        from .plan3_startplay_idol_return import matches_effect as matches_idol_return
        from .plan3_play_history import matches_trigger_contract as matches_history_trigger
        exact_startplay_return = matches_history_trigger(rule.trigger) and matches_idol_return(effect)
        if effect.effect_type == EFFECT_FORCE_PLAY_CARD_SEARCH:
            errors.append(
                f"runtime-force-play-outside-exact-encore:{effect.id}"
            )
        if effect.effect_type == EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST:
            from .plan3_use_pool import matches_with_cost_effect

            if not matches_with_cost_effect(effect):
                errors.append(
                    f"runtime-force-play-with-cost-shape:{effect.id}"
                )
        if effect.effect_type not in RUNTIME_STATUS_EFFECT_TYPES and not (
            (allow_native_grow and effect.effect_type == EFFECT_ADD_GROW) or exact_startplay_return
        ):
            errors.append(
                f"runtime-effect-type:{effect.effect_type}:{effect.id}"
            )
        errors.extend(_effect_shape_errors(effect))
    return _unique(errors)


def validate_plan3_status_enchant(
    rule: Plan3StatusEnchantRule,
    *,
    allow_native_grow: bool = False,
) -> tuple[str, ...]:
    """Return strict native-shape errors for a loaded runtime listener."""

    if not isinstance(rule, Plan3StatusEnchantRule):
        raise TypeError("rule must be Plan3StatusEnchantRule")
    return _status_rule_shape_errors(
        rule, allow_native_grow=allow_native_grow
    )


def _card_shape_errors(
    card: Plan3Card, *, prior_play_count: int = 0
) -> tuple[str, ...]:
    errors: list[str] = []
    exact_sleepy_trouble = False
    if card.category == CATEGORY_TROUBLE:
        try:
            from .plan3_sleepy_trouble import (
                CARD_ID as SLEEPY_CARD_ID,
                load_sleepy_trouble_contract,
            )

            contract = load_sleepy_trouble_contract()
            exact_sleepy_trouble = (
                card.id == SLEEPY_CARD_ID
                and card.upgrade == contract.upgrade == 0
                and card.plan_type == contract.plan_type
                and card.category == contract.category
                and card.stamina_cost == contract.stamina_cost == 0
                and card.force_stamina_cost == contract.force_stamina_cost == 0
                and card.cost_type == contract.cost_type
                and card.cost_value == contract.cost_value == 0
                and card.play_trigger is None
                and card.move_position_type == contract.move_position_type
                and card.effects == ()
                and card.effect_group_ids == ()
                and contract.playable_by_static_native_gate
            )
        except (KeyError, TypeError, ValueError, OSError):
            exact_sleepy_trouble = False
        if not exact_sleepy_trouble:
            errors.append(f"exact-sleepy-trouble-card-family:{card.id}+{card.upgrade}")
    exact_family_effects = {
        EFFECT_MULTIPLE_ENTHUSIASTIC_LESSON,
        EFFECT_STATUS_ENCHANT_ENCORE,
    }
    if any(effect.effect_type in exact_family_effects for effect in card.effects):
        from .plan3_encore_enthusiastic_lesson import CARD_VERSION_BY_UPGRADE

        expected = CARD_VERSION_BY_UPGRADE.get(card.upgrade)
        if (
            card.id != EXACT_ENCORE_CARD_ID
            or expected is None
            or tuple(effect.id for effect in card.effects)
            != expected.ordered_effect_ids
            or tuple(effect.once for effect in card.effects)
            != (False, False, False, True)
        ):
            errors.append(f"exact-encore-card-family:{card.id}+{card.upgrade}")
    men100016_family = any(
        effect.effect_type == EFFECT_EXTRA_TURN
        or effect.effect_type == EFFECT_LESSON_VALUE_MULTIPLE
        or effect.status_enchant_id
        == "enchant-p_card-03-men-100_016-enc01"
        or (
            effect.status_enchant is not None
            and any(
                child.effect_type == EFFECT_LESSON_VALUE_MULTIPLE
                for child in effect.status_enchant.effects
            )
        )
        for effect in card.effects
    )
    if men100016_family:
        from .plan3_men100016 import matches_card as matches_men100016_card

        if not matches_men100016_card(card):
            errors.append(f"exact-men100016-card-family:{card.id}+{card.upgrade}")
    if card.plan_type not in SUPPORTED_PLAN_TYPES:
        errors.append(f"plan-type:{card.plan_type}")
    if card.category not in SUPPORTED_CARD_CATEGORIES:
        errors.append(f"card-category:{card.category}")
    if card.cost_type not in SUPPORTED_COST_TYPES:
        errors.append(f"cost-type:{card.cost_type}")
    if card.cost_type == COST_STAMINA:
        if card.cost_value != 0:
            errors.append(f"cost-value:{card.cost_value}")
    elif card.cost_type == COST_FULL_POWER_POINT:
        if card.cost_value <= 0:
            errors.append(f"cost-value:{card.cost_value}")
        if card.stamina_cost != 0 or card.force_stamina_cost != 0:
            errors.append(f"cost-full-power-point-shape:{card.id}")
    if card.stamina_cost < 0:
        errors.append(f"stamina-cost:{card.stamina_cost}")
    if card.force_stamina_cost < 0:
        errors.append(f"force-stamina-cost:{card.force_stamina_cost}")
    elif card.force_stamina_cost > 0 and (
        card.cost_type != COST_STAMINA or card.stamina_cost != 0
    ):
        # The PC Master rows prove forceStamina as a stamina-cost override:
        # every supported row has ordinary stamina == 0 and costType ==
        # ExamCostType_Unknown.  Keep mixed/other combinations fail-closed.
        errors.append(f"force-stamina-cost-shape:{card.id}")
    if card.move_position_type not in SUPPORTED_MOVE_POSITION_TYPES:
        errors.append(f"move-position:{card.move_position_type}")
    if card.play_trigger is not None:
        errors.extend(_trigger_shape_errors(card.play_trigger))
    if not card.effects and card.category != CATEGORY_TROUBLE:
        errors.append(f"card-effects-empty:{card.id}")
    direct_effects = tuple(
        effect
        for effect in card.effects
        if not (prior_play_count > 0 and effect.once)
    )
    card_move_effects = tuple(
        effect
        for effect in direct_effects
        if effect.effect_type == EFFECT_CARD_MOVE
    )
    if card_move_effects:
        if len(card_move_effects) != 1:
            errors.append(f"effect-card-move-count:{card.id}")
        else:
            move_effect = card_move_effects[0]
            move_rule = move_effect.card_move_rule
            if (
                move_rule is not None
                and _is_playing_self_hold_rule(move_rule)
                and card.move_position_type != MOVE_GRAVE
            ):
                errors.append(f"effect-card-move-base-position:{card.id}")
            move_index = direct_effects.index(move_effect)
            if any(
                effect.effect_type == EFFECT_CARD_DRAW
                for effect in direct_effects[:move_index]
            ):
                # Search candidates and Random keys are evaluated at the move
                # effect slot.  Prefix draws can mutate both zones and RNG;
                # keep that not-currently-used ordering fail-closed.
                errors.append(f"effect-card-move-draw-prefix:{card.id}")
    for effect in card.effects:
        # The supported native horizon currently executes only repeat zero.
        # Android skips an isOncePlayEffect slot on that repeat when this
        # exact card instance has already been played, so a skipped slot does
        # not need an executor or trigger implementation.
        if prior_play_count > 0 and effect.once:
            continue
        errors.extend(_effect_shape_errors(effect))
    from .plan3_use_pool import (
        NEO_CARD_ID,
        WITH_COST_CARD_ID,
        matches_neo_card,
        matches_with_cost_card,
    )

    if card.id == NEO_CARD_ID and not matches_neo_card(card):
        errors.append(f"card-neo-force-play-shape:{card.id}:{card.upgrade}")
    if card.id == WITH_COST_CARD_ID and not matches_with_cost_card(card):
        errors.append(
            f"card-force-play-with-cost-shape:{card.id}:{card.upgrade}"
        )
    return _unique(errors)


def _enchant_shape_errors(enchant: ActivePlan3StatusEnchant) -> tuple[str, ...]:
    errors = list(
        _status_rule_shape_errors(
            enchant.rule,
            allow_native_grow=enchant.source_id.startswith("p_memory_skill-"),
        )
    )
    if enchant.max_uses < 0:
        errors.append(f"status-max-uses:{enchant.instance_id}")
    if enchant.uses < 0 or (
        enchant.max_uses > 0 and enchant.uses > enchant.max_uses
    ):
        errors.append(f"status-uses:{enchant.instance_id}")
    if enchant.max_uses_per_turn < 0:
        errors.append(f"status-max-uses-per-turn:{enchant.instance_id}")
    if enchant.uses_this_turn < 0 or (
        enchant.max_uses_per_turn > 0
        and enchant.uses_this_turn > enchant.max_uses_per_turn
    ):
        errors.append(f"status-uses-this-turn:{enchant.instance_id}")
    if enchant.remaining_turns < -1:
        errors.append(f"status-turn:{enchant.instance_id}")
    if (
        not isinstance(enchant.turn_count, int)
        or isinstance(enchant.turn_count, bool)
        or enchant.turn_count < 0
    ):
        errors.append(f"status-turn-count:{enchant.instance_id}")
    phase_keys: set[str] = set()
    for phase, count in enchant.phase_counts:
        if not isinstance(phase, str) or not phase or phase in phase_keys:
            errors.append(f"status-phase-count-key:{enchant.instance_id}")
            continue
        phase_keys.add(phase)
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 0 <= count <= 2**31 - 1
        ):
            errors.append(f"status-phase-count:{enchant.instance_id}")
    exact_encore = enchant.rule.id == EXACT_ENCORE_STATUS_ID
    from .plan3_play_count_interval_trigger import matches_interval_five_rule
    from .plan3_status_change_full_power_point_trigger import (
        matches_status_change_full_power_point_rule,
    )

    exact_native_listener = bool(
        matches_interval_five_rule(enchant.rule)
        or matches_status_change_full_power_point_rule(enchant.rule)
    )
    if exact_native_listener and enchant.native_uid <= 0:
        errors.append(f"status-native-identity:{enchant.instance_id}")
    if enchant.is_encore_enchant != exact_encore:
        errors.append(f"status-encore-flag:{enchant.instance_id}")
    if exact_encore and (
        enchant.native_uid <= 0 or not enchant.captured_card_guid
    ):
        errors.append(f"status-encore-identity:{enchant.instance_id}")
    if not exact_encore and (
        enchant.captured_card_guid or enchant.is_encore_enchant
    ):
        errors.append(f"status-captured-card-unexpected:{enchant.instance_id}")
    return _unique(errors)


def install_plan3_item(state: Plan3State, item: Plan3ItemRule) -> Plan3ItemInstall:
    """Install a supported in-exam P-item without consuming its trigger use."""

    errors = list(item.unsupported_rules)
    if item.plan_type not in SUPPORTED_PLAN_TYPES:
        errors.append(f"item-plan-type:{item.plan_type}:{item.id}")
    # The selected slice models a lesson-long status.  Item-level fire limits
    # and intervals would be a second, independent activation mechanism.
    if item.fire_limit != 0:
        errors.append(f"item-fire-limit:{item.fire_limit}:{item.id}")
    if item.fire_interval != 0:
        errors.append(f"item-fire-interval:{item.fire_interval}:{item.id}")
    if not item.enchantments:
        errors.append(f"item-enchantments-empty:{item.id}")
    for rule, max_uses in item.enchantments:
        probe = ActivePlan3StatusEnchant(
            instance_id="probe",
            source_id=item.id,
            rule=rule,
            max_uses=max_uses,
        )
        errors.extend(_enchant_shape_errors(probe))
        lesson_type_value = LESSON_TYPE_VALUES.get(rule.trigger.lesson_type)
        if lesson_type_value is None:
            errors.append(
                f"trigger-lesson-type:{rule.trigger.lesson_type}:{rule.trigger.id}"
            )
        elif (
            lesson_type_value != 0
            and not state.is_battle
            and state.step_type_value == 0
        ):
            errors.append("state:step-type-unknown")
    errors = list(_unique(errors))
    if errors:
        return Plan3ItemInstall(state, state, item, False, tuple(errors))

    existing = {entry.instance_id for entry in state.active_status_enchants}
    working = state
    installed: list[ActivePlan3StatusEnchant] = []
    for index, (rule, max_uses) in enumerate(item.enchantments):
        lesson_type_value = LESSON_TYPE_VALUES[rule.trigger.lesson_type]
        if lesson_type_value != 0:
            if state.is_battle:
                valid = android_v323_lesson_is_battle_turn_valid(
                    lesson_type_value,
                    state.current_parameter_type,
                )
            else:
                valid = android_v323_lesson_is_start_valid(
                    lesson_type_value,
                    state.step_type_value,
                )
            if not valid:
                continue
        base = f"item:{item.id}:{rule.id}:{index}"
        instance_id = base
        suffix = 1
        while instance_id in existing:
            suffix += 1
            instance_id = f"{base}:{suffix}"
        existing.add(instance_id)
        working, uid = allocate_plan3_status_uid(working)
        installed.append(
            ActivePlan3StatusEnchant(
                instance_id=instance_id,
                source_id=item.id,
                rule=rule,
                max_uses=max_uses,
                native_uid=uid,
            )
        )
    after = replace(
        working,
        active_status_enchants=tuple((*state.active_status_enchants, *installed)),
    )
    return Plan3ItemInstall(state, after, item, True)


def _next_status_instance_id(state: Plan3State, base: str) -> str:
    existing = {entry.instance_id for entry in state.active_status_enchants}
    instance_id = base
    suffix = 1
    while instance_id in existing:
        suffix += 1
        instance_id = f"{base}:{suffix}"
    return instance_id


def _next_native_status_uid(state: Plan3State) -> int:
    """Allocate the next TriggerEffectStatus UID from existing native UIDs."""

    lesson_uids = tuple(
        status.uid
        for status in state.lesson_parameter_multiple_state.statuses
        if status.uid is not None
    )
    anti_debuff_uids = (
        ()
        if state.anti_debuff_runtime.uid is None
        else (state.anti_debuff_runtime.uid,)
    )
    playable_uids = (
        ()
        if state.playable_value_add_runtime.uid is None
        else (state.playable_value_add_runtime.uid,)
    )
    enthusiastic_uids = (
        ()
        if state.enthusiastic_runtime.uid is None
        else (state.enthusiastic_runtime.uid,)
    )
    maximum = max(
        (
            state.next_status_uid - 1,
            *(entry.native_uid for entry in state.active_status_enchants),
            *lesson_uids,
            *anti_debuff_uids,
            *playable_uids,
            *enthusiastic_uids,
        )
    )
    if maximum >= 2**31 - 1:
        raise ValueError("status-enchant-native-uid-overflow")
    return maximum + 1


def allocate_plan3_status_uid(state: Plan3State) -> tuple[Plan3State, int]:
    """Allocate one globally monotonic native ExamStatusEffect UID."""

    if not state.status_uid_cursor_exact:
        raise ValueError("status-uid-cursor-unprojected")
    uid = _next_native_status_uid(state)
    return replace(state, next_status_uid=uid + 1), uid


def _add_plan3_playable_value(
    state: Plan3State,
    count: int,
    *,
    source: str,
) -> Plan3State:
    """Commit one proven Playable add to scalar and typed sidecar."""

    return _add_plan3_playable_value_with_receipt(
        state,
        count,
        source=source,
    )[0]


def _add_plan3_playable_value_with_receipt(
    state: Plan3State,
    count: int,
    *,
    source: str,
) -> tuple[Plan3State, "PlayableValueAddReceipt"]:
    """Commit one Playable add and retain its typed UID receipt."""

    from .playable_value_add_runtime import add_playable_value_add

    if (
        not state.playable_value_add_runtime.status_present
        and not state.status_uid_cursor_exact
    ):
        raise ValueError("status-uid-cursor-unprojected")
    receipt = add_playable_value_add(
        state.playable_value_add_runtime,
        count,
        next_status_uid=state.next_status_uid,
        source=source,
    )
    return (
        replace(
            state,
            playable_value_add_runtime=receipt.after,
            next_status_uid=receipt.cursor_after,
            plays_remaining=state.plays_remaining + count,
        ),
        receipt,
    )


def _spend_plan3_playable_value_turn(
    state: Plan3State,
    *,
    source: str,
) -> Plan3State:
    """Apply the native TurnStart spend gate without changing scalar plays."""

    from .playable_value_add_runtime import spend_playable_value_add_turn

    receipt = spend_playable_value_add_turn(
        state.playable_value_add_runtime,
        next_status_uid=state.next_status_uid,
        source=source,
    )
    return replace(state, playable_value_add_runtime=receipt.after)


def _mark_plan3_playable_value_passing(
    state: Plan3State,
    *,
    source: str,
) -> Plan3State:
    """Apply the native outer TurnStart passing marker."""

    from .playable_value_add_runtime import (
        mark_playable_value_add_passing,
    )

    receipt = mark_playable_value_add_passing(
        state.playable_value_add_runtime,
        next_status_uid=state.next_status_uid,
        source=source,
    )
    return replace(state, playable_value_add_runtime=receipt.after)


def _add_plan3_enthusiastic_release(
    state: Plan3State,
    value: int,
    *,
    source: str,
) -> tuple[Plan3State, "EnthusiasticReceipt"]:
    """Commit one exact release value to scalar and typed UID sidecar."""

    from .enthusiastic_runtime import add_enthusiastic

    if state.enthusiasm != state.enthusiastic_runtime.value:
        raise ValueError("enthusiastic-runtime-scalar-mismatch")
    if (
        not state.enthusiastic_runtime.status_present
        and not state.status_uid_cursor_exact
    ):
        raise ValueError("status-uid-cursor-unprojected")
    receipt = add_enthusiastic(
        state.enthusiastic_runtime,
        value,
        next_status_uid=state.next_status_uid,
        source=source,
    )
    return (
        replace(
            state,
            enthusiasm=receipt.after.value,
            enthusiastic_runtime=receipt.after,
            next_status_uid=receipt.cursor_after,
        ),
        receipt,
    )


def _spend_plan3_enthusiastic_turn(
    state: Plan3State,
    *,
    source: str,
) -> tuple[Plan3State, "EnthusiasticReceipt | None"]:
    """Remove one exact passing turn-one release status."""

    from .enthusiastic_runtime import spend_enthusiastic_turn

    if state.enthusiasm != state.enthusiastic_runtime.value:
        raise ValueError("enthusiastic-runtime-scalar-mismatch")
    if not state.enthusiastic_runtime.status_present:
        return state, None
    receipt = spend_enthusiastic_turn(
        state.enthusiastic_runtime,
        next_status_uid=state.next_status_uid,
        source=source,
    )
    return (
        replace(
            state,
            enthusiasm=0,
            enthusiastic_runtime=receipt.after,
        ),
        receipt,
    )


def _install_exact_encore_status_enchant(
    state: Plan3State,
    effect: Plan3Effect,
    *,
    source_id: str,
    playing_guid: str,
) -> Plan3State:
    """Install the exact non-unique 197 listener with native UID/history."""

    errors = _exact_encore_effect_errors(effect)
    if errors:
        raise ValueError(errors[0])
    if not playing_guid:
        raise ValueError("encore-playing-guid-unavailable")
    assert effect.status_enchant is not None
    working, uid = allocate_plan3_status_uid(state)
    installed = ActivePlan3StatusEnchant(
        instance_id=f"native-status:{uid}:{effect.status_enchant.id}",
        source_id=source_id,
        rule=effect.status_enchant,
        max_uses=effect.effect_count,
        max_uses_per_turn=effect.value1,
        remaining_turns=effect.effect_turn,
        passing_turn_start=False,
        is_item_direct=False,
        native_uid=uid,
        captured_card_guid=playing_guid,
        is_encore_enchant=True,
    )
    return replace(
        working,
        active_status_enchants=tuple(
            (*state.active_status_enchants, installed)
        ),
    )


def _install_card_status_enchant(
    state: Plan3State,
    effect: Plan3Effect,
    *,
    source_id: str,
) -> Plan3State:
    """Append one native non-unique TriggerEffectStatusEffect instance."""

    assert effect.status_enchant is not None
    working, uid = allocate_plan3_status_uid(state)
    base = f"card:{source_id}:{effect.status_enchant.id}:{effect.id}"
    installed = ActivePlan3StatusEnchant(
        instance_id=_next_status_instance_id(state, base),
        source_id=source_id,
        rule=effect.status_enchant,
        max_uses=effect.effect_count,
        max_uses_per_turn=effect.value1,
        remaining_turns=effect.effect_turn,
        passing_turn_start=False,
        is_item_direct=False,
        native_uid=uid,
    )
    return replace(
        working,
        active_status_enchants=tuple(
            (*state.active_status_enchants, installed)
        ),
    )


def _install_effect_timer(
    state: Plan3State,
    effect: Plan3Effect,
    *,
    source_id: str,
) -> Plan3State:
    """Install the one-shot relative-turn listener made by EffectTimer."""

    assert effect.chain_effect is not None
    trigger = Plan3Trigger(
        id=f"effect-timer-trigger:{effect.id}",
        phase_types=(PHASE_TURN_TIMER,),
        phase_values=(effect.value1,),
    )
    rule = Plan3StatusEnchantRule(
        id=f"effect-timer:{effect.id}",
        trigger=trigger,
        effects=(effect.chain_effect,),
    )
    base = f"card:{source_id}:{rule.id}"
    working, uid = allocate_plan3_status_uid(state)
    installed = ActivePlan3StatusEnchant(
        instance_id=_next_status_instance_id(state, base),
        source_id=effect.id,
        rule=rule,
        max_uses=1,
        remaining_turns=-1,
        passing_turn_start=False,
        is_item_direct=False,
        turn_count=0,
        native_uid=uid,
    )
    return replace(
        working,
        active_status_enchants=tuple(
            (*state.active_status_enchants, installed)
        ),
    )


def _advance_status_enchants_for_turn_start(state: Plan3State) -> Plan3State:
    """Advance every retained TriggerEffect listener before StartTurn dispatch."""

    overflowing = next(
        (
            enchant
            for enchant in state.active_status_enchants
            if enchant.turn_count == 2**31 - 1
        ),
        None,
    )
    if overflowing is not None:
        raise ValueError(
            "status-enchant-turn-count-overflow:"
            f"{overflowing.instance_id}"
        )
    active: list[ActivePlan3StatusEnchant] = []
    for enchant in state.active_status_enchants:
        turns = enchant.remaining_turns
        if turns >= 0 and enchant.passing_turn_start:
            turns -= 1
        if turns == 0:
            continue
        active.append(
            replace(
                enchant,
                remaining_turns=turns,
                uses_this_turn=0,
                turn_count=enchant.turn_count + 1,
            )
        )
    return replace(state, active_status_enchants=tuple(active))


def _remove_triggered_turn_timer_statuses(state: Plan3State) -> Plan3State:
    """Mirror native ``RemoveTurnTimerTriggeredStatus`` for this round."""

    active = tuple(
        enchant
        for enchant in state.active_status_enchants
        if not (
            enchant.rule.trigger.phase_types == (PHASE_TURN_TIMER,)
            and enchant.turn_count in enchant.rule.trigger.phase_values
        )
    )
    return state if active == state.active_status_enchants else replace(
        state, active_status_enchants=active
    )


@dataclass(frozen=True, slots=True)
class _RuntimeResult:
    state: Plan3State
    fired_enchant_ids: tuple[str, ...] = ()
    fired_effect_ids: tuple[str, ...] = ()
    unsupported_rules: tuple[str, ...] = ()
    parameter_pre_fix_values: tuple[int, ...] = ()
    parameter_actual_values: tuple[int, ...] = ()
    parameter_clear_changed: bool = False
    parameter_perfect_changed: bool = False
    enthusiasm_released: int = 0
    extra_plays: int = 0
    full_power_points_gained: int = 0
    full_power_points_reduced: int = 0
    stamina_recovered: int = 0
    effect_events: tuple[Plan3RuntimeEffectEvent, ...] = ()


def _replace_active_enchant(
    state: Plan3State,
    instance_id: str,
    replacement: ActivePlan3StatusEnchant | None,
) -> Plan3State:
    active: list[ActivePlan3StatusEnchant] = []
    found = False
    for enchant in state.active_status_enchants:
        if enchant.instance_id != instance_id:
            active.append(enchant)
            continue
        found = True
        if replacement is not None:
            active.append(replacement)
    if not found:
        return state
    return replace(state, active_status_enchants=tuple(active))


def _replace_enchant_phase_count(
    enchant: ActivePlan3StatusEnchant,
    phase: str,
    count: int,
) -> ActivePlan3StatusEnchant:
    counts = [
        (stored_phase, stored_count)
        for stored_phase, stored_count in enchant.phase_counts
        if stored_phase != phase
    ]
    counts.append((phase, count))
    return replace(enchant, phase_counts=tuple(counts))


def _prepare_play_count_interval_candidates(
    state: Plan3State,
    *,
    externally_handled_instance_ids: tuple[str, ...] = (),
) -> tuple[
    Plan3State,
    tuple[ActivePlan3StatusEnchant, ...],
    tuple[str, ...],
]:
    """Increment exact phase-23 counters and snapshot modulo candidates."""

    from .plan3_play_count_interval_trigger import (
        matches_interval_five_rule,
    )

    handled = set(externally_handled_instance_ids)
    found_handled: set[str] = set()
    active: list[ActivePlan3StatusEnchant] = []
    candidates: list[ActivePlan3StatusEnchant] = []
    for enchant in state.active_status_enchants:
        if enchant.instance_id in handled:
            found_handled.add(enchant.instance_id)
            if enchant.rule.trigger.phase_types != (
                PHASE_PLAY_COUNT_INTERVAL,
            ):
                return state, (), (
                    "external-play-count-interval-phase-shape:"
                    f"{enchant.instance_id}",
                )
            active.append(enchant)
            continue
        if enchant.rule.trigger.phase_types != (PHASE_PLAY_COUNT_INTERVAL,):
            active.append(enchant)
            continue
        if not matches_interval_five_rule(enchant.rule):
            return state, (), (f"status-interval-five-shape:{enchant.instance_id}",)
        before = enchant.phase_count(PHASE_PLAY_COUNT_INTERVAL)
        if before >= 2**31 - 1:
            return state, (), (f"status-phase-count-overflow:{enchant.instance_id}",)
        updated = _replace_enchant_phase_count(
            enchant, PHASE_PLAY_COUNT_INTERVAL, before + 1
        )
        active.append(updated)
        decision = evaluate_plan3_trigger(
            updated.rule.trigger,
            state,
            event_phase=PHASE_PLAY_COUNT_INTERVAL,
            phase_count=before + 1,
        )
        if not decision.supported:
            return state, (), decision.unsupported_rules
        if decision.fires:
            candidates.append(updated)
    missing = handled.difference(found_handled)
    if missing:
        return state, (), tuple(
            f"external-play-count-interval-listener-missing:{instance_id}"
            for instance_id in externally_handled_instance_ids
            if instance_id in missing
        )
    if tuple(active) == state.active_status_enchants:
        return state, (), ()
    return replace(state, active_status_enchants=tuple(active)), tuple(candidates), ()


def _exact_unlimited_listener_skips_spend(
    enchant: ActivePlan3StatusEnchant,
) -> bool:
    return enchant.max_uses == 0 and enchant.max_uses_per_turn == 0


def _runtime_effect_source_allows_status_change(
    enchant: ActivePlan3StatusEnchant,
) -> bool:
    """Native 103/190 gate; current core creates only exact type-103 timers."""

    return bool(
        enchant.rule.id.startswith("effect-timer:")
        and enchant.source_id.startswith("e_effect-exam_effect_timer-")
        and enchant.rule.trigger.phase_types == (PHASE_TURN_TIMER,)
    )


def _apply_runtime_phase(
    state: Plan3State,
    event_phase: str,
    *,
    event_card: Plan3Card | None = None,
    trigger_state: Plan3State | None = None,
    settings: Plan3ExamSettings | None = None,
    candidate_snapshot: tuple[ActivePlan3StatusEnchant, ...] | None = None,
    event_effect_type: str | None = None,
    effect_result_trigger_enabled: bool = True,
) -> _RuntimeResult:
    if settings is None:
        settings = load_plan3_exam_settings()
    if trigger_state is None:
        trigger_state = state
    fired_enchants: list[str] = []
    fired_effects: list[str] = []
    parameter_pre_fix_values: list[int] = []
    parameter_actual_values: list[int] = []
    parameter_clear_changed = False
    parameter_perfect_changed = False
    enthusiasm_released = 0
    extra_plays = 0
    full_power_points_gained = 0
    full_power_points_reduced = 0
    stamina_recovered = 0
    effect_events: list[Plan3RuntimeEffectEvent] = []
    working = state

    candidates: list[ActivePlan3StatusEnchant] = []
    if candidate_snapshot is not None:
        if not isinstance(candidate_snapshot, tuple) or not all(
            isinstance(item, ActivePlan3StatusEnchant)
            for item in candidate_snapshot
        ):
            return _RuntimeResult(
                state, unsupported_rules=("runtime-candidate-snapshot-invalid",)
            )
        candidates.extend(candidate_snapshot)
    else:
        for enchant in state.active_status_enchants:
            errors = _enchant_shape_errors(enchant)
            if errors:
                return _RuntimeResult(state, unsupported_rules=errors)
            if enchant.max_uses > 0 and enchant.uses >= enchant.max_uses:
                continue
            if (
                enchant.max_uses_per_turn > 0
                and enchant.uses_this_turn >= enchant.max_uses_per_turn
            ):
                continue
            decision = evaluate_plan3_trigger(
                enchant.rule.trigger,
                trigger_state,
                event_phase=event_phase,
                event_card=event_card,
                turn_timer_count=enchant.turn_count,
                phase_count=enchant.phase_count(event_phase),
                event_effect_type=event_effect_type,
                effect_result_trigger_enabled=effect_result_trigger_enabled,
            )
            if not decision.supported:
                return _RuntimeResult(state, unsupported_rules=decision.unsupported_rules)
            if decision.fires:
                candidates.append(enchant)

    # Android v3.2.3 builds the whole phase candidate list first.  Count is
    # spent when each queued trigger command is inserted, before its nested
    # effects execute; later state changes cannot add new peers to this event.
    for candidate in candidates:
        current = next(
            (
                enchant
                for enchant in working.active_status_enchants
                if enchant.instance_id == candidate.instance_id
            ),
            None,
        )
        if current is None:
            continue
        spent = (
            current
            if _exact_unlimited_listener_skips_spend(current)
            else replace(
                current,
                uses=current.uses + 1,
                uses_this_turn=current.uses_this_turn + 1,
            )
        )
        exhausted_card_listener = (
            not current.is_item_direct
            and spent.max_uses > 0
            and spent.uses >= spent.max_uses
        )
        working = _replace_active_enchant(
            working,
            current.instance_id,
            None if exhausted_card_listener else spent,
        )
        fired_enchants.append(current.rule.id)
        for effect in current.rule.effects:
            effect_before = working
            effect_after: Plan3State | None = None
            nested_effect_events: tuple[Plan3RuntimeEffectEvent, ...] = ()
            if effect.effect_type == EFFECT_PRESERVATION:
                if working.stance != STANCE_FULL_POWER:
                    stance_result = _change_stance(
                        working,
                        STANCE_PRESERVATION,
                        effect.value1,
                        settings,
                    )
                    working = stance_result.state
                    enthusiasm_released += stance_result.enthusiasm_released
                    extra_plays += stance_result.extra_plays
            elif effect.effect_type == EFFECT_CONCENTRATION:
                if working.stance != STANCE_FULL_POWER:
                    stance_result = _change_stance(
                        working,
                        STANCE_CONCENTRATION,
                        effect.value1,
                        settings,
                    )
                    working = stance_result.state
                    enthusiasm_released += stance_result.enthusiasm_released
                    extra_plays += stance_result.extra_plays
                    if not stance_result.changed:
                        continue
                    effect_after = working
                    nested = _apply_runtime_phase(
                        working,
                        PHASE_STANCE_CHANGE_CONCENTRATION,
                        settings=settings,
                    )
                    if nested.unsupported_rules:
                        return _RuntimeResult(
                            state,
                            unsupported_rules=nested.unsupported_rules,
                        )
                    working = nested.state
                    nested_effect_events = nested.effect_events
                    fired_enchants.extend(nested.fired_enchant_ids)
                    fired_effects.extend(nested.fired_effect_ids)
                    parameter_pre_fix_values.extend(
                        nested.parameter_pre_fix_values
                    )
                    parameter_actual_values.extend(
                        nested.parameter_actual_values
                    )
                    parameter_clear_changed = (
                        parameter_clear_changed
                        or nested.parameter_clear_changed
                    )
                    parameter_perfect_changed = (
                        parameter_perfect_changed
                        or nested.parameter_perfect_changed
                    )
                    enthusiasm_released += nested.enthusiasm_released
                    extra_plays += nested.extra_plays
                    full_power_points_gained += (
                        nested.full_power_points_gained
                    )
                    full_power_points_reduced += (
                        nested.full_power_points_reduced
                    )
                    stamina_recovered += nested.stamina_recovered
            elif effect.effect_type == EFFECT_BLOCK:
                working = _add_plan3_block(working, effect.value1)
            elif effect.effect_type == EFFECT_BLOCK_FIX:
                # BlockFixEffectExecutor bypasses CalculateAddBlock and calls
                # AddBlock directly, so BlockRestriction does not suppress it.
                working = replace(working, block=working.block + effect.value1)
            elif effect.effect_type == EFFECT_ENTHUSIASTIC_ADDITIVE:
                working = apply_plan3_enthusiasm_additive_status(
                    working,
                    value=effect.value1,
                    turns=effect.effect_turn,
                    source_effect_id=effect.id,
                )
            elif effect.effect_type == EFFECT_ENTHUSIASTIC_MULTIPLE:
                if working.enthusiasm_gain_bonus_percent != 0:
                    return _RuntimeResult(
                        state,
                        unsupported_rules=(
                            "state:enthusiasm-multiple-legacy-source-conflict",
                        ),
                    )
                working = _add_enthusiasm_multiple_status(
                    working,
                    value_permille=effect.value1,
                    turns=effect.effect_turn,
                )
            elif effect.effect_type == EFFECT_FULL_POWER_POINT:
                working, gained = _add_full_power_points(
                    working, effect.value1
                )
                from .plan3_status_change_full_power_point_trigger import (
                    matches_status_change_full_power_point_rule,
                )

                if matches_status_change_full_power_point_rule(current.rule):
                    # The audited phase-13 child uses the Full Power gauge's
                    # native ten-point ceiling.  Keep this narrowly attached
                    # to that exact listener so legacy generic gains retain
                    # their established behavior.
                    ceiling = max(10, effect_before.full_power_points)
                    if working.full_power_points > ceiling:
                        actual = ceiling - effect_before.full_power_points
                        working = replace(
                            working,
                            full_power_points=ceiling,
                            full_power_points_total=(
                                effect_before.full_power_points_total + actual
                            ),
                        )
                        gained = actual
                full_power_points_gained += gained
                effect_after = working
                if gained > 0 and _runtime_effect_source_allows_status_change(
                    current
                ):
                    nested = _apply_runtime_phase(
                        working,
                        PHASE_STATUS_CHANGE,
                        trigger_state=working,
                        settings=settings,
                        event_effect_type=EFFECT_FULL_POWER_POINT,
                        effect_result_trigger_enabled=True,
                    )
                    if nested.unsupported_rules:
                        return _RuntimeResult(
                            state,
                            unsupported_rules=nested.unsupported_rules,
                        )
                    working = nested.state
                    nested_effect_events = nested.effect_events
                    fired_enchants.extend(nested.fired_enchant_ids)
                    fired_effects.extend(nested.fired_effect_ids)
                    parameter_pre_fix_values.extend(
                        nested.parameter_pre_fix_values
                    )
                    parameter_actual_values.extend(
                        nested.parameter_actual_values
                    )
                    parameter_clear_changed = (
                        parameter_clear_changed
                        or nested.parameter_clear_changed
                    )
                    parameter_perfect_changed = (
                        parameter_perfect_changed
                        or nested.parameter_perfect_changed
                    )
                    enthusiasm_released += nested.enthusiasm_released
                    extra_plays += nested.extra_plays
                    full_power_points_gained += nested.full_power_points_gained
                    full_power_points_reduced += nested.full_power_points_reduced
                    stamina_recovered += nested.stamina_recovered
            elif effect.effect_type == EFFECT_FULL_POWER_POINT_ADDITIVE:
                working = _install_full_power_point_additive(working, effect)
            elif effect.effect_type == EFFECT_FULL_POWER_POINT_REDUCE:
                reduced = min(working.full_power_points, effect.value1)
                working = replace(
                    working,
                    full_power_points=working.full_power_points - reduced,
                )
                full_power_points_reduced += reduced
            elif effect.effect_type in (
                EFFECT_LESSON,
                EFFECT_LESSON_FULL_POWER_POINT,
            ):
                lesson_value = effect.value1
                if effect.effect_type == EFFECT_LESSON_FULL_POWER_POINT:
                    lesson_value += get_ratio_effect_int_value(
                        working.full_power_points_total,
                        effect.value2,
                        is_ceil=False,
                    )
                for _ in range(effect.effect_count):
                    hit = calculate_adding_parameter(
                        lesson_value,
                        is_buff_active=True,
                        status=_plan3_adding_parameter_status(working),
                        settings=_adding_parameter_settings(settings),
                    )
                    application = apply_parameter_add(
                        hit,
                        status=ParameterApplicationStatus(
                            judge_parameter=working.score,
                            limit_border=working.limit_border,
                            clear_border=working.clear_border,
                            current_turn_total_add_parameter=(
                                working.current_turn_total_add_parameter
                            ),
                            slump=working.slump,
                            is_battle=working.is_battle,
                            current_parameter_type=ProduceParameterType(
                                working.current_parameter_type
                            ),
                            battle_bonus_permille_vocal=(
                                working.battle_bonus_permille_vocal
                            ),
                            battle_bonus_permille_dance=(
                                working.battle_bonus_permille_dance
                            ),
                            battle_bonus_permille_visual=(
                                working.battle_bonus_permille_visual
                            ),
                            judge_parameter_vocal=working.judge_parameter_vocal,
                            judge_parameter_dance=working.judge_parameter_dance,
                            judge_parameter_visual=working.judge_parameter_visual,
                        ),
                    )
                    parameter_pre_fix_values.append(
                        application.pre_fix_parameter
                    )
                    parameter_actual_values.append(
                        application.actual_parameter
                    )
                    parameter_clear_changed = (
                        parameter_clear_changed or application.clear_changed
                    )
                    parameter_perfect_changed = (
                        parameter_perfect_changed
                        or application.perfect_changed
                    )
                    working = replace(
                        working,
                        score=application.after,
                        current_turn_total_add_parameter=(
                            application.current_turn_total_add_parameter
                        ),
                        judge_parameter_vocal=(
                            application.judge_parameter_vocal
                        ),
                        judge_parameter_dance=(
                            application.judge_parameter_dance
                        ),
                        judge_parameter_visual=(
                            application.judge_parameter_visual
                        ),
                    )
            elif effect.effect_type == EFFECT_LESSON_VALUE_MULTIPLE:
                from .nia_lesson_value_multiple import (
                    install_lesson_value_multiple,
                    matches_lesson_value_multiple_effect_shape,
                )

                if not matches_lesson_value_multiple_effect_shape(effect):
                    return _RuntimeResult(
                        state,
                        unsupported_rules=(
                            f"effect-lesson-value-multiple-shape:{effect.id}",
                        ),
                    )
                installed = install_lesson_value_multiple(
                    working.lesson_parameter_multiple_state,
                    permil=effect.value1,
                    turn=effect.effect_turn,
                )
                if not installed.installed:
                    return _RuntimeResult(
                        state,
                        unsupported_rules=(
                            f"effect-lesson-value-multiple-blocked:{effect.id}",
                        ),
                    )
                multiplier_state = installed.state
                if installed.merged_index is None:
                    working, status_uid = allocate_plan3_status_uid(working)
                    statuses = list(multiplier_state.statuses)
                    statuses[-1] = replace(statuses[-1], uid=status_uid)
                    multiplier_state = type(multiplier_state)(tuple(statuses))
                working = replace(
                    working,
                    lesson_parameter_multiple_state=multiplier_state,
                )
            elif effect.effect_type == EFFECT_STAMINA_RECOVER_FIX:
                if working.max_stamina is None:
                    return _RuntimeResult(
                        state,
                        unsupported_rules=("state:max-stamina-unknown",),
                    )
                recovered = min(
                    effect.value1,
                    max(0, working.max_stamina - working.stamina),
                )
                working = replace(
                    working, stamina=working.stamina + recovered
                )
                stamina_recovered += recovered
            elif effect.effect_type == EFFECT_STAMINA_RECOVER_MULTIPLE:
                if working.max_stamina is None:
                    return _RuntimeResult(
                        state,
                        unsupported_rules=("state:max-stamina-unknown",),
                    )
                from .exam_stamina_recover_multiple import (
                    MultipleEffectContract,
                    StaminaRecoverMultipleRuntime,
                    evaluate_stamina_recover_multiple,
                )

                evaluated = evaluate_stamina_recover_multiple(
                    MultipleEffectContract(
                        effect_id=effect.id,
                        effect_value1=effect.value1,
                        effect_value2=effect.value2,
                        effect_count=effect.effect_count,
                        effect_turn=effect.effect_turn,
                        status_enchant_id=effect.status_enchant_id,
                        chain_effect_id=effect.chain_effect_id,
                    ),
                    StaminaRecoverMultipleRuntime(
                        stamina=working.stamina,
                        max_stamina=working.max_stamina,
                        block=working.block,
                    ),
                )
                if not evaluated.executable:
                    return _RuntimeResult(
                        state,
                        unsupported_rules=(
                            "effect-stamina-recover-multiple-runtime:"
                            f"{effect.id}:{evaluated.reason or 'unresolved'}",
                        ),
                    )
                recovered = evaluated.actual_stamina - working.stamina
                working = replace(
                    working,
                    stamina=evaluated.actual_stamina,
                )
                stamina_recovered += recovered
            elif effect.effect_type == EFFECT_STAMINA_REDUCE_FIX:
                working = replace(
                    working,
                    stamina=max(0, working.stamina - effect.value1),
                )
            elif effect.effect_type == EFFECT_STAMINA_REDUCE:
                if working.max_stamina is None:
                    return _RuntimeResult(
                        state,
                        unsupported_rules=("state:max-stamina-unknown",),
                    )
                if effect.id != "e_effect-exam_stamina_reduce-1000":
                    return _RuntimeResult(
                        state,
                        unsupported_rules=(
                            f"effect-stamina-reduce-shape:{effect.id}",
                        ),
                    )
                working = replace(working, stamina=0)
            elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN:
                working = _add_stamina_consumption_down_status(
                    working,
                    turns=effect.effect_turn,
                )
            elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN_FIX:
                working = _add_stamina_consumption_down_fix_status(
                    working,
                    value=effect.value1,
                    turns=effect.effect_turn,
                )
            elif effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
                working = replace(
                    working,
                    plays_remaining=(
                        working.plays_remaining + effect.effect_count
                    ),
                )
            elif effect.effect_type == EFFECT_ADD_GROW:
                # The scalar kernel records the authoritative effect ID.  The
                # GUID-native boundary applies its deck-all card mutation.
                pass
            elif effect.effect_type == EFFECT_CARD_DRAW:
                # Zone movement and HandAdd are replayed by the GUID-native
                # simulator at this exact event slot.
                pass
            elif effect.effect_type == EFFECT_CARD_UPGRADE:
                # The scalar event plan records the exact delayed slot.  The
                # GUID-native replay resolves the Hand search and replaces
                # each selected instance with its upgraded Master lineage.
                pass
            elif effect.effect_type == EFFECT_CARD_MOVE:
                # Only the exact StartPlay/history-bound idol return passed
                # the status-rule validator. Its GUID move settles in the
                # same native event queue before the following random upgrade.
                from .plan3_startplay_idol_return import matches_effect as matches_idol_return
                from .plan3_play_history import matches_trigger_contract as matches_history_trigger
                if not matches_history_trigger(current.rule.trigger) or not matches_idol_return(effect):
                    return _RuntimeResult(state, unsupported_rules=("runtime-card-move-unresolved:" + effect.id,))
            elif (
                effect.effect_type
                == EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST
            ):
                # The scalar listener records the ordered command slot.  The
                # GUID-native UsePool boundary resolves Select1 and pays the
                # selected card's live cost after StartTurn settlement.
                pass
            elif effect.effect_type == EFFECT_STANCE_LOCK:
                from .plan3_stance_lock import execute_standalone_stance_lock

                lock_result = execute_standalone_stance_lock(
                    effect, working.stance_lock_runtime
                )
                if not lock_result.executable:
                    return _RuntimeResult(
                        state,
                        unsupported_rules=(
                            f"effect-stance-lock-runtime:{effect.id}",
                        ),
                    )
                working = replace(
                    working,
                    stance_lock_runtime=lock_result.runtime_after,
                )
            elif effect.effect_type == EFFECT_OVER_PRESERVATION:
                stance_result, over_errors = _apply_over_preservation(working)
                if over_errors:
                    return _RuntimeResult(
                        state, unsupported_rules=over_errors
                    )
                working = stance_result.state
                if not stance_result.changed:
                    continue
            else:
                return _RuntimeResult(
                    state,
                    unsupported_rules=(
                        f"runtime-effect-type:{effect.effect_type}:{effect.id}",
                    ),
                )
            fired_effects.append(effect.id)
            if effect_after is None:
                effect_after = working
            effect_events.append(
                Plan3RuntimeEffectEvent(
                    sequence_index=len(effect_events),
                    phase=event_phase,
                    source_enchant_instance_id=current.instance_id,
                    source_rule_id=current.rule.id,
                    source_id=current.source_id,
                    effect=effect,
                    before=effect_before,
                    after=effect_after,
                    requested_count=(
                        effect.value1
                        if effect.effect_type == EFFECT_CARD_DRAW
                        else None
                    ),
                )
            )
            effect_events.extend(nested_effect_events)

        exhausted = spent.max_uses > 0 and spent.uses >= spent.max_uses
        if exhausted and not spent.is_item_direct:
            working = _replace_active_enchant(
                working, spent.instance_id, None
            )
    return _RuntimeResult(
        state=working,
        fired_enchant_ids=tuple(fired_enchants),
        fired_effect_ids=tuple(fired_effects),
        parameter_pre_fix_values=tuple(parameter_pre_fix_values),
        parameter_actual_values=tuple(parameter_actual_values),
        parameter_clear_changed=parameter_clear_changed,
        parameter_perfect_changed=parameter_perfect_changed,
        enthusiasm_released=enthusiasm_released,
        extra_plays=extra_plays,
        full_power_points_gained=full_power_points_gained,
        full_power_points_reduced=full_power_points_reduced,
        stamina_recovered=stamina_recovered,
        effect_events=tuple(
            replace(event, sequence_index=index)
            for index, event in enumerate(effect_events)
        ),
    )


@dataclass(frozen=True, slots=True)
class _StanceResult:
    state: Plan3State
    enthusiasm_released: int = 0
    extra_plays: int = 0
    changed: bool = True
    enthusiastic_receipts: tuple["EnthusiasticReceipt", ...] = ()
    release_status_receipts: tuple[object, ...] = ()


def _native_stance_snapshot_for_lock(state: Plan3State):
    """Project only the fields read before StanceLock's mutation gate."""

    from .plan3_stance_lock import NativeStance, StanceMutationSnapshot

    if state.stance == STANCE_CONCENTRATION:
        status_type = NativeStance.CONCENTRATION
        status_step = state.stance_level
    elif state.stance == STANCE_PRESERVATION:
        status_type = (
            NativeStance.OVER_PRESERVATION
            if state.stance_level >= 3
            else NativeStance.PRESERVATION
        )
        status_step = 1 if state.stance_level >= 3 else state.stance_level
    elif state.stance == STANCE_FULL_POWER:
        status_type = NativeStance.FULL_POWER
        status_step = 1
    else:
        status_type = NativeStance.UNKNOWN
        status_step = 0
    return StanceMutationSnapshot(
        status_type=status_type,
        status_step=status_step,
        stance_change_count=state.stance_change_count,
        concentration_change_count=state.concentration_change_count,
        preservation_change_count=state.preservation_change_count,
        full_power_change_count=state.full_power_change_count,
    )


def _apply_over_preservation(
    state: Plan3State,
) -> tuple[_StanceResult, tuple[str, ...]]:
    """Run OverPreservation through the shared generic stance-lock gate."""

    from .plan3_over_preservation import (
        OVER_PRESERVATION_EFFECT_ROW,
        execute_plan3_over_preservation,
    )
    from .plan3_stance_lock import (
        NativeStance,
        StanceGateOutcome,
        evaluate_try_internal_set_stance_gate,
    )

    # The executor's FullPower and already-Over guards precede every utility
    # lock query.  Preserve that ordering, including no recently-used marker.
    if state.stance == STANCE_FULL_POWER or (
        state.stance == STANCE_PRESERVATION and state.stance_level >= 3
    ):
        result = execute_plan3_over_preservation(
            OVER_PRESERVATION_EFFECT_ROW, state
        )
        return _StanceResult(state, changed=False), ()

    gate = evaluate_try_internal_set_stance_gate(
        state.stance_lock_runtime,
        _native_stance_snapshot_for_lock(state),
        target_status=NativeStance.OVER_PRESERVATION,
        requested_step=1,
    )
    gated_state = replace(state, stance_lock_runtime=gate.lock_after)
    if gate.outcome is not StanceGateOutcome.PASSED:
        return _StanceResult(gated_state, changed=False), ()

    result = execute_plan3_over_preservation(
        OVER_PRESERVATION_EFFECT_ROW, gated_state
    )
    if not result.resolved:
        return (
            _StanceResult(state, changed=False),
            (f"effect-over-preservation-runtime:{result.reason.value}",),
        )
    return _StanceResult(result.state_after, changed=result.changed), ()


def apply_plan3_enthusiasm_additive_status(
    state: Plan3State,
    *,
    value: int,
    turns: int,
    source_effect_id: str = "",
) -> Plan3State:
    """Install/update one native additive layer and its scalar projection."""

    if not isinstance(state, Plan3State):
        raise TypeError("state must be Plan3State")
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("enthusiasm additive value must be an integer")
    if (
        not isinstance(turns, int)
        or isinstance(turns, bool)
        or (turns != -1 and turns <= 0)
    ):
        raise ValueError(
            "enthusiasm additive turns must be -1 or a positive integer"
        )
    if not isinstance(source_effect_id, str):
        raise TypeError("source_effect_id must be text")

    statuses = list(state.enthusiasm_additive_statuses)
    for index in range(len(statuses) - 1, -1, -1):
        current = statuses[index]
        if current.turns != turns:
            continue
        wrapped = (current.value + value) & 0xFFFFFFFF
        signed = wrapped - 0x100000000 if wrapped & 0x80000000 else wrapped
        merged = max(0, signed)
        if merged == 0:
            statuses.pop(index)
        else:
            statuses[index] = Plan3EnthusiasmAdditiveStatus(
                value=merged,
                turns=turns,
                source_effect_ids=(
                    *current.source_effect_ids,
                    *((source_effect_id,) if source_effect_id else ()),
                ),
            )
        break
    else:
        wrapped = value & 0xFFFFFFFF
        signed = wrapped - 0x100000000 if wrapped & 0x80000000 else wrapped
        added = max(0, signed)
        if added:
            statuses.append(
                Plan3EnthusiasmAdditiveStatus(
                    value=added,
                    turns=turns,
                    source_effect_ids=(
                        (source_effect_id,) if source_effect_id else ()
                    ),
                )
            )
    return replace(
        state,
        enthusiasm_additive=sum(status.value for status in statuses),
        enthusiasm_additive_statuses=tuple(statuses),
    )


def _spend_enthusiasm_additive_turns(
    statuses: tuple[Plan3EnthusiasmAdditiveStatus, ...],
) -> tuple[Plan3EnthusiasmAdditiveStatus, ...]:
    spent: list[Plan3EnthusiasmAdditiveStatus] = []
    for status in statuses:
        if status.turns == -1:
            spent.append(status)
        elif status.turns > 1:
            spent.append(replace(status, turns=status.turns - 1))
    return tuple(spent)


def _add_enthusiasm_multiple_status(
    state: Plan3State,
    *,
    value_permille: int,
    turns: int,
) -> Plan3State:
    """Mirror ``TryAddEnthusiasticMultipleStatus``'s same-turn merge."""

    statuses = list(state.enthusiasm_multiple_statuses)
    for index in range(len(statuses) - 1, -1, -1):
        current = statuses[index]
        if current.turns != turns:
            continue
        # The native ``add w`` is Int32 wrapping, followed by a non-negative
        # clamp.  Master values stay far below overflow, but preserving that
        # edge keeps synthetic regression inputs honest.
        wrapped = (current.value_permille + value_permille) & 0xFFFFFFFF
        signed = wrapped - 0x100000000 if wrapped & 0x80000000 else wrapped
        merged = max(0, signed)
        if merged == 0:
            statuses.pop(index)
        else:
            statuses[index] = Plan3EnthusiasmMultipleStatus(merged, turns)
        return replace(state, enthusiasm_multiple_statuses=tuple(statuses))
    statuses.append(Plan3EnthusiasmMultipleStatus(value_permille, turns))
    return replace(state, enthusiasm_multiple_statuses=tuple(statuses))


def _install_full_power_point_additive(
    state: Plan3State, effect: Plan3Effect
) -> Plan3State:
    from .plan3_full_power_point_additive import (
        Plan3FullPowerPointAdditiveContract,
        execute_plan3_full_power_point_additive,
    )

    contract = Plan3FullPowerPointAdditiveContract(
        effect_id=effect.id,
        effect_type=effect.effect_type,
        value1=effect.value1,
        value2=effect.value2,
        effect_count=effect.effect_count,
        effect_turn=effect.effect_turn,
    )
    execution = execute_plan3_full_power_point_additive(
        contract,
        state.full_power_point_additive_runtime,
    )
    if not execution.applied:
        raise ValueError(
            "full-power-point-additive-unresolved:"
            f"{effect.id}:{execution.resolution.blocker_code}"
        )
    return replace(
        state,
        full_power_point_additive_runtime=execution.after,
    )


def _add_full_power_points(
    state: Plan3State, base_value: int
) -> tuple[Plan3State, int]:
    from .plan3_full_power_point_additive import (
        apply_plan3_full_power_point_gain,
    )

    execution = apply_plan3_full_power_point_gain(
        state,
        state.full_power_point_additive_runtime,
        base_value,
    )
    if not execution.supported or execution.gain is None:
        raise ValueError(
            "full-power-point-gain-unresolved:"
            f"{execution.unsupported_reason or 'unknown'}"
        )
    return execution.after, execution.gain.gained


def _add_stamina_consumption_down_status(
    state: Plan3State,
    *,
    turns: int,
) -> Plan3State:
    # Android v3.2.3 TryAddStaminaConsumptionDownStatus adds the requested
    # duration to an active same-type status at 0x7E97AB4..0x7E97ACC.  Direct
    # card effects and nested status-enchant effects use this same lifecycle.
    return replace(
        state,
        stamina_consumption_down_turns=(
            state.stamina_consumption_down_turns + turns
        ),
        stamina_consumption_down_fresh=True,
    )


def _set_stamina_consumption_down_fix_statuses(
    state: Plan3State,
    statuses: tuple[Plan3StaminaConsumptionDownFixStatus, ...],
) -> Plan3State:
    return replace(
        state,
        stamina_consumption_down_fixed=sum(status.value for status in statuses),
        stamina_consumption_down_fix_statuses=statuses,
    )


def _add_stamina_consumption_down_fix_status(
    state: Plan3State,
    *,
    value: int,
    turns: int,
    fresh: bool = True,
) -> Plan3State:
    """Append one fixed reduction status with its native lifetime."""

    status = Plan3StaminaConsumptionDownFixStatus(
        value=value,
        turns=turns,
        fresh=fresh,
    )
    return _set_stamina_consumption_down_fix_statuses(
        state,
        (*state.stamina_consumption_down_fix_statuses, status),
    )


def _spend_stamina_consumption_down_fix_turns(
    statuses: tuple[Plan3StaminaConsumptionDownFixStatus, ...],
) -> tuple[Plan3StaminaConsumptionDownFixStatus, ...]:
    spent: list[Plan3StaminaConsumptionDownFixStatus] = []
    for status in statuses:
        if status.fresh:
            spent.append(replace(status, fresh=False))
        elif status.turns == -1:
            spent.append(status)
        elif status.turns > 1:
            spent.append(replace(status, turns=status.turns - 1))
        # Native SpendTurn removes finite statuses when their counter reaches
        # zero.  Each fixed layer is spent independently.
    return tuple(spent)


def _add_plan3_block(state: Plan3State, value: int) -> Plan3State:
    """Apply a normal Block executor through v3.2.3 CalculateAddBlock."""

    added = calculate_add_block(
        value,
        is_buff_active=True,
        status=AddBlockStatus(
            block_restriction=state.block_restriction_turns > 0,
        ),
        settings=AddBlockSettings(),
    )
    return replace(state, block=state.block + added)


def _apply_block_restriction_effect(
    state: Plan3State,
    effect: Plan3Effect,
    *,
    direct_card: bool,
) -> Plan3State:
    """Run the exact status add gate and preserve native freshness."""

    from .plan3_block_restriction import (
        BlockRestrictionRuntime,
        EffectContext,
        execute_effect,
    )

    context = (
        EffectContext.DIRECT_CARD
        if direct_card
        else EffectContext.TURN_START_GIMMICK
    )
    execution = execute_effect(
        effect,
        BlockRestrictionRuntime(
            state.block_restriction_turns,
            is_passing_turn_start=(
                state.block_restriction_turns > 0
                and not state.block_restriction_fresh
            ),
        ),
        state.anti_debuff_runtime,
        context=context,
    )
    if not execution.executable:
        raise ValueError(
            f"effect-block-restriction-runtime:{effect.id}:"
            f"{','.join(execution.reasons)}"
        )
    after = execution.after
    fresh = after.status_present and not after.is_passing_turn_start
    if not direct_card and after.status_present:
        # A turn-start gimmick is followed by the native outer marker before
        # card play begins; the scalar end-turn boundary therefore sees it as
        # passing during this turn.
        fresh = False
    return replace(
        state,
        block_restriction_turns=after.turns,
        block_restriction_fresh=fresh,
        anti_debuff_runtime=execution.anti_debuff_gate.state_after,
    )


def _enthusiasm_multiple(
    statuses: tuple[Plan3EnthusiasmMultipleStatus, ...],
) -> float:
    """Return native ``1 + sum(value / 1000)`` with per-step float32."""

    multiple = f32(1.0)
    for status in statuses:
        multiple = f32(multiple + permille_to_f32(status.value_permille))
    return multiple


def _spend_enthusiasm_multiple_turns(
    statuses: tuple[Plan3EnthusiasmMultipleStatus, ...],
) -> tuple[Plan3EnthusiasmMultipleStatus, ...]:
    spent: list[Plan3EnthusiasmMultipleStatus] = []
    for status in statuses:
        if status.turns == -1:
            spent.append(status)
        elif status.turns > 1:
            spent.append(
                Plan3EnthusiasmMultipleStatus(
                    status.value_permille,
                    status.turns - 1,
                )
            )
        # Native SpendTurn removes finite statuses when Turn reaches zero.
    return tuple(spent)


def _change_stance(
    state: Plan3State,
    target: str,
    requested_level: int,
    settings: Plan3ExamSettings,
) -> _StanceResult:
    # Android v3.2.3 TryInternalSetStance checks IsIdolStatusIsStepMax before
    # any mutation when the current and target types match
    # (0x7E60B2C..0x7E60B44).  Concentration and Preservation are capped at
    # step 2 (0x7EA460C..0x7EA4630); OverPreservation is a separate type 4,
    # not a third regular Preservation step.  A capped request is therefore
    # a complete no-op, including the global and per-stance change counters.
    if (
        target in {STANCE_CONCENTRATION, STANCE_PRESERVATION}
        and state.stance == target
        and state.stance_level >= 2
    ):
        return _StanceResult(state, 0, 0, False)

    from .plan3_stance_lock import (
        NativeStance,
        StanceGateOutcome,
        evaluate_try_internal_set_stance_gate,
    )

    target_status = {
        STANCE_CONCENTRATION: NativeStance.CONCENTRATION,
        STANCE_PRESERVATION: NativeStance.PRESERVATION,
        STANCE_FULL_POWER: NativeStance.FULL_POWER,
    }[target]
    gate = evaluate_try_internal_set_stance_gate(
        state.stance_lock_runtime,
        _native_stance_snapshot_for_lock(state),
        target_status=target_status,
        requested_step=requested_level,
    )
    if gate.outcome is not StanceGateOutcome.PASSED:
        gated = (
            state
            if gate.lock_after is state.stance_lock_runtime
            else replace(state, stance_lock_runtime=gate.lock_after)
        )
        return _StanceResult(gated, 0, 0, False)

    # A zero-turn status does not block, but GetStanceLock(true) still marks
    # its UID before all release/counter/difference mutations.
    state = replace(state, stance_lock_runtime=gate.lock_after)

    enthusiasm_released = 0
    extra_plays = 0
    enthusiastic_receipts: tuple["EnthusiasticReceipt", ...] = ()
    release_status_receipts: tuple[object, ...] = ()
    block = state.block
    enthusiasm = state.enthusiasm
    plays = state.plays_remaining
    if state.stance == STANCE_PRESERVATION and target != STANCE_PRESERVATION:
        if state.stance_level == 1:
            base = settings.preservation_release_enthusiasm[0]
            release_plays = settings.preservation_release_plays[0]
            release_block = settings.preservation_release_block[0]
        elif state.stance_level == 2:
            base = settings.preservation_release_enthusiasm[1]
            release_plays = settings.preservation_release_plays[1]
            release_block = settings.preservation_release_block[1]
        else:
            base = settings.over_preservation_release_enthusiasm
            release_plays = settings.over_preservation_release_plays
            release_block = settings.over_preservation_release_block
        raw = state.enthusiasm_additive + base
        if state.enthusiasm_multiple_statuses:
            # TryInternalSetStance (Android v3.2.3, 0x7E613D8..0x7E61438)
            # converts the raw Int32 and accumulated multiplier to float32,
            # multiplies them, then uses ``fcvtps`` (ceiling).
            multiple = _enthusiasm_multiple(
                state.enthusiasm_multiple_statuses
            )
            enthusiasm_released = ceil_f32_to_i32(
                f32(f32(raw) * multiple)
            )
        else:
            enthusiasm_released = (
                raw * (100 + state.enthusiasm_gain_bonus_percent) + 99
            ) // 100
        if release_plays:
            state, playable_receipt = _add_plan3_playable_value_with_receipt(
                state,
                release_plays,
                source="preservation-release",
            )
            release_status_receipts = (playable_receipt,)
        if (
            enthusiasm_released
            and state.enthusiasm == state.enthusiastic_runtime.value
        ):
            state, receipt = _add_plan3_enthusiastic_release(
                state,
                enthusiasm_released,
                source="preservation-release",
            )
            enthusiastic_receipts = (receipt,)
            release_status_receipts = (*release_status_receipts, receipt)
        elif enthusiasm_released:
            # Compatibility-only scalar states have no UID-bearing sidecar.
            # Preserve their historical arithmetic without manufacturing an
            # exact native status receipt.
            state = replace(
                state,
                enthusiasm=state.enthusiasm + enthusiasm_released,
            )
        enthusiasm = state.enthusiasm
        plays = state.plays_remaining
        extra_plays = release_plays
        block = state.block + release_block

    if target == STANCE_FULL_POWER:
        level = 1
    elif target in {STANCE_CONCENTRATION, STANCE_PRESERVATION}:
        # The native merge is min((current type == target) + requested, 2)
        # at 0x7E61730..0x7E6175C and 0x7E61870..0x7E6189C.
        level = min(2, int(state.stance == target) + requested_level)
    else:
        level = requested_level
    concentration_changes = state.concentration_change_count
    preservation_changes = state.preservation_change_count
    full_power_changes = state.full_power_change_count
    if target == STANCE_CONCENTRATION:
        concentration_changes += 1
    elif target == STANCE_PRESERVATION:
        preservation_changes += 1
    elif target == STANCE_FULL_POWER:
        full_power_changes += 1
    changed = replace(
        state,
        stance=target,
        stance_level=level,
        stance_change_count=state.stance_change_count + 1,
        concentration_change_count=concentration_changes,
        preservation_change_count=preservation_changes,
        full_power_change_count=full_power_changes,
        enthusiasm=enthusiasm,
        block=block,
        plays_remaining=plays,
    )
    return _StanceResult(
        changed,
        enthusiasm_released,
        extra_plays,
        enthusiastic_receipts=enthusiastic_receipts,
        release_status_receipts=release_status_receipts,
    )


def _native_idol_status(state: Plan3State) -> IdolStatusType:
    if state.stance == STANCE_CONCENTRATION:
        return IdolStatusType.CONCENTRATION
    if state.stance == STANCE_PRESERVATION:
        return (
            IdolStatusType.OVER_PRESERVATION
            if state.stance_level >= 3
            else IdolStatusType.PRESERVATION
        )
    if state.stance == STANCE_FULL_POWER:
        return IdolStatusType.FULL_POWER
    return IdolStatusType.UNKNOWN


def _adding_parameter_settings(
    settings: Plan3ExamSettings,
) -> AddingParameterSettings:
    return AddingParameterSettings(
        concentration_lesson_permille=settings.concentration_lesson_permille,
        preservation_lesson_permille=settings.preservation_lesson_permille,
        full_power_lesson_permille=settings.full_power_lesson_permille,
        over_preservation_lesson_permille=(
            settings.over_preservation_lesson_permille
        ),
    )


def _stamina_payment_settings(
    settings: Plan3ExamSettings,
) -> StaminaPaymentSettings:
    return StaminaPaymentSettings(
        concentration_permille=settings.concentration_stamina_permille,
        preservation_permille=settings.preservation_stamina_permille,
        over_preservation_permille=settings.over_preservation_stamina_permille,
        consumption_down_permille=(
            settings.stamina_consumption_down_permille
        ),
        consumption_down_add_permille=(
            settings.stamina_consumption_down_add_permille
        ),
    )


def _plan3_effective_stamina_costs(
    card: Plan3Card,
    state: Plan3State,
    settings: Plan3ExamSettings,
) -> tuple[int, int]:
    """Calculate ordinary and unavoidable stamina costs in native order.

    ``forceStamina`` is a separate HP payment: it is reduced by the same
    stance/consumption-down percentage, but it cannot be paid by Block.  The
    fixed reduction is consumed by that unavoidable portion first; any
    remainder then applies to the ordinary card cost.  The Master slice has
    no card with both raw fields non-zero, while this ordering keeps the
    proven two-part payment deterministic for synthetic checks as well.
    """

    if card.cost_type != COST_STAMINA:
        return 0, 0
    payment_settings = _stamina_payment_settings(settings)
    common = dict(
        idol_status_type=_native_idol_status(state),
        idol_status_step=max(1, state.stance_level),
        consumption_down=state.stamina_consumption_down_turns > 0,
    )
    force_before_fixed = calculate_effective_stamina_cost(
        card.force_stamina_cost,
        status=StaminaPaymentStatus(**common),
        settings=payment_settings,
    )
    fixed_for_force = min(
        state.stamina_consumption_down_fixed,
        force_before_fixed,
    )
    effective_force_cost = max(0, force_before_fixed - fixed_for_force)
    effective_stamina_cost = calculate_effective_stamina_cost(
        card.stamina_cost,
        status=StaminaPaymentStatus(
            **common,
            consumption_down_fix=(
                state.stamina_consumption_down_fixed - fixed_for_force
            ),
        ),
        settings=payment_settings,
    )
    return effective_stamina_cost, effective_force_cost


def _concentration_direct_stamina(
    state: Plan3State, settings: Plan3ExamSettings
) -> int:
    """Post-cost direct HP reduction; this does not participate in block split."""

    if state.stance != STANCE_CONCENTRATION:
        return 0
    return settings.concentration_stamina_penetration[state.stance_level - 1]


def _active_plan3_card_move_effect(
    state: Plan3State,
    card: Plan3Card,
    *,
    prior_play_count: int,
    card_search_counts: Mapping[str, int] | None,
) -> Plan3Effect | None:
    effects = tuple(
        effect
        for effect in card.effects
        if effect.effect_type == EFFECT_CARD_MOVE
        and not (prior_play_count > 0 and effect.once)
    )
    if len(effects) != 1:
        return None
    effect = effects[0]
    if effect.trigger is None:
        return effect
    decision = evaluate_plan3_trigger(
        effect.trigger,
        state,
        card_search_counts=card_search_counts,
    )
    if not decision.supported:
        raise ValueError(
            "card-move-trigger-unsupported:" + ",".join(decision.unsupported_rules)
        )
    return effect if decision.fires else None


def get_plan3_card_move_candidates(
    state: Plan3State,
    card: Plan3Card,
    *,
    prior_play_count: int = 0,
    card_search_counts: Mapping[str, int] | None = None,
    card_move_searches: Mapping[str, ProduceCardSearchRule] | None = None,
) -> tuple[Plan3CardMoveCandidate, ...]:
    """Return the ordered scalar projection of the active CardMove search.

    ``UseHand`` has already made the played card logically ``Playing`` when
    the effect executor searches.  The scalar state retains that ref in Hand
    until final settlement, so Hand searches exclude it and Playing searches
    expose it through a virtual source zone.
    """

    effect = _active_plan3_card_move_effect(
        state,
        card,
        prior_play_count=prior_play_count,
        card_search_counts=card_search_counts,
    )
    if effect is None:
        return ()
    rule = effect.card_move_rule
    if rule is None or not _is_general_card_move_rule(rule):
        raise ValueError(f"card-move-rule-unsupported:{effect.id}")
    if state.stance == STANCE_FULL_POWER and rule.destination == MOVE_HOLD:
        return ()

    unplayed_hand = list(state.hand)
    unplayed_hand.remove(card.ref)
    if rule.search_id == SEARCH_HAND:
        return tuple(
            Plan3CardMoveCandidate(ref, "hand")
            for ref in unplayed_hand
        )
    if rule.search_id == SEARCH_PLAYING_SELF:
        return (Plan3CardMoveCandidate(card.ref, "playing"),)

    searches = card_move_searches or {}
    search = searches.get(rule.search_id)
    if search is None:
        raise ValueError(f"card-move-search-missing:{rule.search_id}")
    invalid_search = validate_plan3_card_move_search(search)
    if invalid_search is not None:
        raise ValueError(invalid_search)
    zone_groups = {
        "ProduceCardPositionType_Hand": (
            ("hand", tuple(unplayed_hand)),
        ),
        "ProduceCardPositionType_Deck": (("draw", state.draw_pile),),
        "ProduceCardPositionType_Grave": (("discard", state.discard_pile),),
        "ProduceCardPositionType_DeckGrave": (
            ("draw", state.draw_pile),
            ("discard", state.discard_pile),
        ),
        "ProduceCardPositionType_Hold": (("hold", state.hold_pile),),
        "ProduceCardPositionType_Playing": (("playing", (card.ref,)),),
    }.get(search.card_position_type)
    if zone_groups is None:
        raise ValueError(f"card-move-search-position:{search.id}")
    candidates: list[Plan3CardMoveCandidate] = []
    for source_zone, refs in zone_groups:
        for ref in refs:
            matches, reason = match_plan3_card_move_search(
                search, ref.card_id, ref.upgrade
            )
            if reason is not None:
                raise ValueError(reason)
            if matches:
                candidates.append(Plan3CardMoveCandidate(ref, source_zone))
    if search.limit_count > 0:
        candidates = candidates[: search.limit_count]
    return tuple(candidates)


def get_plan3_hand_hold_selection_count(
    state: Plan3State,
    card: Plan3Card,
    *,
    prior_play_count: int = 0,
    card_search_counts: Mapping[str, int] | None = None,
) -> int:
    """Return the active exact Hand -> Hold selection count for this play.

    ``UseHand`` has already removed the played GUID when the native search is
    evaluated.  Full Power rejects Hold insertion and therefore has no target
    selection command.  Unsupported rule shapes remain the responsibility of
    :func:`apply_plan3_card`, so this helper only exposes a proven active rule.
    """

    direct_effects = tuple(
        effect
        for effect in card.effects
        if not (prior_play_count > 0 and effect.once)
    )
    matches = tuple(
        effect
        for effect in direct_effects
        if effect.effect_type == EFFECT_CARD_MOVE
        and effect.card_move_rule is not None
        and _is_hand_select_hold_rule(effect.card_move_rule)
    )
    if len(matches) != 1 or state.stance == STANCE_FULL_POWER:
        return 0
    effect = matches[0]
    if effect.trigger is not None:
        decision = evaluate_plan3_trigger(
            effect.trigger,
            state,
            card_search_counts=card_search_counts,
        )
        if not decision.supported or not decision.fires:
            return 0
    assert effect.card_move_rule is not None
    return effect.card_move_rule.pick_count_min


def apply_plan3_card(
    state: Plan3State,
    card: Plan3Card,
    *,
    settings: Plan3ExamSettings | None = None,
    prior_play_count: int = 0,
    card_search_counts: Mapping[str, int] | None = None,
    card_move_selection: tuple[Plan3CardRef, ...] = (),
    card_move_selection_sources: tuple[str, ...] = (),
    card_move_searches: Mapping[str, ProduceCardSearchRule] | None = None,
    playing_guid: str = "",
    forced_replay: bool = False,
    forced_use_pool: bool = False,
    detached_use_pool: bool = False,
    use_pool_consume_cost: bool = False,
    direct_effect_repeat_count: int = 0,
    externally_handled_play_count_interval_instance_ids: tuple[str, ...] = (),
    pre_direct_transform: Plan3PreDirectTransform | None = None,
) -> Plan3Transition:
    """Apply one supported card atomically; unknown rules never mutate state.

    ``prior_play_count`` belongs to the selected GUID card instance.  The
    ordinary scalar caller defaults to a first play; the GUID-native horizon
    supplies the persisted value from LocalSave and subsequent zone moves.
    """

    if (
        not isinstance(prior_play_count, int)
        or isinstance(prior_play_count, bool)
        or prior_play_count < 0
    ):
        raise ValueError("prior_play_count must be a non-negative integer")
    if not isinstance(playing_guid, str):
        raise TypeError("playing_guid must be text")
    if not isinstance(forced_replay, bool):
        raise TypeError("forced_replay must be bool")
    if not isinstance(forced_use_pool, bool):
        raise TypeError("forced_use_pool must be bool")
    if type(detached_use_pool) is not bool or detached_use_pool and not forced_use_pool:
        raise ValueError("detached_use_pool requires a forced UsePool command")
    if not isinstance(use_pool_consume_cost, bool):
        raise TypeError("use_pool_consume_cost must be bool")
    if (
        not isinstance(direct_effect_repeat_count, int)
        or isinstance(direct_effect_repeat_count, bool)
        or direct_effect_repeat_count < 0
    ):
        raise ValueError(
            "direct_effect_repeat_count must be a non-negative integer"
        )
    if not isinstance(
        externally_handled_play_count_interval_instance_ids, tuple
    ) or not all(
        isinstance(value, str) and value
        for value in externally_handled_play_count_interval_instance_ids
    ):
        raise TypeError(
            "externally handled play-count interval IDs must be a tuple of text"
        )
    if len(externally_handled_play_count_interval_instance_ids) != len(
        set(externally_handled_play_count_interval_instance_ids)
    ):
        raise ValueError(
            "externally handled play-count interval IDs must be unique"
        )
    if pre_direct_transform is not None and not callable(pre_direct_transform):
        raise TypeError("pre_direct_transform must be callable or None")
    if forced_replay and forced_use_pool:
        raise ValueError("Encore replay and forced UsePool are mutually exclusive")
    if use_pool_consume_cost and not forced_use_pool:
        raise ValueError("UsePool cost policy requires forced_use_pool")
    if forced_replay and (
        card.id != EXACT_ENCORE_CARD_ID
        or card.upgrade not in range(4)
        or prior_play_count <= 0
        or not playing_guid
    ):
        raise ValueError("forced replay is restricted to the exact Encore card")
    if not isinstance(card_move_selection, tuple) or not all(
        isinstance(ref, Plan3CardRef) for ref in card_move_selection
    ):
        raise TypeError("card_move_selection must be a tuple of Plan3CardRef")
    if not isinstance(card_move_selection_sources, tuple) or not all(
        isinstance(value, str) and value
        for value in card_move_selection_sources
    ):
        raise TypeError("card_move_selection_sources must be a tuple of strings")
    if card_move_selection_sources and (
        len(card_move_selection_sources) != len(card_move_selection)
    ):
        raise ValueError("card_move_selection_sources must match selection length")
    state.validate()
    if settings is None:
        settings = load_plan3_exam_settings()
    direct_effects = tuple(
        effect
        for effect in card.effects
        if not (prior_play_count > 0 and effect.once)
    )
    shape_errors = list(
        _card_shape_errors(card, prior_play_count=prior_play_count)
    )
    for effect in direct_effects:
        if effect.effect_type == EFFECT_BLOCK_RESTRICTION:
            from .plan3_block_restriction import (
                EffectContext as BlockRestrictionContext,
                matches_effect_contract as matches_block_restriction,
            )

            if not matches_block_restriction(
                effect, BlockRestrictionContext.DIRECT_CARD
            ):
                shape_errors.append(
                    f"effect-context:direct-card:{effect.effect_type}:{effect.id}"
                )
    for enchant in state.active_status_enchants:
        shape_errors.extend(_enchant_shape_errors(enchant))
    shape_errors = list(_unique(shape_errors))
    if shape_errors:
        return Plan3Transition(
            state, state, card, False, False, tuple(shape_errors)
        )
    if state.parameter_bonus_percent != 0:
        # This legacy synthetic field predates the native status audit.  Its
        # source status is ambiguous, so a non-zero value must not be folded
        # into the proven formula as an invented generic percentage.
        return Plan3Transition(
            state,
            state,
            card,
            False,
            False,
            ("state:parameter-bonus-native-source-unmapped",),
        )
    if state.enthusiasm_gain_bonus_percent != 0 and (
        state.enthusiasm_multiple_statuses
        or any(
            effect.effect_type == EFFECT_ENTHUSIASTIC_MULTIPLE
            for effect in direct_effects
        )
    ):
        # The legacy percentage field has no native status-layer identity or
        # insertion order.  Combining it with proven timed layers would make
        # float32 accumulation order an unsupported guess.
        return Plan3Transition(
            state,
            state,
            card,
            False,
            False,
            ("state:enthusiasm-multiple-legacy-source-conflict",),
        )
    if (
        state.max_stamina is None
        and any(
            effect.effect_type
            in {EFFECT_STAMINA_RECOVER_FIX, EFFECT_STAMINA_REDUCE}
            for effect in direct_effects
        )
    ):
        return Plan3Transition(
            state,
            state,
            card,
            False,
            False,
            ("state:max-stamina-unknown",),
        )
    if state.awaiting_turn_start:
        return Plan3Transition(
            state,
            state,
            card,
            False,
            False,
            ("phase:awaiting-turn-start",),
        )
    if state.turns_remaining == 0 or (
        state.plays_remaining <= 0
        and not forced_replay
        and not forced_use_pool
    ):
        return Plan3Transition(state, state, card, True, False)
    source_cards = state.lost_pile if forced_replay else state.hand
    if card.ref not in source_cards:
        return Plan3Transition(state, state, card, True, False)
    if card.play_trigger is not None:
        play_gate_phase = (
            PHASE_START_TURN
            if card.play_trigger.id in _EXACT_START_TURN_CARD_PLAY_GATES
            else None
        )
        decision = evaluate_plan3_trigger(
            card.play_trigger,
            state,
            event_phase=play_gate_phase,
            card_search_counts=card_search_counts,
        )
        if not decision.supported:
            return Plan3Transition(
                state, state, card, False, False, decision.unsupported_rules
            )
        if not decision.fires:
            return Plan3Transition(state, state, card, True, False)

    try:
        active_card_move = _active_plan3_card_move_effect(
            state,
            card,
            prior_play_count=prior_play_count,
            card_search_counts=card_search_counts,
        )
        card_move_candidates = get_plan3_card_move_candidates(
            state,
            card,
            prior_play_count=prior_play_count,
            card_search_counts=card_search_counts,
            card_move_searches=card_move_searches,
        )
    except ValueError as error:
        return Plan3Transition(
            state, state, card, False, False, (str(error),)
        )
    selected_card_move_candidates: tuple[Plan3CardMoveCandidate, ...] = ()
    if active_card_move is None:
        if card_move_selection:
            return Plan3Transition(
                state,
                state,
                card,
                False,
                False,
                (f"effect-card-move-selection-inactive:{card.id}",),
            )
    else:
        assert active_card_move.card_move_rule is not None
        move_rule = active_card_move.card_move_rule
        implicit_playing = _is_playing_self_hold_rule(move_rule)
        if implicit_playing:
            if card_move_selection:
                return Plan3Transition(
                    state,
                    state,
                    card,
                    False,
                    False,
                    (f"effect-card-move-selection-playing:{card.id}",),
                )
        else:
            candidate_count = len(card_move_candidates)
            if move_rule.pick_range_type == PICK_RANGE_ALL:
                minimum = maximum = candidate_count
            else:
                minimum = min(move_rule.pick_count_min, candidate_count)
                maximum = min(move_rule.pick_count_max, candidate_count)
            if not minimum <= len(card_move_selection) <= maximum:
                expected_count = (
                    str(minimum)
                    if minimum == maximum
                    else f"{minimum}-{maximum}"
                )
                return Plan3Transition(
                    state,
                    state,
                    card,
                    False,
                    False,
                    (
                        "effect-card-move-selection-count:"
                        f"{len(card_move_selection)}:{expected_count}:{card.id}",
                    ),
                )
            pending = list(
                zip(card_move_selection, card_move_selection_sources)
                if card_move_selection_sources
                else ((ref, "") for ref in card_move_selection)
            )
            normalized: list[Plan3CardMoveCandidate] = []
            for candidate in card_move_candidates:
                match_index = next(
                    (
                        index
                        for index, (ref, source) in enumerate(pending)
                        if ref == candidate.card_ref
                        and (not source or source == candidate.source_zone)
                    ),
                    None,
                )
                if match_index is not None:
                    normalized.append(candidate)
                    pending.pop(match_index)
            if pending:
                ref, source = pending[0]
                return Plan3Transition(
                    state,
                    state,
                    card,
                    False,
                    False,
                    (
                        "effect-card-move-selection-not-in-search:"
                        f"{source}:{ref.card_id}:{card.id}",
                    ),
                )
            if tuple(candidate.card_ref for candidate in normalized) != (
                card_move_selection
            ):
                return Plan3Transition(
                    state,
                    state,
                    card,
                    False,
                    False,
                    (f"effect-card-move-selection-order:{card.id}",),
                )
            if card_move_selection_sources and tuple(
                candidate.source_zone for candidate in normalized
            ) != card_move_selection_sources:
                return Plan3Transition(
                    state,
                    state,
                    card,
                    False,
                    False,
                    (f"effect-card-move-selection-source-order:{card.id}",),
                )
            selected_card_move_candidates = tuple(normalized)

    full_power_point_cost = (
        0
        if forced_replay or (forced_use_pool and not use_pool_consume_cost)
        else card.cost_value
        if card.cost_type == COST_FULL_POWER_POINT
        else 0
    )
    if full_power_point_cost > state.full_power_points:
        return Plan3Transition(state, state, card, True, False)
    effective_stamina_cost, effective_force_stamina_cost = (
        (0, 0)
        if forced_replay or (forced_use_pool and not use_pool_consume_cost)
        else _plan3_effective_stamina_costs(card, state, settings)
    )
    block_paid = min(state.block, effective_stamina_cost)
    stamina_cost_paid = effective_stamina_cost - block_paid
    force_stamina_paid = effective_force_stamina_cost
    if stamina_cost_paid + force_stamina_paid > state.stamina:
        return Plan3Transition(
            state,
            state,
            card,
            True,
            False,
            raw_stamina_cost=card.stamina_cost,
            effective_stamina_cost=effective_stamina_cost,
            raw_force_stamina_cost=card.force_stamina_cost,
            effective_force_stamina_cost=effective_force_stamina_cost,
        )
    stamina_after_cost = (
        state.stamina - stamina_cost_paid - force_stamina_paid
    )
    direct_requested = (
        _concentration_direct_stamina(state, settings)
        if card.cost_type == COST_STAMINA
        and not forced_replay
        and (not forced_use_pool or use_pool_consume_cost)
        else 0
    )
    stamina_direct_paid = min(direct_requested, stamina_after_cost)
    stamina_paid = stamina_cost_paid + force_stamina_paid + stamina_direct_paid
    working = replace(
        state,
        block=state.block - block_paid,
        stamina=state.stamina - stamina_paid,
        full_power_points=(
            state.full_power_points - full_power_point_cost
        ),
        plays_remaining=(
            state.plays_remaining
            if forced_replay or forced_use_pool
            else state.plays_remaining - 1
        ),
        stamina_consumption_down_fresh=False,
    )
    score_gain = 0
    parameter_pre_fix_values: list[int] = []
    parameter_actual_values: list[int] = []
    parameter_clear_changed = False
    parameter_perfect_changed = False
    enthusiasm_released = 0
    extra_plays = 0
    extra_turns_added = 0
    full_power_points_gained = 0
    full_power_points_reduced = 0
    stamina_recovered = 0
    draw_count = 0
    fired_enchants: list[str] = []
    fired_effects: list[str] = []
    runtime_effect_events: list[Plan3RuntimeEffectEvent] = []
    native_add_grow_effects: list[Plan3Effect] = []
    native_deferred_effects: list[Plan3Effect] = []
    release_status_receipts: list[object] = []
    move_playing_self_to_hold = False
    applied_card_move_selection: list[Plan3CardRef] = []

    # Master play/effect eligibility is evaluated against the same snapshot
    # shown before payment and before any ordered play effect mutates fields.
    trigger_state = state
    # Native ExamCardPlay candidates are captured before payment/direct
    # effects, but their commands execute after payment.  A listener installed
    # by this card therefore cannot observe the installing card in this phase.
    working, interval_candidates, interval_errors = (
        _prepare_play_count_interval_candidates(
            working,
            externally_handled_instance_ids=(
                externally_handled_play_count_interval_instance_ids
            ),
        )
    )
    if interval_errors:
        return Plan3Transition(
            state, state, card, False, False, interval_errors
        )
    runtime_score_before = working.score
    runtime = _apply_runtime_phase(
        working,
        PHASE_CARD_PLAY,
        event_card=card,
        trigger_state=trigger_state,
        settings=settings,
    )
    if runtime.unsupported_rules:
        return Plan3Transition(
            state, state, card, False, False, runtime.unsupported_rules
        )
    working = runtime.state
    score_gain += working.score - runtime_score_before
    parameter_pre_fix_values.extend(runtime.parameter_pre_fix_values)
    parameter_actual_values.extend(runtime.parameter_actual_values)
    parameter_clear_changed = (
        parameter_clear_changed or runtime.parameter_clear_changed
    )
    parameter_perfect_changed = (
        parameter_perfect_changed or runtime.parameter_perfect_changed
    )
    enthusiasm_released += runtime.enthusiasm_released
    extra_plays += runtime.extra_plays
    full_power_points_gained += runtime.full_power_points_gained
    full_power_points_reduced += runtime.full_power_points_reduced
    stamina_recovered += runtime.stamina_recovered
    fired_enchants.extend(runtime.fired_enchant_ids)
    fired_effects.extend(runtime.fired_effect_ids)
    runtime_effect_events.extend(runtime.effect_events)

    # Android builds phase-23 candidates from the incremented listener-local
    # counters before any phase-2/direct command executes.  Execute that
    # captured list after ExamCardPlay and still before the card's own effects.
    runtime_score_before = working.score
    runtime = _apply_runtime_phase(
        working,
        PHASE_PLAY_COUNT_INTERVAL,
        event_card=card,
        trigger_state=working,
        settings=settings,
        candidate_snapshot=interval_candidates,
    )
    if runtime.unsupported_rules:
        return Plan3Transition(
            state, state, card, False, False, runtime.unsupported_rules
        )
    working = runtime.state
    score_gain += working.score - runtime_score_before
    parameter_pre_fix_values.extend(runtime.parameter_pre_fix_values)
    parameter_actual_values.extend(runtime.parameter_actual_values)
    parameter_clear_changed = (
        parameter_clear_changed or runtime.parameter_clear_changed
    )
    parameter_perfect_changed = (
        parameter_perfect_changed or runtime.parameter_perfect_changed
    )
    enthusiasm_released += runtime.enthusiasm_released
    extra_plays += runtime.extra_plays
    full_power_points_gained += runtime.full_power_points_gained
    full_power_points_reduced += runtime.full_power_points_reduced
    stamina_recovered += runtime.stamina_recovered
    fired_enchants.extend(runtime.fired_enchant_ids)
    fired_effects.extend(runtime.fired_effect_ids)
    runtime_effect_events.extend(runtime.effect_events)

    if pre_direct_transform is not None:
        try:
            transformed = pre_direct_transform(
                working,
                tuple(runtime_effect_events),
            )
        except (TypeError, ValueError, OSError, KeyError) as error:
            return Plan3Transition(
                state,
                state,
                card,
                False,
                False,
                (
                    "pre-direct-transform-failed:"
                    f"{type(error).__name__}:{error}",
                ),
            )
        if not isinstance(transformed, Plan3PreDirectTransformResult):
            raise TypeError(
                "pre_direct_transform must return "
                "Plan3PreDirectTransformResult"
            )
        if transformed.unsupported_rules:
            return Plan3Transition(
                state,
                state,
                card,
                False,
                False,
                transformed.unsupported_rules,
            )
        runtime_score_before = working.score
        working = transformed.state
        score_gain += working.score - runtime_score_before
        parameter_pre_fix_values.extend(
            transformed.parameter_pre_fix_values
        )
        parameter_actual_values.extend(
            transformed.parameter_actual_values
        )
        parameter_clear_changed = (
            parameter_clear_changed
            or transformed.parameter_clear_changed
        )
        parameter_perfect_changed = (
            parameter_perfect_changed
            or transformed.parameter_perfect_changed
        )
        fired_enchants.extend(transformed.fired_status_enchant_ids)
        fired_effects.extend(transformed.fired_effect_ids)

    direct_command_snapshots = []
    direct_effect_passes = tuple(
        direct_effects
        if pass_index == 0
        else tuple(effect for effect in direct_effects if not effect.once)
        for pass_index in range(direct_effect_repeat_count + 1)
    )
    for direct_effect_index, effect in enumerate(
        effect
        for effect_pass in direct_effect_passes
        for effect in effect_pass
    ):
        if effect.trigger is not None:
            decision = evaluate_plan3_trigger(
                effect.trigger,
                trigger_state,
                card_search_counts=card_search_counts,
            )
            if not decision.supported:
                return Plan3Transition(
                    state, state, card, False, False, decision.unsupported_rules
                )
            if not decision.fires:
                continue
        if effect.effect_type in {EFFECT_LESSON, EFFECT_LESSON_FULL_POWER_POINT}:
            from .plan3_fullpower_point_gimmick import following_lesson_command_snapshot
            try:
                snapshot = following_lesson_command_snapshot(effect, direct_effect_index, runtime_effect_events)
            except (TypeError, ValueError) as error:
                return Plan3Transition(state, state, card, False, False, (str(error),))
            if snapshot is not None:
                direct_command_snapshots.append(snapshot)
        if effect.effect_type == EFFECT_PRESERVATION:
            if working.stance == STANCE_FULL_POWER:
                # Official HelpContent: no other policy can replace Full Power
                # during the active Full Power turn.  The rest of the card is
                # still resolved normally.
                continue
            stance_result = _change_stance(
                working, STANCE_PRESERVATION, effect.value1, settings
            )
            working = stance_result.state
            enthusiasm_released += stance_result.enthusiasm_released
            extra_plays += stance_result.extra_plays
            release_status_receipts.extend(
                stance_result.release_status_receipts
            )
        elif effect.effect_type == EFFECT_CONCENTRATION:
            if working.stance == STANCE_FULL_POWER:
                continue
            stance_result = _change_stance(
                working, STANCE_CONCENTRATION, effect.value1, settings
            )
            working = stance_result.state
            enthusiasm_released += stance_result.enthusiasm_released
            extra_plays += stance_result.extra_plays
            release_status_receipts.extend(
                stance_result.release_status_receipts
            )
            if not stance_result.changed:
                continue
            # Resolve after release and after the stance field is committed.
            # Therefore a newly gained additive cannot retroactively increase
            # this transition's already calculated release.
            runtime_score_before = working.score
            runtime = _apply_runtime_phase(
                working,
                PHASE_STANCE_CHANGE_CONCENTRATION,
                settings=settings,
            )
            if runtime.unsupported_rules:
                return Plan3Transition(
                    state,
                    state,
                    card,
                    False,
                    False,
                    runtime.unsupported_rules,
                )
            working = runtime.state
            score_gain += working.score - runtime_score_before
            parameter_pre_fix_values.extend(
                runtime.parameter_pre_fix_values
            )
            parameter_actual_values.extend(
                runtime.parameter_actual_values
            )
            parameter_clear_changed = (
                parameter_clear_changed or runtime.parameter_clear_changed
            )
            parameter_perfect_changed = (
                parameter_perfect_changed
                or runtime.parameter_perfect_changed
            )
            enthusiasm_released += runtime.enthusiasm_released
            extra_plays += runtime.extra_plays
            full_power_points_gained += runtime.full_power_points_gained
            full_power_points_reduced += runtime.full_power_points_reduced
            stamina_recovered += runtime.stamina_recovered
            fired_enchants.extend(runtime.fired_enchant_ids)
            fired_effects.extend(runtime.fired_effect_ids)
            runtime_effect_events.extend(runtime.effect_events)
        elif effect.effect_type == EFFECT_BLOCK:
            working = _add_plan3_block(working, effect.value1)
        elif effect.effect_type == EFFECT_BLOCK_FIX:
            # Unlike ordinary Block, the fixed executor calls AddBlock
            # directly and remains effective during BlockRestriction.
            working = replace(working, block=working.block + effect.value1)
        elif effect.effect_type == EFFECT_BLOCK_RESTRICTION:
            working = _apply_block_restriction_effect(
                working,
                effect,
                direct_card=True,
            )
        elif effect.effect_type == EFFECT_STAMINA_RECOVER_FIX:
            # Preflight above proves the cap is authoritative before any cost
            # or ordered effect is committed.
            assert working.max_stamina is not None
            recovered = min(
                effect.value1,
                max(0, working.max_stamina - working.stamina),
            )
            working = replace(working, stamina=working.stamina + recovered)
            stamina_recovered += recovered
        elif effect.effect_type == EFFECT_STAMINA_REDUCE:
            # The only admitted row is the exact 1000-permille legend effect;
            # native derives it from MaxStamina and therefore reaches zero.
            assert working.max_stamina is not None
            working = replace(working, stamina=0)
        elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN:
            working = _add_stamina_consumption_down_status(
                working,
                turns=effect.effect_turn,
            )
        elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN_FIX:
            working = _add_stamina_consumption_down_fix_status(
                working,
                value=effect.value1,
                turns=effect.effect_turn,
            )
        elif effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
            working = replace(
                working,
                plays_remaining=working.plays_remaining + effect.effect_count,
            )
            extra_plays += effect.effect_count
        elif effect.effect_type == EFFECT_CARD_SEARCH_PLAY_COUNT_BUFF:
            from .plan3_card_search_effect_play_count_buff import (
                install_card_search_effect_play_count_buff,
                load_card_search_effect_play_count_buff_contract,
            )

            installed = install_card_search_effect_play_count_buff(
                working.play_count_buff_runtime,
                load_card_search_effect_play_count_buff_contract(),
                native_status_add_blocked=False,
            )
            if not installed.accepted:
                return Plan3Transition(
                    state,
                    state,
                    card,
                    False,
                    False,
                    (f"effect-play-count-buff-blocked:{effect.id}",),
                )
            working = replace(
                working, play_count_buff_runtime=installed.after
            )
        elif effect.effect_type == EFFECT_CARD_DRAW:
            # The scalar kernel records the exact ordered draw command.  GUID
            # zones, Grave recycling, native RNG and HandAdd support upgrades
            # are resolved by plan3_native_search before the played card is
            # settled, matching the game's command order.
            draw_count += effect.value1
        elif effect.effect_type == EFFECT_EXTRA_TURN:
            from .plan3_men100016 import matches_extra_turn_effect

            if not matches_extra_turn_effect(effect):
                return Plan3Transition(
                    state,
                    state,
                    card,
                    False,
                    False,
                    (f"effect-extra-turn-shape:{effect.id}",),
                )
            # ExtraTurnEffectExecutor commits RemainTurn first and the
            # separate ExtraTurn counter second.  The scheduler's ordinary
            # decrement remains at end_plan3_turn.
            working = replace(
                working,
                turns_remaining=working.turns_remaining + 1,
                extra_turn=working.extra_turn + 1,
            )
            extra_turns_added += 1
        elif effect.effect_type == EFFECT_ADD_GROW:
            # The scalar state has no card-instance growth layer.  Preserve
            # this exact ordered command for the GUID-native boundary.
            native_add_grow_effects.append(effect)
        elif effect.effect_type in {
            EFFECT_SEARCH_PLAY_CARD_STAMINA_CHANGE,
            EFFECT_HAND_GRAVE_COUNT_CARD_DRAW,
            EFFECT_CARD_UPGRADE,
            EFFECT_CARD_CREATE_ID,
            EFFECT_CARD_CREATE_SEARCH,
            EFFECT_ANTI_DEBUFF,
            EFFECT_FORCE_PLAY_CARD_SEARCH,
            EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST,
        }:
            # Preserve the exact effect slot for the GUID-native executor.
            native_deferred_effects.append(effect)
        elif effect.effect_type == EFFECT_ENTHUSIASTIC_ADDITIVE:
            working = apply_plan3_enthusiasm_additive_status(
                working,
                value=effect.value1,
                turns=effect.effect_turn,
                source_effect_id=effect.id,
            )
        elif effect.effect_type == EFFECT_ENTHUSIASTIC_MULTIPLE:
            working = _add_enthusiasm_multiple_status(
                working,
                value_permille=effect.value1,
                turns=effect.effect_turn,
            )
        elif effect.effect_type == EFFECT_FULL_POWER_POINT:
            working, gained = _add_full_power_points(
                working, effect.value1
            )
            full_power_points_gained += gained
            if gained > 0:
                runtime_score_before = working.score
                runtime = _apply_runtime_phase(
                    working,
                    PHASE_STATUS_CHANGE,
                    trigger_state=working,
                    settings=settings,
                    event_effect_type=EFFECT_FULL_POWER_POINT,
                    effect_result_trigger_enabled=True,
                )
                if runtime.unsupported_rules:
                    return Plan3Transition(
                        state,
                        state,
                        card,
                        False,
                        False,
                        runtime.unsupported_rules,
                    )
                working = runtime.state
                score_gain += working.score - runtime_score_before
                parameter_pre_fix_values.extend(
                    runtime.parameter_pre_fix_values
                )
                parameter_actual_values.extend(
                    runtime.parameter_actual_values
                )
                parameter_clear_changed = (
                    parameter_clear_changed
                    or runtime.parameter_clear_changed
                )
                parameter_perfect_changed = (
                    parameter_perfect_changed
                    or runtime.parameter_perfect_changed
                )
                enthusiasm_released += runtime.enthusiasm_released
                extra_plays += runtime.extra_plays
                full_power_points_gained += runtime.full_power_points_gained
                full_power_points_reduced += runtime.full_power_points_reduced
                stamina_recovered += runtime.stamina_recovered
                fired_enchants.extend(runtime.fired_enchant_ids)
                fired_effects.extend(runtime.fired_effect_ids)
                runtime_effect_events.extend(
                    replace(event, source_direct_effect_index=direct_effect_index)
                    for event in runtime.effect_events
                )
        elif effect.effect_type == EFFECT_FULL_POWER_POINT_ADDITIVE:
            working = _install_full_power_point_additive(working, effect)
        elif effect.effect_type == EFFECT_FULL_POWER_POINT_REDUCE:
            reduced = min(working.full_power_points, effect.value1)
            working = replace(
                working,
                full_power_points=working.full_power_points - reduced,
            )
            full_power_points_reduced += reduced
        elif effect.effect_type == EFFECT_STANCE_LOCK:
            from .plan3_stance_lock import execute_standalone_stance_lock

            lock_result = execute_standalone_stance_lock(
                effect, working.stance_lock_runtime
            )
            if not lock_result.executable:
                return Plan3Transition(
                    state,
                    state,
                    card,
                    False,
                    False,
                    (f"effect-stance-lock-runtime:{effect.id}",),
                )
            working = replace(
                working,
                stance_lock_runtime=lock_result.runtime_after,
            )
        elif effect.effect_type == EFFECT_OVER_PRESERVATION:
            stance_result, over_errors = _apply_over_preservation(working)
            if over_errors:
                return Plan3Transition(
                    state, state, card, False, False, over_errors
                )
            working = stance_result.state
        elif effect.effect_type == EFFECT_STATUS_ENCHANT:
            working = _install_card_status_enchant(
                working,
                effect,
                source_id=card.id,
            )
        elif effect.effect_type == EFFECT_STATUS_ENCHANT_ENCORE:
            try:
                working = _install_exact_encore_status_enchant(
                    working,
                    effect,
                    source_id=card.id,
                    playing_guid=playing_guid,
                )
            except ValueError as error:
                return Plan3Transition(
                    state, state, card, False, False, (str(error),)
                )
        elif effect.effect_type == EFFECT_TIMER:
            # Android v3.2.3 stores the chain in a fresh one-use
            # TriggerEffectStatusEffect.  It does not execute the child at the
            # installing card's effect slot.
            working = _install_effect_timer(
                working,
                effect,
                source_id=card.id,
            )
        elif effect.effect_type == EFFECT_CARD_MOVE:
            assert effect.card_move_rule is not None
            if _is_playing_self_hold_rule(effect.card_move_rule):
                # Hold rejects additions during Full Power.  Outside Full
                # Power, playing/is_self overrides the played card's ordinary
                # final Grave destination after ordered effects settle.
                move_playing_self_to_hold = (
                    working.stance != STANCE_FULL_POWER
                )
            elif not (
                working.stance == STANCE_FULL_POWER
                and effect.card_move_rule.destination == MOVE_HOLD
            ):
                # The GUID-native layer supplied exact ordered source zones.
                # Apply the scalar projection at this effect slot so a later
                # CardDraw observes the post-move Deck/Grave state.
                try:
                    for candidate in selected_card_move_candidates:
                        working = _move_plan3_card_candidate(
                            working,
                            candidate,
                            destination=effect.card_move_rule.destination,
                            settings=settings,
                        )
                        applied_card_move_selection.append(candidate.card_ref)
                except ValueError as error:
                    return Plan3Transition(
                        state, state, card, False, False, (str(error),)
                    )
        elif effect.effect_type == EFFECT_MULTIPLE_ENTHUSIASTIC_LESSON:
            from .plan3_encore_enthusiastic_lesson import (
                MultipleEnthusiasticLessonInput,
                execute_multiple_enthusiastic_lesson,
                resolve_multiple_enthusiastic_lesson,
            )

            try:
                contract = resolve_multiple_enthusiastic_lesson(effect)
            except ValueError as error:
                return Plan3Transition(
                    state, state, card, False, False, (str(error),)
                )
            execution = execute_multiple_enthusiastic_lesson(
                contract,
                MultipleEnthusiasticLessonInput(
                    _plan3_adding_parameter_status(working),
                    _adding_parameter_settings(settings),
                    ParameterApplicationStatus(
                        judge_parameter=working.score,
                        limit_border=working.limit_border,
                        clear_border=working.clear_border,
                        current_turn_total_add_parameter=(
                            working.current_turn_total_add_parameter
                        ),
                        slump=working.slump,
                        is_battle=working.is_battle,
                        current_parameter_type=ProduceParameterType(
                            working.current_parameter_type
                        ),
                        battle_bonus_permille_vocal=(
                            working.battle_bonus_permille_vocal
                        ),
                        battle_bonus_permille_dance=(
                            working.battle_bonus_permille_dance
                        ),
                        battle_bonus_permille_visual=(
                            working.battle_bonus_permille_visual
                        ),
                        judge_parameter_vocal=working.judge_parameter_vocal,
                        judge_parameter_dance=working.judge_parameter_dance,
                        judge_parameter_visual=working.judge_parameter_visual,
                    ),
                ),
            )
            if not execution.resolved:
                return Plan3Transition(
                    state,
                    state,
                    card,
                    False,
                    False,
                    (f"multiple-enthusiastic-runtime:{execution.reason}",),
                )
            for application in execution.applications:
                score_gain += application.after - application.before
                parameter_pre_fix_values.append(
                    application.pre_fix_parameter
                )
                parameter_actual_values.append(application.actual_parameter)
                parameter_clear_changed = (
                    parameter_clear_changed or application.clear_changed
                )
                parameter_perfect_changed = (
                    parameter_perfect_changed or application.perfect_changed
                )
            working = replace(
                working,
                score=execution.after.judge_parameter,
                current_turn_total_add_parameter=(
                    execution.after.current_turn_total_add_parameter
                ),
                judge_parameter_vocal=execution.after.judge_parameter_vocal,
                judge_parameter_dance=execution.after.judge_parameter_dance,
                judge_parameter_visual=execution.after.judge_parameter_visual,
            )
        elif effect.effect_type in (
            EFFECT_LESSON,
            EFFECT_LESSON_FULL_POWER_POINT,
        ):
            lesson_value = effect.value1
            if effect.effect_type == EFFECT_LESSON_FULL_POWER_POINT:
                # Android v3.2.3 LessonFullPowerPointEffectExecutor converts
                # Value2 to a float32 permille factor, multiplies the current
                # cumulative gauge, floors it, then adds Value1 for every hit.
                lesson_value += get_ratio_effect_int_value(
                    working.full_power_points_total,
                    effect.value2,
                    is_ceil=False,
                )
            for _ in range(effect.effect_count):
                working, application = _apply_plan3_lesson_hit(
                    working, lesson_value, settings
                )
                gained = application.after - application.before
                score_gain += gained
                parameter_pre_fix_values.append(application.pre_fix_parameter)
                parameter_actual_values.append(application.actual_parameter)
                parameter_clear_changed = (
                    parameter_clear_changed or application.clear_changed
                )
                parameter_perfect_changed = (
                    parameter_perfect_changed or application.perfect_changed
                )
        else:
            # Preflight should make this unreachable; remain fail-closed.
            return Plan3Transition(
                state,
                state,
                card,
                False,
                False,
                (f"effect-type:{effect.effect_type}:{effect.id}",),
            )

    moved_to: str
    if detached_use_pool:
        if move_playing_self_to_hold:
            return Plan3Transition(state,state,card,False,False,
                ("detached-use-pool-explicit-self-move-unmodelled",))
        # RandomPool supplied a detached temporary card. It is dropped before
        # CardPlayAfter, rather than becoming an extra Grave/Lost search target.
        hand = list(working.hand)
        hand.remove(card.ref)
        working = replace(working, hand=tuple(hand))
        moved_to = "detached"
    elif move_playing_self_to_hold:
        if forced_replay:
            return Plan3Transition(
                state,
                state,
                card,
                False,
                False,
                ("exact-encore-forced-replay-hold-unsupported",),
            )
        if len(working.hold_pile) > settings.hold_limit:
            return Plan3Transition(
                state,
                state,
                card,
                False,
                False,
                (
                    "effect-card-move-hold-limit:"
                    f"{len(working.hold_pile)}:{settings.hold_limit}",
                ),
            )
        working = move_plan3_card_to_hold(
            working,
            card.ref,
            source_zone="hand",
            settings=settings,
        )
        moved_to = "hold"
    elif card.move_position_type == MOVE_LOST:
        if forced_replay:
            lost = list(working.lost_pile)
            lost.remove(card.ref)
            working = replace(
                working,
                lost_pile=tuple((*lost, card.ref)),
            )
        else:
            hand = list(working.hand)
            hand.remove(card.ref)
            working = replace(
                working,
                hand=tuple(hand),
                lost_pile=tuple((*working.lost_pile, card.ref)),
            )
        moved_to = "lost"
    elif card.move_position_type == MOVE_GRAVE:
        if forced_replay:
            lost = list(working.lost_pile)
            lost.remove(card.ref)
            working = replace(
                working,
                lost_pile=tuple(lost),
                discard_pile=tuple((*working.discard_pile, card.ref)),
            )
        else:
            hand = list(working.hand)
            hand.remove(card.ref)
            working = replace(
                working,
                hand=tuple(hand),
                discard_pile=tuple((*working.discard_pile, card.ref)),
            )
        moved_to = "discard"
    else:
        return Plan3Transition(
            state,
            state,
            card,
            False,
            False,
            (f"move-position:{card.move_position_type}",),
        )
    # Native UserCardAfter schedules CardPlayAfter only after the card's own
    # ordered effects and final zone move have settled.
    runtime_score_before = working.score
    runtime = _apply_runtime_phase(
        working,
        PHASE_CARD_PLAY_AFTER,
        event_card=card,
        settings=settings,
    )
    if runtime.unsupported_rules:
        return Plan3Transition(
            state, state, card, False, False, runtime.unsupported_rules
        )
    working = runtime.state
    score_gain += working.score - runtime_score_before
    parameter_pre_fix_values.extend(runtime.parameter_pre_fix_values)
    parameter_actual_values.extend(runtime.parameter_actual_values)
    parameter_clear_changed = (
        parameter_clear_changed or runtime.parameter_clear_changed
    )
    parameter_perfect_changed = (
        parameter_perfect_changed or runtime.parameter_perfect_changed
    )
    enthusiasm_released += runtime.enthusiasm_released
    extra_plays += runtime.extra_plays
    full_power_points_gained += runtime.full_power_points_gained
    full_power_points_reduced += runtime.full_power_points_reduced
    stamina_recovered += runtime.stamina_recovered
    fired_enchants.extend(runtime.fired_enchant_ids)
    fired_effects.extend(runtime.fired_effect_ids)
    runtime_effect_events.extend(runtime.effect_events)
    if working.play_history is not None:
        working = replace(working, play_history=working.play_history.append_completed(
            card_id=card.id, upgrade=card.upgrade, category=card.category, guid=playing_guid,
            command_type=4 if forced_replay or forced_use_pool else 2,
        ))
    return Plan3Transition(
        before=state,
        after=working,
        card=card,
        supported=True,
        legal=True,
        block_paid=block_paid,
        stamina_paid=stamina_paid,
        force_stamina_paid=force_stamina_paid,
        stamina_penetration_paid=0,
        stamina_direct_paid=stamina_direct_paid,
        raw_stamina_cost=card.stamina_cost,
        effective_stamina_cost=effective_stamina_cost,
        raw_force_stamina_cost=card.force_stamina_cost,
        effective_force_stamina_cost=effective_force_stamina_cost,
        score_gain=score_gain,
        parameter_pre_fix_values=tuple(parameter_pre_fix_values),
        parameter_actual_values=tuple(parameter_actual_values),
        parameter_clear_changed=parameter_clear_changed,
        parameter_perfect_changed=parameter_perfect_changed,
        enthusiasm_released=enthusiasm_released,
        extra_plays=extra_plays,
        extra_turns_added=extra_turns_added,
        full_power_points_gained=full_power_points_gained,
        full_power_points_reduced=full_power_points_reduced,
        stamina_recovered=stamina_recovered,
        draw_count=draw_count,
        fired_status_enchant_ids=tuple(fired_enchants),
        fired_status_effect_ids=tuple(fired_effects),
        moved_to=moved_to,
        card_move_selected_refs=tuple(applied_card_move_selection),
        native_add_grow_effects=tuple(native_add_grow_effects),
        native_deferred_effects=tuple(native_deferred_effects),
        runtime_effect_events=tuple(
            replace(event, sequence_index=index)
            for index, event in enumerate(runtime_effect_events)
        ),
        release_status_receipts=tuple(release_status_receipts),
        direct_command_snapshots=tuple(direct_command_snapshots),
    )


def _move_plan3_card_candidate(
    state: Plan3State,
    candidate: Plan3CardMoveCandidate,
    *,
    destination: str,
    settings: Plan3ExamSettings,
) -> Plan3State:
    if destination not in _CARD_MOVE_DESTINATIONS:
        raise ValueError(f"effect-card-move-destination:{destination}")
    if candidate.source_zone == "playing":
        raise ValueError("effect-card-move-playing-destination-unmodelled")
    if destination == MOVE_HOLD and candidate.source_zone in {
        "hand",
        "draw",
        "discard",
    }:
        return move_plan3_card_to_hold(
            state,
            candidate.card_ref,
            source_zone=candidate.source_zone,
            settings=settings,
        )

    zone_fields = {
        "hand": "hand",
        "draw": "draw_pile",
        "discard": "discard_pile",
        "lost": "lost_pile",
        "hold": "hold_pile",
    }
    source_field = zone_fields.get(candidate.source_zone)
    if source_field is None:
        raise ValueError(
            f"effect-card-move-source:{candidate.source_zone}"
        )
    source = list(getattr(state, source_field))
    if candidate.card_ref not in source:
        raise ValueError(
            "effect-card-move-source-card-missing:"
            f"{candidate.source_zone}:{candidate.card_ref.card_id}"
        )
    source.remove(candidate.card_ref)
    changes: dict[str, object] = {source_field: tuple(source)}
    if destination == MOVE_HAND:
        hand = source if source_field == "hand" else list(state.hand)
        if len(hand) >= settings.hand_limit:
            raise ValueError(
                f"effect-card-move-hand-limit:{len(hand)}:{settings.hand_limit}"
            )
        hand.append(candidate.card_ref)
        changes["hand"] = tuple(hand)
    elif destination == MOVE_GRAVE:
        grave = (
            source if source_field == "discard_pile" else list(state.discard_pile)
        )
        grave.append(candidate.card_ref)
        changes["discard_pile"] = tuple(grave)
    elif destination == MOVE_LOST:
        lost = source if source_field == "lost_pile" else list(state.lost_pile)
        lost.append(candidate.card_ref)
        changes["lost_pile"] = tuple(lost)
    else:
        hold = source if source_field == "hold_pile" else list(state.hold_pile)
        hold.append(candidate.card_ref)
        grave = list(state.discard_pile)
        while len(hold) > settings.hold_limit:
            grave.append(hold.pop(0))
        changes["hold_pile"] = tuple(hold)
        changes["discard_pile"] = tuple(grave)
    return replace(state, **changes)


def move_plan3_card_to_hold(
    state: Plan3State,
    card: Plan3CardRef,
    *,
    source_zone: str,
    settings: Plan3ExamSettings | None = None,
) -> Plan3State:
    """Move an explicitly selected card to Hold and evict the oldest overflow.

    The caller must identify the source zone; the kernel never guesses which
    duplicate card a selection referred to.
    """

    state.validate(allow_completed=True)
    if settings is None:
        settings = load_plan3_exam_settings()
    if state.awaiting_turn_start:
        raise ValueError("cannot move a card to Hold while awaiting turn start")
    if state.stance == STANCE_FULL_POWER:
        raise ValueError("cannot add a card to Hold during Full Power")
    if len(state.hold_pile) > settings.hold_limit:
        raise ValueError("existing Hold zone already exceeds its Master limit")
    zone_fields = {
        "hand": "hand",
        "draw": "draw_pile",
        "discard": "discard_pile",
    }
    field_name = zone_fields.get(source_zone)
    if field_name is None:
        raise ValueError(f"unsupported Hold source zone: {source_zone}")
    source = list(getattr(state, field_name))
    if card not in source:
        raise ValueError(f"card is not present in {source_zone}: {card.card_id}")
    source.remove(card)
    hold = list(state.hold_pile)
    discard = list(state.discard_pile)
    hold.append(card)
    while len(hold) > settings.hold_limit:
        discard.append(hold.pop(0))
    changes: dict[str, object] = {
        field_name: tuple(source),
        "hold_pile": tuple(hold),
    }
    # If the selected source was discard, ``source`` is the authoritative
    # post-removal discard base before the oldest Hold card is appended.
    if field_name == "discard_pile":
        discard = source
        if len(state.hold_pile) >= settings.hold_limit:
            discard = [*source, state.hold_pile[0]]
    changes["discard_pile"] = tuple(discard)
    return replace(state, **changes)


def _remove_drawn_cards(
    state: Plan3State, cards: tuple[Plan3CardRef, ...]
) -> tuple[tuple[Plan3CardRef, ...], tuple[Plan3CardRef, ...], tuple[str, ...]]:
    draw = list(state.draw_pile)
    discard = list(state.discard_pile)
    errors: list[str] = []
    for card in cards:
        if card in draw:
            draw.remove(card)
        elif card in discard:
            discard.remove(card)
        else:
            errors.append(f"turn-start-card-not-in-known-zones:{card.card_id}")
    return tuple(draw), tuple(discard), _unique(errors)


@dataclass(frozen=True, slots=True)
class _Plan3GimmickResolution:
    state: Plan3State
    fired_effect_ids: tuple[str, ...] = ()
    stamina_paid: int = 0
    stamina_recovered: int = 0
    unsupported_rules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan3ExternalTurnStartGimmickResolution:
    """Pure extension result at the native post-Full-Power/pre-draw boundary."""

    state: Plan3State
    fired_effect_ids: tuple[str, ...] = ()
    stamina_paid: int = 0
    stamina_recovered: int = 0
    unsupported_rules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, Plan3State):
            raise TypeError("state must be Plan3State")
        if any(
            not isinstance(value, str) or not value
            for value in (*self.fired_effect_ids, *self.unsupported_rules)
        ):
            raise TypeError("effect IDs and unsupported rules must be text")
        for value, label in (
            (self.stamina_paid, "stamina_paid"),
            (self.stamina_recovered, "stamina_recovered"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TypeError(f"{label} must be a non-negative integer")


def _plan3_adding_parameter_status(state: Plan3State) -> AddingParameterStatus:
    """Bind every Plan 3 lesson executor to the active native multiplier."""

    from .nia_lesson_value_multiple import bind_adding_parameter_status

    return bind_adding_parameter_status(
        AddingParameterStatus(
            slump=state.slump,
            enthusiastic_buff=state.enthusiasm,
            idol_status_type=_native_idol_status(state),
            idol_status_step=max(1, state.stance_level),
        ),
        state.lesson_parameter_multiple_state,
    )


def _apply_plan3_lesson_hit(
    state: Plan3State,
    lesson_value: int,
    settings: Plan3ExamSettings,
) -> tuple[Plan3State, Any]:
    """Apply one native lesson-parameter hit from any executor source."""

    hit = calculate_adding_parameter(
        lesson_value,
        is_buff_active=True,
        status=_plan3_adding_parameter_status(state),
        settings=_adding_parameter_settings(settings),
    )
    application = apply_parameter_add(
        hit,
        status=ParameterApplicationStatus(
            judge_parameter=state.score,
            limit_border=state.limit_border,
            clear_border=state.clear_border,
            current_turn_total_add_parameter=state.current_turn_total_add_parameter,
            slump=state.slump,
            is_battle=state.is_battle,
            current_parameter_type=ProduceParameterType(state.current_parameter_type),
            battle_bonus_permille_vocal=state.battle_bonus_permille_vocal,
            battle_bonus_permille_dance=state.battle_bonus_permille_dance,
            battle_bonus_permille_visual=state.battle_bonus_permille_visual,
            judge_parameter_vocal=state.judge_parameter_vocal,
            judge_parameter_dance=state.judge_parameter_dance,
            judge_parameter_visual=state.judge_parameter_visual,
        ),
    )
    return (
        replace(
            state,
            score=application.after,
            current_turn_total_add_parameter=(
                application.current_turn_total_add_parameter
            ),
            judge_parameter_vocal=application.judge_parameter_vocal,
            judge_parameter_dance=application.judge_parameter_dance,
            judge_parameter_visual=application.judge_parameter_visual,
        ),
        application,
    )


def _resolve_plan3_turn_start_gimmick(
    profile: Plan3GimmickProfile,
    state: Plan3State,
    settings: Plan3ExamSettings,
) -> _Plan3GimmickResolution:
    working = state
    fired: list[str] = []
    stamina_paid = 0
    stamina_recovered = 0
    for step in profile.steps:
        if step.start_turn != state.round_number:
            continue
        if step.remaining_turn_permille != 0:
            return _Plan3GimmickResolution(
                state,
                unsupported_rules=(
                    f"gimmick-remaining-turn:{profile.id}:{step.priority}",
                ),
            )
        condition: bool
        if step.field_status_type == FIELD_UNKNOWN:
            # NIA's empty Unknown/0/Unknown condition is unconditional.  A
            # Not check is still a valid native condition and simply
            # inverts that true base result.
            if step.field_status_check_type == CHECK_UNKNOWN:
                condition = True
            elif step.field_status_check_type == CHECK_NOT:
                condition = False
            else:
                return _Plan3GimmickResolution(
                    state,
                    unsupported_rules=(
                        f"gimmick-condition:{profile.id}:{step.priority}",
                    ),
                )
        else:
            counter_values = {
                FIELD_CONCENTRATION_CHANGE_COUNT_UP: (
                    working.concentration_change_count
                ),
                FIELD_PRESERVATION_CHANGE_COUNT_UP: (
                    working.preservation_change_count
                ),
                FIELD_FULL_POWER_CHANGE_COUNT_UP: (
                    working.full_power_change_count
                ),
                FIELD_FULL_POWER_POINT_GET_SUM_UP: (
                    working.full_power_points_total
                ),
            }
            current_counter = counter_values.get(step.field_status_type)
            if current_counter is not None and step.field_status_check_type in (
                CHECK_UNKNOWN,
                CHECK_NOT,
            ):
                # IsFieldStatusTriggerStatusEffect compares the counter with
                # >=; TriggerCheckType.Not inverts the result.  Do not
                # special-case an idol/card: this is the shared NIA field
                # contract used by every Plan 3 gimmick group.
                condition = current_counter >= step.field_status_value
                if step.field_status_check_type == CHECK_NOT:
                    condition = not condition
            elif (
                step.field_status_type == FIELD_CONCENTRATION_UP
                and step.field_status_check_type == CHECK_UNKNOWN
                and step.field_status_value == 0
            ):
                condition = working.stance == STANCE_CONCENTRATION
            else:
                return _Plan3GimmickResolution(
                    state,
                    unsupported_rules=(
                        f"gimmick-condition:{profile.id}:{step.priority}",
                    ),
                )
        if not condition:
            continue
        effect = step.effect
        errors = _effect_shape_errors(effect)
        if errors:
            return _Plan3GimmickResolution(state, unsupported_rules=errors)
        if effect.effect_type == EFFECT_BLOCK:
            working = _add_plan3_block(working, effect.value1)
        elif effect.effect_type == EFFECT_BLOCK_FIX:
            working = replace(working, block=working.block + effect.value1)
        elif effect.effect_type == EFFECT_BLOCK_RESTRICTION:
            from .plan3_block_restriction import (
                EffectContext as BlockRestrictionContext,
                matches_effect_contract as matches_block_restriction,
            )
            if not matches_block_restriction(
                effect, BlockRestrictionContext.TURN_START_GIMMICK
            ):
                return _Plan3GimmickResolution(
                    state,
                    unsupported_rules=(
                        f"effect-context:turn-start-gimmick:"
                        f"{effect.effect_type}:{effect.id}",
                    ),
                )
            working = _apply_block_restriction_effect(
                working,
                effect,
                direct_card=False,
            )
        elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN:
            # TurnStart spends already-passing statuses before the gimmick is
            # applied. A status created here is itself passing for this turn,
            # so the end-of-turn approximation must spend it immediately.
            # This makes a five-turn layer fired on turn 2 apply to turns 2..6,
            # rather than incorrectly surviving through turn 7.
            working = replace(
                working,
                stamina_consumption_down_turns=(
                    working.stamina_consumption_down_turns
                    + effect.effect_turn
                ),
                stamina_consumption_down_fresh=False,
            )
        elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN_FIX:
            # Match the ordinary turn-start status lifecycle: this layer is
            # already present for the current turn and is spent at its end.
            working = _add_stamina_consumption_down_fix_status(
                working,
                value=effect.value1,
                turns=effect.effect_turn,
                fresh=False,
            )
        elif effect.effect_type == EFFECT_STAMINA_REDUCE_FIX:
            paid = min(working.stamina, effect.value1)
            stamina_paid += paid
            working = replace(working, stamina=working.stamina - paid)
        elif effect.effect_type == EFFECT_STAMINA_RECOVER_FIX:
            if working.max_stamina is None:
                return _Plan3GimmickResolution(
                    state,
                    unsupported_rules=("state:max-stamina-unknown",),
                )
            recovered = min(
                effect.value1, max(0, working.max_stamina - working.stamina)
            )
            stamina_recovered += recovered
            working = replace(working, stamina=working.stamina + recovered)
        elif effect.effect_type == EFFECT_LESSON:
            for _ in range(effect.effect_count):
                working, _application = _apply_plan3_lesson_hit(
                    working, effect.value1, settings
                )
        elif effect.effect_type == EFFECT_LESSON_VALUE_MULTIPLE:
            from .nia_lesson_value_multiple import (
                install_lesson_value_multiple,
                matches_lesson_value_multiple_effect_shape,
            )

            if not matches_lesson_value_multiple_effect_shape(effect):
                return _Plan3GimmickResolution(
                    state,
                    unsupported_rules=(
                        f"effect-lesson-value-multiple-shape:{effect.id}",
                    ),
                )
            installed = install_lesson_value_multiple(
                working.lesson_parameter_multiple_state,
                permil=effect.value1,
                turn=effect.effect_turn,
            )
            if not installed.installed:
                return _Plan3GimmickResolution(
                    state,
                    unsupported_rules=(
                        f"effect-lesson-value-multiple-blocked:{effect.id}",
                    ),
                )
            multiplier_state = installed.state
            if installed.merged_index is None:
                working, status_uid = allocate_plan3_status_uid(working)
                statuses = list(multiplier_state.statuses)
                statuses[-1] = replace(statuses[-1], uid=status_uid)
                multiplier_state = type(multiplier_state)(tuple(statuses))
            working = replace(
                working,
                lesson_parameter_multiple_state=multiplier_state,
            )
        else:
            return _Plan3GimmickResolution(
                state,
                unsupported_rules=(
                    f"gimmick-effect:{effect.effect_type}:{effect.id}",
                ),
            )
        fired.append(effect.id)
    return _Plan3GimmickResolution(
        working,
        tuple(fired),
        stamina_paid,
        stamina_recovered,
    )


def start_plan3_turn(
    state: Plan3State,
    *,
    authoritative_draw_order: tuple[Plan3CardRef, ...] | None = None,
    observed_hand: tuple[Plan3CardRef, ...] | None = None,
    settings: Plan3ExamSettings | None = None,
    gimmick_profile: Plan3GimmickProfile | None = None,
    external_gimmick_resolver: Callable[
        [Plan3State, Plan3ExamSettings],
        Plan3ExternalTurnStartGimmickResolution,
    ]
    | None = None,
) -> Plan3TurnStart:
    """Resolve one official Plan 3 turn start without inventing draw order.

    Exactly one evidence source may be supplied: a known draw order, or the
    settled hand observed by the live controller.  Missing evidence blocks the
    entire transition before gauge, stance, Hold, or zones are committed.
    """

    state.validate(allow_completed=True)
    if settings is None:
        settings = load_plan3_exam_settings()
    if gimmick_profile is not None and external_gimmick_resolver is not None:
        raise ValueError(
            "gimmick_profile and external_gimmick_resolver are mutually exclusive"
        )

    def blocked(*rules: str) -> Plan3TurnStart:
        return Plan3TurnStart(
            before=state,
            after=state,
            supported=False,
            unsupported_rules=_unique(list(rules)),
        )

    if not state.awaiting_turn_start:
        return blocked("phase:not-awaiting-turn-start")
    if state.turns_remaining == 0:
        return blocked("phase:exam-complete")
    if state.hand:
        return blocked("turn-start:preexisting-hand")
    if len(state.hold_pile) > settings.hold_limit:
        return blocked(
            f"turn-start:hold-limit:{len(state.hold_pile)}:{settings.hold_limit}"
        )
    if authoritative_draw_order is not None and observed_hand is not None:
        return blocked("turn-start:multiple-draw-evidence-sources")
    if authoritative_draw_order is None and observed_hand is None:
        return blocked("turn-start:draw-order-unknown")

    try:
        spent_playable = _spend_plan3_playable_value_turn(
            state,
            source="turn-start-spend",
        )
        advanced_status_enchants = _advance_status_enchants_for_turn_start(
            spent_playable
        )
    except (OverflowError, TypeError, ValueError) as error:
        return blocked(str(error))
    lesson_multiple_at_start = (
        spent_playable.lesson_parameter_multiple_state.spend_turn_start()
    )
    working = replace(
        advanced_status_enchants,
        plays_remaining=(
            1 + spent_playable.playable_value_add_runtime.value
        ),
        awaiting_turn_start=False,
        lesson_parameter_multiple_state=lesson_multiple_at_start,
    )
    from .plan3_stance_lock import (
        clear_stance_lock_recently_used,
        set_stance_lock_passing_turn_start,
    )

    cleared_lock = clear_stance_lock_recently_used(
        working.stance_lock_runtime
    )
    working = replace(
        working,
        stance_lock_runtime=set_stance_lock_passing_turn_start(
            cleared_lock
        ).runtime_after,
    )
    full_power_exit_runtime = _RuntimeResult(working)
    if working.stance == STANCE_FULL_POWER:
        # Official order: expire last turn's Full Power first, then evaluate
        # phase-47 listeners against the committed reset state.  Their child
        # commands and card callbacks settle before the later phase-40 reset
        # callbacks and before gauge consumption/re-entry.
        working = replace(working, stance=STANCE_NEUTRAL, stance_level=0)
        full_power_exit_runtime = _apply_runtime_phase(
            working,
            PHASE_STANCE_CHANGE_FROM_FULL_POWER,
            trigger_state=working,
            settings=settings,
        )
        if full_power_exit_runtime.unsupported_rules:
            return blocked(*full_power_exit_runtime.unsupported_rules)
        working = full_power_exit_runtime.state

    entered_full_power = False
    turn_start_enthusiastic_receipts = ()
    full_power_entry_runtime = _RuntimeResult(working)
    held_returned: tuple[Plan3CardRef, ...] = ()
    if working.full_power_points >= 10:
        stance_result = _change_stance(
            working, STANCE_FULL_POWER, 1, settings
        )
        working = stance_result.state
        entered_full_power = stance_result.changed
        turn_start_enthusiastic_receipts = stance_result.enthusiastic_receipts
        if entered_full_power:
            working = replace(
                working,
                full_power_points=working.full_power_points - 10,
            )
            if settings.full_power_playable_value_add:
                try:
                    working = _add_plan3_playable_value(
                        working,
                        settings.full_power_playable_value_add,
                        source="full-power-entry",
                    )
                except (OverflowError, TypeError, ValueError) as error:
                    return blocked(str(error))
            held_returned = working.hold_pile
            working = replace(working, hold_pile=())
            # ConsumeFullPowerPoint/TryInternalSetStance has committed the
            # stance and count changes before ExamSequence selects phase 39.
            # Evaluate the exact remaining-turn listener against that settled
            # state before the scheduled gimmick and draw.
            full_power_entry_runtime = _apply_runtime_phase(
                working,
                PHASE_STANCE_CHANGE_FULL_POWER,
                trigger_state=working,
                settings=settings,
            )
            if full_power_entry_runtime.unsupported_rules:
                return blocked(*full_power_entry_runtime.unsupported_rules)
            working = full_power_entry_runtime.state

    # ExamLoopTaskAsync clears prior-turn state and resolves Full Power before
    # it queues the current scheduled gimmick; drawing follows afterward.
    gimmick = _Plan3GimmickResolution(working)
    if external_gimmick_resolver is not None:
        external = external_gimmick_resolver(working, settings)
        if not isinstance(external, Plan3ExternalTurnStartGimmickResolution):
            raise TypeError(
                "external_gimmick_resolver must return "
                "Plan3ExternalTurnStartGimmickResolution"
            )
        gimmick = _Plan3GimmickResolution(
            external.state,
            external.fired_effect_ids,
            external.stamina_paid,
            external.stamina_recovered,
            external.unsupported_rules,
        )
        if gimmick.unsupported_rules:
            return blocked(*gimmick.unsupported_rules)
        working = gimmick.state
    elif gimmick_profile is not None:
        gimmick = _resolve_plan3_turn_start_gimmick(
            gimmick_profile, working, settings
        )
        if gimmick.unsupported_rules:
            return blocked(*gimmick.unsupported_rules)
        working = gimmick.state

    available_count = len(working.draw_pile) + len(working.discard_pile)
    hand_space = max(0, settings.hand_limit - len(held_returned))
    draw_count = min(
        settings.turn_start_distribute,
        available_count,
        hand_space,
    )
    if authoritative_draw_order is not None:
        if len(authoritative_draw_order) < draw_count:
            return blocked("turn-start:authoritative-draw-order-too-short")
        drawn = tuple(authoritative_draw_order[:draw_count])
        final_hand = tuple((*held_returned, *drawn))
        used_observed = False
    else:
        assert observed_hand is not None
        final_hand = tuple(observed_hand)
        held_remainder = list(final_hand)
        missing_held: list[str] = []
        for held in held_returned:
            if held in held_remainder:
                held_remainder.remove(held)
            else:
                missing_held.append(held.card_id)
        if missing_held:
            return blocked(
                *(f"turn-start:observed-hand-missing-held:{card_id}" for card_id in missing_held)
            )
        drawn = tuple(held_remainder)
        if len(drawn) != draw_count:
            return blocked(
                f"turn-start:observed-draw-count:{len(drawn)}:expected:{draw_count}"
            )
        used_observed = True

    if len(final_hand) > settings.hand_limit:
        return blocked(
            f"turn-start:hand-limit:{len(final_hand)}:{settings.hand_limit}"
        )
    draw_after, discard_after, zone_errors = _remove_drawn_cards(
        working, drawn
    )
    if zone_errors:
        return blocked(*zone_errors)
    after = replace(
        working,
        hand=final_hand,
        draw_pile=draw_after,
        discard_pile=discard_after,
        awaiting_turn_start=False,
    )
    # LocalSave's userPlayLog writes the turn cell (including its draw list)
    # before the subsequent ExamStartTurn non-card item event.  Apply the
    # listener to the settled post-draw state so the simulator exposes the
    # same observable ordering, while gimmick state execution remains at its
    # separately proven pre-draw native boundary above.
    start_runtime = _apply_runtime_phase(
        after,
        PHASE_START_TURN,
        settings=settings,
    )
    if start_runtime.unsupported_rules:
        return blocked(*start_runtime.unsupported_rules)
    after = start_runtime.state
    start_play_runtime = _apply_runtime_phase(
        after,
        PHASE_START_PLAY,
        settings=settings,
    )
    if start_play_runtime.unsupported_rules:
        return blocked(*start_play_runtime.unsupported_rules)
    turn_timer_runtime = _apply_runtime_phase(
        start_play_runtime.state,
        PHASE_TURN_TIMER,
        settings=settings,
    )
    if turn_timer_runtime.unsupported_rules:
        return blocked(*turn_timer_runtime.unsupported_rules)
    after_timer_removal = _remove_triggered_turn_timer_statuses(
        turn_timer_runtime.state
    )
    after = replace(
        after_timer_removal,
        active_status_enchants=tuple(
            replace(enchant, passing_turn_start=True)
            for enchant in after_timer_removal.active_status_enchants
        ),
        lesson_parameter_multiple_state=(
            after_timer_removal.lesson_parameter_multiple_state
            .mark_passing_turn_start()
        ),
        anti_debuff_runtime=(
            after_timer_removal.anti_debuff_runtime
            .mark_passing_turn_start()
        ),
    )
    try:
        after = _mark_plan3_playable_value_passing(
            after,
            source="turn-start-marker",
        )
    except (OverflowError, TypeError, ValueError) as error:
        return blocked(str(error))
    runtime_effect_events = tuple(
        replace(event, sequence_index=index)
        for index, event in enumerate(
            (
                *full_power_exit_runtime.effect_events,
                *full_power_entry_runtime.effect_events,
                *start_runtime.effect_events,
                *start_play_runtime.effect_events,
                *turn_timer_runtime.effect_events,
            )
        )
    )
    return Plan3TurnStart(
        before=state,
        after=after,
        supported=True,
        entered_full_power=entered_full_power,
        full_power_points_consumed=(10 if entered_full_power else 0),
        held_cards_returned=held_returned,
        drawn_cards=drawn,
        used_observed_hand=used_observed,
        fired_gimmick_effect_ids=gimmick.fired_effect_ids,
        gimmick_stamina_paid=gimmick.stamina_paid,
        gimmick_stamina_recovered=gimmick.stamina_recovered,
        fired_status_enchant_ids=tuple(
            (*full_power_exit_runtime.fired_enchant_ids,
             *full_power_entry_runtime.fired_enchant_ids,
             *start_runtime.fired_enchant_ids,
             *start_play_runtime.fired_enchant_ids,
             *turn_timer_runtime.fired_enchant_ids)
        ),
        fired_status_effect_ids=tuple(
            (*full_power_exit_runtime.fired_effect_ids,
             *full_power_entry_runtime.fired_effect_ids,
             *start_runtime.fired_effect_ids,
             *start_play_runtime.fired_effect_ids,
             *turn_timer_runtime.fired_effect_ids)
        ),
        runtime_effect_events=runtime_effect_events,
        enthusiastic_receipts=turn_start_enthusiastic_receipts,
    )


def end_plan3_turn(
    state: Plan3State,
    *,
    settings: Plan3ExamSettings | None = None,
    apply_turn_end_recovery: bool = False,
) -> Plan3State:
    """Close a verified turn and enter the explicit turn-start phase."""

    state.validate(allow_completed=True)
    if state.awaiting_turn_start:
        raise ValueError("turn is already awaiting its start phase")
    if state.turns_remaining == 0:
        raise ValueError("exam is already complete")
    if state.plays_remaining != 0:
        raise ValueError("cannot end a turn while card plays remain")
    if type(apply_turn_end_recovery) is not bool:
        raise TypeError("apply_turn_end_recovery must be boolean")
    if settings is None:
        settings = load_plan3_exam_settings()
    working = state
    if apply_turn_end_recovery and settings.turn_end_stamina_recovery:
        if working.max_stamina is None:
            raise ValueError("turn-end-recovery:max-stamina-unknown")
        working = replace(
            working,
            stamina=min(
                working.max_stamina,
                working.stamina + settings.turn_end_stamina_recovery,
            ),
        )
    end_runtime = _apply_runtime_phase(
        working,
        PHASE_END_TURN,
        settings=settings,
    )
    if end_runtime.unsupported_rules:
        joined = ",".join(end_runtime.unsupported_rules)
        raise ValueError(f"unsupported end-turn status: {joined}")
    working = end_runtime.state
    if working.enthusiasm == working.enthusiastic_runtime.value:
        working, _enthusiastic_spend = _spend_plan3_enthusiastic_turn(
            working,
            source="turn-end-spend",
        )
    turns_remaining = working.turns_remaining - 1
    fixed_statuses = _spend_stamina_consumption_down_fix_turns(
        working.stamina_consumption_down_fix_statuses
    )
    enthusiasm_additive_statuses = _spend_enthusiasm_additive_turns(
        working.enthusiasm_additive_statuses
    )
    full_power_point_additive_runtime = (
        working.full_power_point_additive_runtime.spend_turn()
    )
    from .plan3_stance_lock import spend_stance_lock_turn

    stance_lock_runtime = spend_stance_lock_turn(
        working.stance_lock_runtime
    ).runtime_after
    return replace(
        working,
        turns_remaining=turns_remaining,
        round_number=working.round_number + 1,
        plays_remaining=0,
        enthusiasm=0,
        enthusiastic_runtime=(
            working.enthusiastic_runtime
            if working.enthusiasm == 0
            else type(working.enthusiastic_runtime)()
        ),
        enthusiasm_additive=sum(
            status.value for status in enthusiasm_additive_statuses
        ),
        enthusiasm_additive_statuses=enthusiasm_additive_statuses,
        enthusiasm_multiple_statuses=_spend_enthusiasm_multiple_turns(
            working.enthusiasm_multiple_statuses
        ),
        full_power_point_additive_runtime=(
            full_power_point_additive_runtime
        ),
        stance_lock_runtime=stance_lock_runtime,
        stamina_consumption_down_turns=(
            working.stamina_consumption_down_turns
            if working.stamina_consumption_down_fresh
            else max(0, working.stamina_consumption_down_turns - 1)
        ),
        stamina_consumption_down_fresh=False,
        stamina_consumption_down_fixed=sum(
            status.value for status in fixed_statuses
        ),
        stamina_consumption_down_fix_statuses=fixed_statuses,
        block_restriction_turns=max(
            0,
            working.block_restriction_turns
            - (0 if working.block_restriction_fresh else 1),
        ),
        block_restriction_fresh=False,
        current_turn_total_add_parameter=0,
        hand=(),
        discard_pile=tuple((*working.discard_pile, *working.hand)),
        awaiting_turn_start=turns_remaining > 0,
    )


def _trigger_from_dict(payload: Mapping[str, Any]) -> Plan3Trigger:
    return Plan3Trigger(
        id=str(payload.get("id", "")),
        phase_types=tuple(str(value) for value in payload.get("phase_types", ())),
        phase_values=tuple(int(value) for value in payload.get("phase_values", ())),
        field_check_types=tuple(
            str(value) for value in payload.get("field_check_types", ())
        ),
        field_types=tuple(str(value) for value in payload.get("field_types", ())),
        field_values=tuple(int(value) for value in payload.get("field_values", ())),
        field_card_search_ids=tuple(
            str(value) for value in payload.get("field_card_search_ids", ())
        ),
        produce_card_search_id=str(payload.get("produce_card_search_id", "")),
        upper_search_count=int(payload.get("upper_search_count", 0)),
        lower_search_count=int(payload.get("lower_search_count", 0)),
        card_move_position_type=str(
            payload.get("card_move_position_type", MOVE_UNKNOWN)
        ),
        effect_types=tuple(str(value) for value in payload.get("effect_types", ())),
        lesson_type=str(payload.get("lesson_type", LESSON_UNKNOWN)),
    )


def _card_move_rule_from_dict(
    payload: Mapping[str, Any],
) -> Plan3CardMoveRule:
    return Plan3CardMoveRule(
        target_card_id=str(payload.get("target_card_id", "")),
        target_upgrade=int(payload.get("target_upgrade", 0)),
        target_effect_type=str(
            payload.get("target_effect_type", EFFECT_UNKNOWN)
        ),
        search_id=str(payload.get("search_id", "")),
        destination=str(payload.get("destination", MOVE_UNKNOWN)),
        pick_range_type=str(
            payload.get("pick_range_type", PICK_RANGE_UNKNOWN)
        ),
        pick_reference_search_id=str(
            payload.get("pick_reference_search_id", "")
        ),
        pick_count_type=str(
            payload.get("pick_count_type", PICK_COUNT_UNKNOWN)
        ),
        pick_count_min=int(payload.get("pick_count_min", 0)),
        pick_count_max=int(payload.get("pick_count_max", 0)),
        second_search_id=str(payload.get("second_search_id", "")),
        second_pick_range_type=str(
            payload.get("second_pick_range_type", PICK_RANGE_UNKNOWN)
        ),
        second_pick_reference_search_id=str(
            payload.get("second_pick_reference_search_id", "")
        ),
        second_pick_count_type=str(
            payload.get("second_pick_count_type", PICK_COUNT_UNKNOWN)
        ),
        second_pick_count_min=int(payload.get("second_pick_count_min", 0)),
        second_pick_count_max=int(payload.get("second_pick_count_max", 0)),
        chain_effect_ids=tuple(
            str(value) for value in payload.get("chain_effect_ids", ())
        ),
        card_status_enchant_id=str(
            payload.get("card_status_enchant_id", "")
        ),
        card_grow_effect_ids=tuple(
            str(value) for value in payload.get("card_grow_effect_ids", ())
        ),
        effect_group_ids=tuple(
            str(value) for value in payload.get("effect_group_ids", ())
        ),
    )


def _effect_from_dict(payload: Mapping[str, Any]) -> Plan3Effect:
    raw_trigger = payload.get("trigger")
    trigger = (
        _trigger_from_dict(raw_trigger)
        if isinstance(raw_trigger, Mapping)
        else None
    )
    raw_status_enchant = payload.get("status_enchant")
    status_enchant = (
        _status_rule_from_dict(raw_status_enchant)
        if isinstance(raw_status_enchant, Mapping)
        else None
    )
    raw_card_move_rule = payload.get("card_move_rule")
    if raw_card_move_rule is not None and not isinstance(
        raw_card_move_rule, Mapping
    ):
        raise TypeError("card move rule must be a mapping")
    raw_chain_effect = payload.get("chain_effect")
    if raw_chain_effect is not None and not isinstance(
        raw_chain_effect, Mapping
    ):
        raise TypeError("chain effect must be a mapping")
    return Plan3Effect(
        id=str(payload.get("id", "")),
        effect_type=str(payload.get("effect_type", "")),
        value1=int(payload.get("value1", 0)),
        value2=int(payload.get("value2", 0)),
        effect_count=int(payload.get("effect_count", 0)),
        effect_turn=int(payload.get("effect_turn", 0)),
        status_enchant_id=str(payload.get("status_enchant_id", "")),
        status_enchant=status_enchant,
        chain_effect_id=str(payload.get("chain_effect_id", "")),
        chain_effect_ids=tuple(
            str(value) for value in payload.get("chain_effect_ids", ())
        ),
        chain_effect=(
            _effect_from_dict(raw_chain_effect)
            if isinstance(raw_chain_effect, Mapping)
            else None
        ),
        trigger=trigger,
        once=bool(payload.get("once", False)),
        card_move_rule=(
            _card_move_rule_from_dict(raw_card_move_rule)
            if isinstance(raw_card_move_rule, Mapping)
            else None
        ),
    )


def _status_rule_from_dict(payload: Mapping[str, Any]) -> Plan3StatusEnchantRule:
    raw_trigger = payload.get("trigger")
    if not isinstance(raw_trigger, Mapping):
        raise TypeError("status enchant trigger must be a mapping")
    raw_effects = payload.get("effects", ())
    if not isinstance(raw_effects, (list, tuple)):
        raise TypeError("status enchant effects must be a sequence")
    rule = Plan3StatusEnchantRule(
        id=str(payload.get("id", "")),
        trigger=_trigger_from_dict(raw_trigger),
        effects=tuple(
            _effect_from_dict(effect)
            for effect in raw_effects
            if isinstance(effect, Mapping)
        ),
    )
    if len(rule.effects) != len(raw_effects):
        raise TypeError("status enchant effects must be mappings")
    return rule


def _active_enchant_from_dict(payload: Mapping[str, Any]) -> ActivePlan3StatusEnchant:
    raw_rule = payload.get("rule")
    if not isinstance(raw_rule, Mapping):
        raise TypeError("status enchant rule must be a mapping")
    rule = _status_rule_from_dict(raw_rule)
    raw_phase_counts = payload.get("phase_counts", ())
    if not isinstance(raw_phase_counts, (list, tuple)):
        raise TypeError("status enchant phase_counts must be a sequence")
    phase_counts: list[tuple[str, int]] = []
    for raw in raw_phase_counts:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise TypeError("status enchant phase-count rows must be pairs")
        phase_counts.append((str(raw[0]), int(raw[1])))
    return ActivePlan3StatusEnchant(
        instance_id=str(payload.get("instance_id", "")),
        source_id=str(payload.get("source_id", "")),
        rule=rule,
        max_uses=int(payload.get("max_uses", 0)),
        uses=int(payload.get("uses", 0)),
        max_uses_per_turn=int(payload.get("max_uses_per_turn", 0)),
        uses_this_turn=int(payload.get("uses_this_turn", 0)),
        remaining_turns=int(payload.get("remaining_turns", -1)),
        passing_turn_start=bool(payload.get("passing_turn_start", False)),
        is_item_direct=bool(payload.get("is_item_direct", True)),
        turn_count=int(payload.get("turn_count", 0)),
        phase_counts=tuple(phase_counts),
        native_uid=int(payload.get("native_uid", 0)),
        captured_card_guid=str(payload.get("captured_card_guid", "")),
        is_encore_enchant=bool(payload.get("is_encore_enchant", False)),
    )
