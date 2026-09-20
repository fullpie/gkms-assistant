"""Bounded Plan2 model and native audit for ``ExamDebuffRecover``.

This module is intentionally standalone.  It proves the four ordered
``p_card-02-ido-3_127`` rows and models only the direct
``ProduceExamEffectType_ExamDebuffRecover`` executor.  It does not register a
new central runtime handler, update formal coverage, or execute the card's
other ``AggressiveValueMultiple`` and ``Timer -> Block`` slots.

Android v3.2.3 is the behavioural authority here.  The executor snapshots
the active status list, keeps statuses whose normalized
``ProduceExamEffectType`` maps to ``ExamStatusEffectTargetType.Debuff``, sorts
by ``Uid`` descending, takes the newest ``value1`` entries when ``value1 >=
1`` (all entries otherwise), and removes the selected status objects as whole
objects.  It does not use a random source.  A non-empty selection creates one
deferred effect-difference record based on the first selected status.

The PC material in this repository is a card/effect metadata cross-check only;
there is no local PC native body used to claim behavioural parity.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from enum import IntEnum
from pathlib import Path
from typing import Final, Mapping, Sequence

from .master_db import DEFAULT_DATABASE


PROJECT_ROOT = Path(__file__).resolve().parents[2]

TARGET_CARD_ID: Final = "p_card-02-ido-3_127"
CARD_ID: Final = TARGET_CARD_ID
TARGET_CARD_NAME: Final = "わたしらしい色"
TARGET_UPGRADES: Final = (0, 1, 2, 3)

EFFECT_TYPE: Final = "ProduceExamEffectType_ExamDebuffRecover"
EFFECT_ID_UPGRADE0: Final = "e_effect-exam_debuff_recover-0001"
EFFECT_ID_UPGRADE1_3: Final = "e_effect-exam_debuff_recover-0002"
TARGET_EFFECT_IDS: Final = (
    EFFECT_ID_UPGRADE0,
    EFFECT_ID_UPGRADE1_3,
)
TARGET_SLOT_INDEX: Final = 1

AGGRESSIVE_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamAggressiveValueMultiple"
)
TIMER_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamEffectTimer"
BLOCK_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamBlock"
AGGRESSIVE_EFFECT_ID: Final = "e_effect-exam_aggressive_value_multiple-0500"
AGGRESSIVE_GROUP: Final = (
    "effect_group-visible-exam_card_play_aggressive-000"
)
TIMER_GROUP: Final = "effect_group-visible-exam_effect_timer-000"
BLOCK_GROUP: Final = "effect_group-visible-exam_block-000"

# These are the two retained same-card co groups for this bounded report.
# The Block leaf is owned by the existing timer-chain module and is not a
# third DebuffRecover co group.
CO_GROUPS: Final = (AGGRESSIVE_GROUP, TIMER_GROUP)
CO_GAP_GROUPS: Final = (
    "C:effect:ProduceExamEffectType_ExamAggressiveValueMultiple",
    "C:effect:ProduceExamEffectType_ExamEffectTimer",
)
GAP_AGGRESSIVE_MULTIPLE: Final = CO_GAP_GROUPS[0]
GAP_TIMER: Final = CO_GAP_GROUPS[1]

PLAY_ORIGINS: Final = ("ordinary", "forced", "extra")
_PLAY_ORIGIN_ALIASES: Final = {"extra-play": "extra"}

CARD_SLOT_ORDER_LABELS: Final = (
    "slot[0]:AggressiveValueMultiple",
    "slot[1]:DebuffRecover",
    "slot[2]:ExamEffectTimer->Block",
)
EXECUTION_ORDER: Final = (
    "card.playEffects[0] AggressiveValueMultiple",
    "card.playEffects[1] DebuffRecover",
    "card.playEffects[2] ExamEffectTimer installs Timer->Block child",
)
STATUS_REGISTRY_ORDER: Final = (
    "ExamStatusEffectCollection.GetStatusEffectList() active list",
    "native GetEffectType(status.Type)",
    "native GetEffectTargetType(normalized type)",
    "LINQ OrderByDescending(status.Uid), stable source-order ties",
)
DEBUFF_CANDIDATE_RULE: Final = (
    "active status and "
    "GetEffectTargetType(GetEffectType(status.Type)) == Debuff; group ignored"
)

_EXPECTED_DEBUFF_VALUE_BY_UPGRADE: Final = (1, 2, 2, 2)
_EXPECTED_DEBUFF_ID_BY_UPGRADE: Final = (
    EFFECT_ID_UPGRADE0,
    EFFECT_ID_UPGRADE1_3,
    EFFECT_ID_UPGRADE1_3,
    EFFECT_ID_UPGRADE1_3,
)
_EXPECTED_STAMINA_BY_UPGRADE: Final = (2, 2, 2, 1)
_EXPECTED_BLOCK_VALUE_BY_UPGRADE: Final = (4, 8, 10, 10)
_EXPECTED_TIMER_ID_BY_UPGRADE: Final = (
    "e_effect-exam_effect_timer-0001-01-e_effect-exam_block-0004",
    "e_effect-exam_effect_timer-0001-01-e_effect-exam_block-0008",
    "e_effect-exam_effect_timer-0001-01-e_effect-exam_block-0010",
    "e_effect-exam_effect_timer-0001-01-e_effect-exam_block-0010",
)
_EXPECTED_BLOCK_ID_BY_UPGRADE: Final = (
    "e_effect-exam_block-0004",
    "e_effect-exam_block-0008",
    "e_effect-exam_block-0010",
    "e_effect-exam_block-0010",
)

INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1


class Plan2DebuffRecoverContractError(ValueError):
    """Stable fail-closed error for an unknown or altered contract shape."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class ExamStatusEffectTargetType(IntEnum):
    NONE = 0
    BUFF = 1
    DEBUFF = 2


STATUS_TARGET_NONE: Final = int(ExamStatusEffectTargetType.NONE)
STATUS_TARGET_BUFF: Final = int(ExamStatusEffectTargetType.BUFF)
STATUS_TARGET_DEBUFF: Final = int(ExamStatusEffectTargetType.DEBUFF)


@dataclass(frozen=True, slots=True)
class Plan2DebuffRecoverBatchAccounting:
    """Accounting for this family only, not whole-card unlock coverage."""

    affected: int = 4
    direct: int = 0
    co_blocked: int = 4

    def __post_init__(self) -> None:
        if self.affected != self.direct + self.co_blocked:
            raise ValueError("DebuffRecover accounting does not balance")

    @property
    def co(self) -> int:
        return self.co_blocked


BATCH_ACCOUNTING: Final = Plan2DebuffRecoverBatchAccounting()


@dataclass(frozen=True, slots=True)
class DebuffRecoverEffectRow:
    """The normalized Master effect row plus all shape-bearing references."""

    id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    chain_effect_ids: tuple[str, ...]
    effect_group_ids: tuple[str, ...]

    @property
    def effect_id(self) -> str:
        return self.id

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "effect_type": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "effect_count": self.effect_count,
            "effect_turn": self.effect_turn,
            "status_enchant_id": self.status_enchant_id,
            "chain_effect_id": self.chain_effect_id,
            "chain_effect_ids": list(self.chain_effect_ids),
            "effect_group_ids": list(self.effect_group_ids),
        }


@dataclass(frozen=True, slots=True)
class CardEffectSlot:
    slot_index: int
    trigger_id: str
    effect_id: str
    hide_icon: bool
    once: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "slot_index": self.slot_index,
            "trigger_id": self.trigger_id,
            "effect_id": self.effect_id,
            "hide_icon": self.hide_icon,
            "once": self.once,
        }


@dataclass(frozen=True, slots=True)
class DebuffRecoverCardVersion:
    card_id: str
    upgrade_count: int
    name: str
    stamina: int
    play_trigger_id: str
    move_position_type: str
    effect_group_ids: tuple[str, ...]
    ordered_slots: tuple[CardEffectSlot, ...]
    effect: DebuffRecoverEffectRow

    @property
    def upgrade(self) -> int:
        return self.upgrade_count

    @property
    def effect_row(self) -> DebuffRecoverEffectRow:
        return self.effect

    @property
    def target_slot(self) -> CardEffectSlot:
        return self.ordered_slots[TARGET_SLOT_INDEX]

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.ordered_slots)

    @property
    def display_name(self) -> str:
        suffix = "" if self.upgrade_count == 0 else "+" * self.upgrade_count
        return f"{TARGET_CARD_NAME}{suffix}"

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "master_name": self.name,
            "display_name": self.display_name,
            "stamina": self.stamina,
            "play_trigger_id": self.play_trigger_id,
            "move_position_type": self.move_position_type,
            "effect_group_ids": list(self.effect_group_ids),
            "ordered_slots": [slot.to_dict() for slot in self.ordered_slots],
            "debuff_recover": self.effect.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Plan2DebuffRecoverCatalog:
    database: str
    versions: tuple[DebuffRecoverCardVersion, ...]

    @property
    def affected_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple((version.card_id, version.upgrade_count) for version in self.versions)

    @property
    def direct_refs(self) -> tuple[tuple[str, int], ...]:
        return ()

    @property
    def co_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return self.affected_refs

    def version(self, upgrade_count: int) -> DebuffRecoverCardVersion:
        for version in self.versions:
            if version.upgrade_count == upgrade_count:
                return version
        raise KeyError(upgrade_count)

    def summary(self) -> dict[str, object]:
        return {
            "family": EFFECT_TYPE,
            "card_id": TARGET_CARD_ID,
            "upgrades": list(TARGET_UPGRADES),
            "affected": BATCH_ACCOUNTING.affected,
            "direct": BATCH_ACCOUNTING.direct,
            "co_blocked": BATCH_ACCOUNTING.co_blocked,
            "co_groups": list(CO_GAP_GROUPS),
            "target_slot_index": TARGET_SLOT_INDEX,
            "target_effect_ids": [version.effect.id for version in self.versions],
            "whole_card_unlocked": False,
        }


@dataclass(frozen=True, slots=True)
class StatusEffect:
    """One object in the active ``ExamStatusEffectCollection`` list.

    ``effect_type`` is the normalized ``ProduceExamEffectType`` returned by
    native ``GetEffectType(status.Type)``.  ``group`` is intentionally not a
    predicate operand: native DebuffRecover never reads a status group.  A
    status with multiple ``layers`` is still one object and is removed as one
    object when selected.
    """

    instance_id: str
    uid: int
    effect_type: str
    icon_value: int = 0
    group: str | None = None
    layers: int = 1
    active: bool = True
    target_type: int | ExamStatusEffectTargetType | None = None
    native_status_type: str | None = None

    @property
    def layer_count(self) -> int:
        return self.layers


ExamStatusEffect = StatusEffect


@dataclass(frozen=True, slots=True)
class DebuffRecoverState:
    """Small immutable state used to prove non-status fields are untouched."""

    statuses: tuple[StatusEffect, ...]
    play_count: int = 0
    history: tuple[str, ...] = ()
    move_position: str = "unmodified"


@dataclass(frozen=True, slots=True)
class DebuffRecoverDifference:
    """The one native ``ExamEffectDifferenceData`` status/remove record."""

    status_effect_type: str
    previous_icon_value: int
    current_icon_value: int = 0
    is_consume: bool = False
    remove_buff_difference: bool = True

    @property
    def effect_type(self) -> str:
        return self.status_effect_type


@dataclass(frozen=True, slots=True)
class DebuffRecoverPlan:
    effect: DebuffRecoverEffectRow
    before: DebuffRecoverState
    ordered_candidates: tuple[StatusEffect, ...]
    selected: tuple[StatusEffect, ...]
    play_origin: str
    event_trace: tuple[str, ...]

    @property
    def selected_instance_ids(self) -> tuple[str, ...]:
        return tuple(status.instance_id for status in self.selected)


@dataclass(frozen=True, slots=True)
class DebuffRecoverExecution:
    effect: DebuffRecoverEffectRow
    before: DebuffRecoverState
    after: DebuffRecoverState
    ordered_candidates: tuple[StatusEffect, ...]
    selected: tuple[StatusEffect, ...]
    removed: tuple[StatusEffect, ...]
    difference: DebuffRecoverDifference | None
    difference_queue: tuple[DebuffRecoverDifference, ...]
    play_origin: str
    rng_calls: int
    direct_callback_events: tuple[str, ...]
    collection_mutation: str
    difference_queue_name: str
    event_trace: tuple[str, ...]

    @property
    def remaining_statuses(self) -> tuple[StatusEffect, ...]:
        return self.after.statuses

    @property
    def callbacks(self) -> tuple[str, ...]:
        return self.direct_callback_events

    @property
    def queue(self) -> tuple[DebuffRecoverDifference, ...]:
        return self.difference_queue


# The target helper is an indexed native table: type values 1..218 index a
# table at 0x273B4F0, returning None=0, Buff=1, or Debuff=2.  These are all
# named ProduceExamEffectType entries in that v3.2.3 registry.  Unnamed or
# otherwise unknown values fail closed in the model.
NATIVE_DEBUFF_EFFECT_TYPE_VALUES: Final = (
    7,
    18,
    27,
    48,
    51,
    60,
    69,
    73,
    78,
    84,
    86,
    105,
    106,
    107,
    113,
    114,
    115,
    122,
    123,
    141,
    151,
    152,
    153,
    154,
    155,
    180,
    191,
    192,
    193,
    199,
    203,
    204,
    205,
    206,
    207,
)
NATIVE_BUFF_EFFECT_TYPE_VALUES: Final = (
    1,
    2,
    3,
    4,
    5,
    10,
    11,
    13,
    14,
    15,
    19,
    23,
    24,
    28,
    29,
    31,
    33,
    36,
    38,
    39,
    42,
    45,
    46,
    47,
    49,
    52,
    56,
    59,
    62,
    63,
    66,
    70,
    74,
    76,
    77,
    81,
    82,
    85,
    89,
    90,
    93,
    103,
    109,
    117,
    118,
    119,
    120,
    121,
    124,
    125,
    126,
    127,
    128,
    129,
    130,
    131,
    132,
    133,
    140,
    142,
    143,
    144,
    145,
    146,
    147,
    148,
    149,
    150,
    157,
    158,
    159,
    160,
    161,
    162,
    163,
    164,
    165,
    166,
    167,
    168,
    169,
    170,
    171,
    172,
    173,
    174,
    175,
    176,
    177,
    178,
    179,
    181,
    182,
    183,
    184,
    185,
    186,
    187,
    188,
    189,
    190,
    194,
    195,
    196,
    197,
    198,
    200,
    201,
    202,
    210,
    211,
    212,
    213,
    214,
    215,
    216,
    217,
    218,
)
NATIVE_NONE_EFFECT_TYPE_VALUES: Final = (
    6,
    8,
    9,
    12,
    16,
    17,
    20,
    21,
    22,
    25,
    26,
    30,
    32,
    34,
    35,
    37,
    40,
    41,
    43,
    44,
    50,
    53,
    54,
    55,
    57,
    58,
    61,
    64,
    65,
    67,
    68,
    71,
    72,
    75,
    79,
    80,
    83,
    87,
    88,
    91,
    92,
    94,
    95,
    96,
    97,
    98,
    99,
    100,
    101,
    102,
    104,
    108,
    110,
    111,
    112,
    116,
    134,
    135,
    136,
    137,
    138,
    139,
    156,
    208,
    209,
)

_NATIVE_VALUE_TARGET: Final = {
    **{value: STATUS_TARGET_DEBUFF for value in NATIVE_DEBUFF_EFFECT_TYPE_VALUES},
    **{value: STATUS_TARGET_BUFF for value in NATIVE_BUFF_EFFECT_TYPE_VALUES},
    **{value: STATUS_TARGET_NONE for value in NATIVE_NONE_EFFECT_TYPE_VALUES},
}
NATIVE_TARGET_TYPE_VALUES: Final = _NATIVE_VALUE_TARGET

# Named registry entries used by the pure model.  Names not in this mapping
# are intentionally not guessed from spelling; this keeps an altered/unknown
# status shape fail closed.
NATIVE_DEBUFF_EFFECT_TYPES: Final = (
    "ProduceExamEffectType_ExamStaminaReduceFix",
    "ProduceExamEffectType_ExamBlockRestriction",
    "ProduceExamEffectType_ExamStaminaDamage",
    "ProduceExamEffectType_ExamStanceReset",
    "ProduceExamEffectType_ExamFullPowerPointReduce",
    "ProduceExamEffectType_ExamStaminaReduce",
    "ProduceExamEffectType_ExamStaminaConsumptionAdd",
    "ProduceExamEffectType_ExamBlockAddDown",
    "ProduceExamEffectType_ExamPanic",
    "ProduceExamEffectType_ExamStaminaConsumptionAddFix",
    "ProduceExamEffectType_ExamStaminaRecoverRestriction",
    "ProduceExamEffectType_ExamGimmickLessonDebuff",
    "ProduceExamEffectType_ExamGimmickParameterDebuff",
    "ProduceExamEffectType_ExamGimmickSleepy",
    "ProduceExamEffectType_ExamGimmickPlayCardLimit",
    "ProduceExamEffectType_ExamGimmickSlump",
    "ProduceExamEffectType_ExamGimmickStartTurnCardDrawDown",
    "ProduceExamEffectType_ExamBlockDown",
    "ProduceExamEffectType_ExamLessonChangeSpecifyMoreThan",
    "ProduceExamEffectType_StanceLock",
    "ProduceExamEffectType_ExamReviewReduce",
    "ProduceExamEffectType_ExamAggressiveReduce",
    "ProduceExamEffectType_ExamLessonBuffReduce",
    "ProduceExamEffectType_ExamParameterBuffReduce",
    "ProduceExamEffectType_ExamLessonValueMultipleDown",
    "ProduceExamEffectType_ExamParameterBuffMultiplePerTurnReduce",
    "ProduceExamEffectType_ExamStanceLockConcentration",
    "ProduceExamEffectType_ExamStanceLockFullPower",
    "ProduceExamEffectType_ExamStanceLockPreservation",
    "ProduceExamEffectType_ExamBuffConsumptionAdd",
    "ProduceExamEffectType_ExamLessonBuffReduceCancellable",
    "ProduceExamEffectType_ExamParameterBuffReduceCancellable",
    "ProduceExamEffectType_ExamAggressiveReduceCancellable",
    "ProduceExamEffectType_ExamReviewReduceCancellable",
    "ProduceExamEffectType_ExamFullPowerPointReduceCancellable",
)
NATIVE_BUFF_EFFECT_TYPES: Final = (
    "ProduceExamEffectType_ExamLesson",
    "ProduceExamEffectType_ExamParameterBuff",
    "ProduceExamEffectType_ExamBlock",
    "ProduceExamEffectType_ExamCardDraw",
    "ProduceExamEffectType_ExamStaminaConsumptionDown",
    "ProduceExamEffectType_ExamLessonBuff",
    "ProduceExamEffectType_ExamCardUpgrade",
    "ProduceExamEffectType_ExamBlockValueMultiple",
    "ProduceExamEffectType_ExamPlayableValueAdd",
    "ProduceExamEffectType_ExamLessonBuffMultiple",
    "ProduceExamEffectType_ExamLessonDependBlock",
    "ProduceExamEffectType_ExamMultipleLessonBuffLesson",
    "ProduceExamEffectType_ExamForcePlayCardSearch",
    "ProduceExamEffectType_ExamStaminaRecoverFix",
    "ProduceExamEffectType_ExamLessonFix",
    "ProduceExamEffectType_ExamReview",
    "ProduceExamEffectType_ExamCardStaminaConsumptionReduce",
    "ProduceExamEffectType_ExamReviewValueMultiple",
    "ProduceExamEffectType_ExamCardSearchEffectPlayCountBuff",
    "ProduceExamEffectType_ExamLessonValueMultiple",
    "ProduceExamEffectType_ExamCardPlayAggressive",
    "ProduceExamEffectType_ExamConcentration",
    "ProduceExamEffectType_ExamPreservation",
    "ProduceExamEffectType_ExamFullPower",
    "ProduceExamEffectType_ExamFullPowerPoint",
    "ProduceExamEffectType_ExamLessonAddBlock",
    "ProduceExamEffectType_ExamLessonFullPowerPoint",
    "ProduceExamEffectType_ExamSearchPlayCardStaminaConsumptionChange",
    "ProduceExamEffectType_ExamUplifting",
    "ProduceExamEffectType_ExamExtraTurn",
    "ProduceExamEffectType_ExamAntiDebuff",
    "ProduceExamEffectType_ExamThresholdDown",
    "ProduceExamEffectType_ExamBlockAddDownRestriction",
    "ProduceExamEffectType_ExamStaminaRecoverAdd",
    "ProduceExamEffectType_ExamStaminaReduceChange",
    "ProduceExamEffectType_ExamLessonChangeSpecifyLessThan",
    "ProduceExamEffectType_ExamHandHold",
    "ProduceExamEffectType_ExamStaminaConsumptionAddDown",
    "ProduceExamEffectType_ExamStaminaConsumptionDownAdd",
    "ProduceExamEffectType_ExamGetCardUpgrade",
    "ProduceExamEffectType_ExamStaminaConsumptionDownFix",
    "ProduceExamEffectType_ExamEffectTimer",
    "ProduceExamEffectType_ExamGimmickEnthusiastic",
    "ProduceExamEffectType_ExamStaminaRecoverMultiple",
    "ProduceExamEffectType_ExamLessonPerSearchCount",
    "ProduceExamEffectType_ExamBlockFix",
    "ProduceExamEffectType_ExamLessonAddMultipleLessonBuff",
    "ProduceExamEffectType_ExamCardStatusEnchant",
    "ProduceExamEffectType_ExamLessonDependExamReview",
    "ProduceExamEffectType_ExamLessonDependExamCardPlayAggressive",
    "ProduceExamEffectType_ExamReviewDependExamBlock",
    "ProduceExamEffectType_ExamBlockDependExamReview",
    "ProduceExamEffectType_ExamReviewDependExamCardPlayAggressive",
    "ProduceExamEffectType_ExamParameterBuffMultiplePerTurn",
    "ProduceExamEffectType_ExamLessonBuffDependParameterBuff",
    "ProduceExamEffectType_ExamLessonDependParameterBuff",
    "ProduceExamEffectType_ExamLessonAddMultipleParameterBuff",
    "ProduceExamEffectType_ExamBlockPerUseCardCount",
    "ProduceExamEffectType_ExamChainEffect",
    "ProduceExamEffectType_ExamLessonDependStamina",
    "ProduceExamEffectType_ExamBlockAddMultipleAggressive",
    "ProduceExamEffectType_ExamLessonDependStaminaConsumptionSum",
    "ProduceExamEffectType_ExamChainEffectPerPassedTurn",
    "ProduceExamEffectType_ExamChainEffectPerRemainTurn",
    "ProduceExamEffectType_ExamLessonDependPlayCardCountSum",
    EFFECT_TYPE,
    AGGRESSIVE_EFFECT_TYPE,
    "ProduceExamEffectType_ExamItemFireLimitAdd",
    "ProduceExamEffectType_ExamParameterBuffPerSearchCount",
    "ProduceExamEffectType_ExamLessonBuffPerSearchCount",
    "ProduceExamEffectType_ExamReviewPerSearchCount",
    "ProduceExamEffectType_ExamAggressivePerSearchCount",
    "ProduceExamEffectType_ExamBlockPerSearchCount",
    "ProduceExamEffectType_ExamFullPowerPointPerSearchCount",
    "ProduceExamEffectType_ExamLessonDependBlockAndSearchCount",
    "ProduceExamEffectType_ExamLessonDependAggressiveAndSearchCount",
    "ProduceExamEffectType_ExamLessonDependReviewAndSearchCount",
    "ProduceExamEffectType_ExamEffectPerSearchCount",
    "ProduceExamEffectType_ExamOverPreservation",
    "ProduceExamEffectType_ExamParameterBuffDependLessonBuff",
    "ProduceExamEffectType_ExamAggressiveDependReview",
    "ProduceExamEffectType_ExamEnthusiasticAdditive",
    "ProduceExamEffectType_ExamEnthusiasticMultiple",
    "ProduceExamEffectType_ExamFullPowerLessonMultipleAdditive",
    "ProduceExamEffectType_ExamConcentrationLessonMultipleAdditive",
    "ProduceExamEffectType_ExamLessonBuffAdditive",
    "ProduceExamEffectType_ExamParameterBuffAdditive",
    "ProduceExamEffectType_ExamAggressiveAdditive",
    "ProduceExamEffectType_ExamReviewAdditive",
    "ProduceExamEffectType_ExamFullPowerPointAdditive",
    "ProduceExamEffectType_ExamGrowEffectLessonAddAdditive",
    "ProduceExamEffectType_ExamLessonValueMultipleDependReviewOrAggressive",
    "ProduceExamEffectType_ExamReviewMultiple",
    "ProduceExamEffectType_ExamMultipleEnthusiasticLesson",
    "ProduceExamEffectType_ExamMultipleConcentrationLesson",
    "ProduceExamEffectType_ExamMultipleFullPowerLesson",
    "ProduceExamEffectType_ExamLessonDependBlockConsumptionSum",
    "ProduceExamEffectType_ExamForcePlayCardSearchWithCost",
    "ProduceExamEffectType_ExamBlockDependBlockConsumptionSum",
    "ProduceExamEffectType_ExamEnthusiasticTurnAdd",
    "ProduceExamEffectType_ExamEffectTimerEndTurn",
    "ProduceExamEffectType_ExamCardShuffleDeckGrave",
    "ProduceExamEffectType_ExamReviewCountAdd",
    "ProduceExamEffectType_ExamReviewTurnEndReduceLock",
    "ProduceExamEffectType_ExamParameterBuffTurnEndReduceLock",
    "ProduceExamEffectType_ExamBuffConsumptionDown",
    "ProduceExamEffectType_ExamSearchPlayCardBuffConsumptionChange",
    "ProduceExamEffectType_ExamPlayCardLimitPlayableValueAdd",
    "ProduceExamEffectType_ExamReviewDependReviewConsumptionSum",
    "ProduceExamEffectType_ExamParameterBuffAdditiveFix",
    "ProduceExamEffectType_ExamLessonBuffAdditiveFix",
    "ProduceExamEffectType_ExamAggressiveAdditiveFix",
    "ProduceExamEffectType_ExamReviewAdditiveFix",
    "ProduceExamEffectType_ExamFullPowerPointAdditiveFix",
    "ProduceExamEffectType_ExamMoveGrowEffect",
    "ProduceExamEffectType_ExamLessonDependEnthusiasticGetSum",
    "ProduceExamEffectType_ExamCardShuffleDeckLost",
    "ProduceExamEffectType_ExamFullPowerPointDependFullPowerPointGetSum",
)
NATIVE_NONE_EFFECT_TYPES: Final = (
    "ProduceExamEffectType_ExamCardCreateId",
    "ProduceExamEffectType_ExamCardMove",
    "ProduceExamEffectType_ExamCardStaminaConsumptionChange",
    "ProduceExamEffectType_ExamCardCreateSearch",
    "ProduceExamEffectType_ExamStatusEnchant",
    "ProduceExamEffectType_ExamCardStaminaConsumptionDownSpecify",
    "ProduceExamEffectType_ExamCardDuplicate",
    "ProduceExamEffectType_ExamLessonValueChangePerPlay",
    "ProduceExamEffectType_ExamForecast",
    "ProduceExamEffectType_ExamHandGraveCountCardDraw",
    "ProduceExamEffectType_ExamHandGraveCountCardAdd",
    "ProduceExamEffectType_ExamAddGrowEffect",
    "ProduceExamEffectType_ExamStatusEnchantTurnAdd",
    "ProduceExamEffectType_ExamStatusEnchantCountAdd",
)

NATIVE_EFFECT_TARGET_TYPE: Final[Mapping[str, int]] = {
    **{effect_type: STATUS_TARGET_DEBUFF for effect_type in NATIVE_DEBUFF_EFFECT_TYPES},
    **{effect_type: STATUS_TARGET_BUFF for effect_type in NATIVE_BUFF_EFFECT_TYPES},
    **{effect_type: STATUS_TARGET_NONE for effect_type in NATIVE_NONE_EFFECT_TYPES},
}
KNOWN_STATUS_EFFECT_TYPES: Final = frozenset(NATIVE_EFFECT_TARGET_TYPE)


def _i32(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise Plan2DebuffRecoverContractError("i32-required", label)
    if value < INT32_MIN or value > INT32_MAX:
        raise Plan2DebuffRecoverContractError("i32-out-of-range", label)
    return int(value)


def _string(value: object, label: str, *, allow_empty: bool = True) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise Plan2DebuffRecoverContractError("string-required", label)
    return value


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise Plan2DebuffRecoverContractError("bool-required", label)
    return value


def _json_object(raw: object, label: str) -> dict[str, object]:
    if not isinstance(raw, str):
        raise Plan2DebuffRecoverContractError("json-object-required", label)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise Plan2DebuffRecoverContractError("json-invalid", label) from error
    if not isinstance(value, dict):
        raise Plan2DebuffRecoverContractError("json-object-required", label)
    return value


def _json_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(type(item) is str for item in value):
        raise Plan2DebuffRecoverContractError("string-array-required", label)
    return tuple(value)


def _json_effect_row(
    row: sqlite3.Row | tuple[object, ...],
) -> DebuffRecoverEffectRow:
    effect_id, effect_type, value1, value2, count, turn, status_id, chain_id, raw_json = row
    raw = _json_object(raw_json, f"effect:{effect_id}:raw_json")
    expected_keys = {
        "id",
        "effectType",
        "effectValue1",
        "effectValue2",
        "effectCount",
        "effectTurn",
        "chainProduceExamEffectId",
        "chainProduceExamEffectIds",
        "produceExamStatusEnchantId",
        "effectGroupIds",
    }
    if not expected_keys.issubset(raw):
        raise Plan2DebuffRecoverContractError("effect-raw-shape", str(effect_id))
    normalized = DebuffRecoverEffectRow(
        _string(effect_id, "effect.id", allow_empty=False),
        _string(effect_type, "effect.effect_type", allow_empty=False),
        _i32(value1, "effect.value1"),
        _i32(value2, "effect.value2"),
        _i32(count, "effect.effect_count"),
        _i32(turn, "effect.effect_turn"),
        _string(status_id, "effect.status_enchant_id"),
        _string(chain_id, "effect.chain_effect_id"),
        _json_strings(raw["chainProduceExamEffectIds"], "effect.chain_effect_ids"),
        _json_strings(raw["effectGroupIds"], "effect.effect_group_ids"),
    )
    raw_values = (
        ("id", raw["id"], normalized.id),
        ("effectType", raw["effectType"], normalized.effect_type),
        ("effectValue1", raw["effectValue1"], normalized.value1),
        ("effectValue2", raw["effectValue2"], normalized.value2),
        ("effectCount", raw["effectCount"], normalized.effect_count),
        ("effectTurn", raw["effectTurn"], normalized.effect_turn),
        (
            "produceExamStatusEnchantId",
            raw["produceExamStatusEnchantId"],
            normalized.status_enchant_id,
        ),
        ("chainProduceExamEffectId", raw["chainProduceExamEffectId"], normalized.chain_effect_id),
    )
    if any(value != expected for _, value, expected in raw_values):
        raise Plan2DebuffRecoverContractError("effect-normalized-drift", normalized.id)
    return normalized


def _read_effect_row(
    connection: sqlite3.Connection,
    effect_id: str,
) -> DebuffRecoverEffectRow:
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
        raise Plan2DebuffRecoverContractError("master-effect-missing", effect_id)
    return _json_effect_row(row)


def _validate_plain_effect(
    row: DebuffRecoverEffectRow,
    *,
    effect_type: str,
    value1: int,
    value2: int = 0,
    effect_count: int = 0,
    effect_turn: int = 0,
    status_enchant_id: str = "",
    chain_effect_id: str = "",
    chain_effect_ids: tuple[str, ...] = (),
    effect_group_ids: tuple[str, ...] = (),
) -> None:
    expected = (
        row.effect_type,
        row.value1,
        row.value2,
        row.effect_count,
        row.effect_turn,
        row.status_enchant_id,
        row.chain_effect_id,
        row.chain_effect_ids,
        row.effect_group_ids,
    )
    actual = (
        effect_type,
        value1,
        value2,
        effect_count,
        effect_turn,
        status_enchant_id,
        chain_effect_id,
        chain_effect_ids,
        effect_group_ids,
    )
    if expected != actual:
        raise Plan2DebuffRecoverContractError("unintegrated-debuff-recover-shape", row.id)


def _validate_debuff_effect(row: DebuffRecoverEffectRow) -> None:
    if row.id not in (EFFECT_ID_UPGRADE0, EFFECT_ID_UPGRADE1_3):
        raise Plan2DebuffRecoverContractError("unintegrated-debuff-recover-row", row.id)
    expected_value = 1 if row.id == EFFECT_ID_UPGRADE0 else 2
    _validate_plain_effect(
        row,
        effect_type=EFFECT_TYPE,
        value1=expected_value,
    )


def resolve_status_target_type(effect_type: str) -> int:
    """Return the pinned native target class for a normalized status type."""

    if type(effect_type) is not str or effect_type not in NATIVE_EFFECT_TARGET_TYPE:
        raise Plan2DebuffRecoverContractError("unknown-status-effect-type", str(effect_type))
    return NATIVE_EFFECT_TARGET_TYPE[effect_type]


def _load_catalog_card(
    connection: sqlite3.Connection,
    upgrade_count: int,
) -> DebuffRecoverCardVersion:
    row = connection.execute(
        """
        SELECT id, upgrade_count, name, plan_type, category, stamina,
               play_trigger_id, move_position_type, play_effects_json, raw_json
          FROM card
         WHERE id = ? AND upgrade_count = ?
        """,
        (TARGET_CARD_ID, upgrade_count),
    ).fetchone()
    if row is None:
        raise Plan2DebuffRecoverContractError(
            "master-card-version-missing", f"{TARGET_CARD_ID}#{upgrade_count}"
        )
    (
        card_id,
        card_upgrade,
        name,
        plan_type,
        category,
        stamina,
        play_trigger_id,
        move_position_type,
        play_effects_json,
        raw_json,
    ) = row
    raw = _json_object(raw_json, f"card:{TARGET_CARD_ID}#{upgrade_count}:raw_json")
    slots_raw = raw.get("playEffects")
    if not isinstance(slots_raw, list) or len(slots_raw) != 3:
        raise Plan2DebuffRecoverContractError("card-play-effects-shape", str(upgrade_count))
    if raw.get("id") != TARGET_CARD_ID or raw.get("upgradeCount") != upgrade_count:
        raise Plan2DebuffRecoverContractError("card-raw-identity", str(upgrade_count))
    if raw.get("planType") != "ProducePlanType_Plan2" or raw.get(
        "category"
    ) != "ProduceCardCategory_MentalSkill":
        raise Plan2DebuffRecoverContractError("card-plan-or-category", str(upgrade_count))
    if raw.get("stamina") != stamina or raw.get("playProduceExamTriggerId") != play_trigger_id:
        raise Plan2DebuffRecoverContractError("card-normalized-drift", str(upgrade_count))
    if raw.get("playMovePositionType") != move_position_type:
        raise Plan2DebuffRecoverContractError("card-move-drift", str(upgrade_count))
    if raw.get("effectGroupIds") != [BLOCK_GROUP, TIMER_GROUP, AGGRESSIVE_GROUP]:
        raise Plan2DebuffRecoverContractError("card-effect-groups-drift", str(upgrade_count))
    expected_ids = (
        AGGRESSIVE_EFFECT_ID,
        _EXPECTED_DEBUFF_ID_BY_UPGRADE[upgrade_count],
        _EXPECTED_TIMER_ID_BY_UPGRADE[upgrade_count],
    )
    slots: list[CardEffectSlot] = []
    for index, (raw_slot, expected_id) in enumerate(zip(slots_raw, expected_ids, strict=True)):
        if not isinstance(raw_slot, dict):
            raise Plan2DebuffRecoverContractError("card-slot-object-required", str(index))
        required = {"produceExamTriggerId", "produceExamEffectId", "hideIcon", "isOncePlayEffect"}
        if not required.issubset(raw_slot):
            raise Plan2DebuffRecoverContractError("card-slot-shape", str(index))
        slot = CardEffectSlot(
            index,
            _string(raw_slot["produceExamTriggerId"], f"card.slot[{index}].trigger"),
            _string(raw_slot["produceExamEffectId"], f"card.slot[{index}].effect", allow_empty=False),
            _bool(raw_slot["hideIcon"], f"card.slot[{index}].hideIcon"),
            _bool(raw_slot["isOncePlayEffect"], f"card.slot[{index}].once"),
        )
        if slot.effect_id != expected_id or slot.trigger_id or slot.hide_icon or slot.once:
            raise Plan2DebuffRecoverContractError("card-slot-shape", str(index))
        slots.append(slot)
    if json.loads(str(play_effects_json)) != slots_raw:
        raise Plan2DebuffRecoverContractError("card-play-effects-normalized-drift", str(upgrade_count))

    aggressive = _read_effect_row(connection, AGGRESSIVE_EFFECT_ID)
    _validate_plain_effect(
        aggressive,
        effect_type=AGGRESSIVE_EFFECT_TYPE,
        value1=500,
        effect_group_ids=(AGGRESSIVE_GROUP,),
    )
    debuff = _read_effect_row(connection, expected_ids[1])
    _validate_debuff_effect(debuff)
    timer = _read_effect_row(connection, expected_ids[2])
    _validate_plain_effect(
        timer,
        effect_type=TIMER_EFFECT_TYPE,
        value1=1,
        effect_count=1,
        chain_effect_id=_EXPECTED_BLOCK_ID_BY_UPGRADE[upgrade_count],
        effect_group_ids=(BLOCK_GROUP, TIMER_GROUP),
    )
    block = _read_effect_row(connection, _EXPECTED_BLOCK_ID_BY_UPGRADE[upgrade_count])
    _validate_plain_effect(
        block,
        effect_type=BLOCK_EFFECT_TYPE,
        value1=_EXPECTED_BLOCK_VALUE_BY_UPGRADE[upgrade_count],
        effect_group_ids=(BLOCK_GROUP,),
    )
    if (
        card_id != TARGET_CARD_ID
        or card_upgrade != upgrade_count
        or type(name) is not str
        or not name
        or plan_type != "ProducePlanType_Plan2"
        or category != "ProduceCardCategory_MentalSkill"
        or stamina != _EXPECTED_STAMINA_BY_UPGRADE[upgrade_count]
        or play_trigger_id != ""
        or move_position_type != "ProduceCardMovePositionType_Lost"
    ):
        raise Plan2DebuffRecoverContractError("card-version-shape", str(upgrade_count))
    return DebuffRecoverCardVersion(
        TARGET_CARD_ID,
        upgrade_count,
        name,
        _i32(stamina, f"card#{upgrade_count}.stamina"),
        play_trigger_id,
        move_position_type,
        (BLOCK_GROUP, TIMER_GROUP, AGGRESSIVE_GROUP),
        tuple(slots),
        debuff,
    )


def load_plan2_debuff_recover_catalog(
    *, database: Path = DEFAULT_DATABASE
) -> Plan2DebuffRecoverCatalog:
    """Load and validate the four exact Master card/effect versions."""

    if not database.is_file():
        raise Plan2DebuffRecoverContractError("master-database-missing", str(database))
    try:
        connection = sqlite3.connect(str(database))
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as error:
        raise Plan2DebuffRecoverContractError("master-database-open", str(database)) from error
    try:
        versions = tuple(_load_catalog_card(connection, upgrade) for upgrade in TARGET_UPGRADES)
    except (sqlite3.Error, TypeError, ValueError) as error:
        if isinstance(error, Plan2DebuffRecoverContractError):
            raise
        raise Plan2DebuffRecoverContractError("master-catalog-shape", str(error)) from error
    finally:
        connection.close()
    if tuple(version.upgrade_count for version in versions) != TARGET_UPGRADES:
        raise Plan2DebuffRecoverContractError("catalog-order-drift")
    return Plan2DebuffRecoverCatalog(str(database), versions)


load_catalog = load_plan2_debuff_recover_catalog


def resolve_plan2_debuff_recover(
    effect: str | DebuffRecoverEffectRow,
    *,
    database: Path = DEFAULT_DATABASE,
) -> DebuffRecoverEffectRow:
    """Resolve only the two exact DebuffRecover rows and fail closed on drift."""

    if isinstance(effect, str):
        if effect not in TARGET_EFFECT_IDS:
            raise Plan2DebuffRecoverContractError("unintegrated-debuff-recover-row", effect)
        if not database.is_file():
            raise Plan2DebuffRecoverContractError("master-database-missing", str(database))
        connection = sqlite3.connect(str(database))
        try:
            row = _read_effect_row(connection, effect)
        finally:
            connection.close()
    elif isinstance(effect, DebuffRecoverEffectRow):
        row = effect
    else:
        raise TypeError("effect must be an effect ID or DebuffRecoverEffectRow")
    _validate_debuff_effect(row)
    return row


resolve_effect = resolve_plan2_debuff_recover


def _coerce_state(
    state: DebuffRecoverState | Sequence[StatusEffect],
) -> DebuffRecoverState:
    if isinstance(state, DebuffRecoverState):
        current = state
    else:
        current = DebuffRecoverState(tuple(state))
    if not isinstance(current.statuses, tuple):
        raise Plan2DebuffRecoverContractError("status-registry-tuple-required")
    _i32(current.play_count, "runtime.play_count")
    if current.play_count < 0:
        raise Plan2DebuffRecoverContractError("runtime.play_count-negative")
    if not isinstance(current.history, tuple) or not all(
        type(item) is str for item in current.history
    ):
        raise Plan2DebuffRecoverContractError("runtime.history-shape")
    _string(current.move_position, "runtime.move_position")
    seen_ids: set[str] = set()
    for status in current.statuses:
        _validate_status(status)
        if status.instance_id in seen_ids:
            raise Plan2DebuffRecoverContractError("duplicate-status-instance", status.instance_id)
        seen_ids.add(status.instance_id)
    return current


def _validate_status(status: StatusEffect) -> None:
    if not isinstance(status, StatusEffect):
        raise Plan2DebuffRecoverContractError("status-object-required")
    _string(status.instance_id, "status.instance_id", allow_empty=False)
    _i32(status.uid, f"status:{status.instance_id}.uid")
    _string(status.effect_type, f"status:{status.instance_id}.effect_type", allow_empty=False)
    _i32(status.icon_value, f"status:{status.instance_id}.icon_value")
    if status.group is not None:
        _string(status.group, f"status:{status.instance_id}.group")
    _i32(status.layers, f"status:{status.instance_id}.layers")
    if status.layers < 1:
        raise Plan2DebuffRecoverContractError("status-layer-count-invalid", status.instance_id)
    _bool(status.active, f"status:{status.instance_id}.active")
    if status.native_status_type is not None:
        _string(status.native_status_type, f"status:{status.instance_id}.native_type")
    actual_target = resolve_status_target_type(status.effect_type)
    if status.target_type is not None:
        target = _i32(status.target_type, f"status:{status.instance_id}.target_type")
        if target not in (STATUS_TARGET_NONE, STATUS_TARGET_BUFF, STATUS_TARGET_DEBUFF):
            raise Plan2DebuffRecoverContractError("unknown-status-target-type", status.instance_id)
        if target != actual_target:
            raise Plan2DebuffRecoverContractError("status-target-type-mismatch", status.instance_id)


def _canonical_play_origin(play_origin: str) -> str:
    if type(play_origin) is not str:
        raise Plan2DebuffRecoverContractError("play-origin-required")
    canonical = _PLAY_ORIGIN_ALIASES.get(play_origin, play_origin)
    if canonical not in PLAY_ORIGINS:
        raise Plan2DebuffRecoverContractError("unknown-play-origin", play_origin)
    return canonical


def snapshot_debuff_recover(
    effect: str | DebuffRecoverEffectRow,
    state: DebuffRecoverState | Sequence[StatusEffect],
    *,
    play_origin: str = "ordinary",
    database: Path = DEFAULT_DATABASE,
) -> DebuffRecoverPlan:
    """Snapshot candidates exactly as native does before collection removal."""

    row = resolve_plan2_debuff_recover(effect, database=database)
    before = _coerce_state(state)
    origin = _canonical_play_origin(play_origin)
    active = tuple(status for status in before.statuses if status.active)
    candidates = tuple(
        sorted(
            (status for status in active if resolve_status_target_type(status.effect_type) == STATUS_TARGET_DEBUFF),
            key=lambda status: status.uid,
            reverse=True,
        )
    )
    selected = candidates[: row.value1] if row.value1 >= 1 else candidates
    trace = (
        "status-registry:GetStatusEffectList(active-only)",
        "status-filter:GetEffectTargetType(GetEffectType(status.Type))==Debuff",
        "status-order:OrderByDescending(status.Uid) [stable source-order ties]",
        "status-select:Take(value1) when value1>=1 else all",
        "status-snapshot:selected-whole-status-objects",
    )
    return DebuffRecoverPlan(row, before, candidates, tuple(selected), origin, trace)


def commit_debuff_recover(
    plan: DebuffRecoverPlan,
    state_at_remove: DebuffRecoverState | Sequence[StatusEffect] | None = None,
) -> DebuffRecoverExecution:
    """Apply the captured object predicate at the native removal boundary.

    The optional second state is a focused test seam for a status disappearing
    or a new status being added after snapshot and before
    ``RemoveStatusEffect``.  Membership is object identity, matching
    ``List.Contains`` on the captured managed status objects; a replacement
    object with the same UID/label is not selected.
    """

    if not isinstance(plan, DebuffRecoverPlan):
        raise TypeError("plan must be a DebuffRecoverPlan")
    current = plan.before if state_at_remove is None else _coerce_state(state_at_remove)
    if (
        current.play_count != plan.before.play_count
        or current.history != plan.before.history
        or current.move_position != plan.before.move_position
    ):
        raise Plan2DebuffRecoverContractError("runtime-metadata-drift")
    selected_ids = {id(status) for status in plan.selected}
    removed = tuple(status for status in current.statuses if id(status) in selected_ids)
    remaining = tuple(status for status in current.statuses if id(status) not in selected_ids)
    after = replace(current, statuses=remaining)
    difference = None
    if plan.selected:
        first = plan.selected[0]
        difference = DebuffRecoverDifference(
            status_effect_type=first.effect_type,
            previous_icon_value=first.icon_value,
        )
    queue = () if difference is None else (difference,)
    trace_parts = [
        *plan.event_trace,
        "status-remove:ExamStatusEffectCollection.RemoveStatusEffect(predicate)",
    ]
    if difference is None:
        trace_parts.append("difference:none-selected")
    else:
        trace_parts.extend(
            (
                "difference:create-after-remove-if-selected-nonempty",
                "difference:append-to-context.effectDifferenceList",
            )
        )
    return DebuffRecoverExecution(
        effect=plan.effect,
        before=plan.before,
        after=after,
        ordered_candidates=plan.ordered_candidates,
        selected=plan.selected,
        removed=removed,
        difference=difference,
        difference_queue=queue,
        play_origin=plan.play_origin,
        rng_calls=0,
        direct_callback_events=(),
        collection_mutation="RemoveStatusEffect(predicate captured-list.Contains)",
        difference_queue_name="context.effectDifferenceList",
        event_trace=tuple(trace_parts),
    )


def execute_debuff_recover(
    effect: str | DebuffRecoverEffectRow,
    state: DebuffRecoverState | Sequence[StatusEffect],
    *,
    statuses_at_remove: DebuffRecoverState | Sequence[StatusEffect] | None = None,
    play_origin: str = "ordinary",
    database: Path = DEFAULT_DATABASE,
) -> DebuffRecoverExecution:
    """Snapshot then commit the bounded executor model."""

    plan = snapshot_debuff_recover(
        effect,
        state,
        play_origin=play_origin,
        database=database,
    )
    return commit_debuff_recover(plan, statuses_at_remove)


apply_debuff_recover = execute_debuff_recover


ANDROID_NATIVE_SOURCE: Final = (
    "_research/android/game-v3.2.3/cpp2il-plugin/DiffableCs/"
    "Assembly-CSharp/Campus/Ingame/Exam/DebuffRecoverEffectExecutor.cs"
)
ANDROID_DUMP_SOURCE: Final = "_research/android/game-v3.2.3/il2cppdumper/dump.cs"
ANDROID_SCRIPT_SOURCE: Final = "_research/android/game-v3.2.3/il2cppdumper/script.json"
ANDROID_EXTENSIONS_ISIL_SOURCE: Final = (
    "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
    "Assembly-CSharp/Campus/InGame/ExamExtensions.txt"
)
ANDROID_MAPPING_SOURCE: Final = (
    "_research/android/game-v3.2.3/native-analysis/create-executor-mapping.json"
)
ANDROID_ELF_SOURCE: Final = (
    "_research/android/game-v3.2.3/extracted/lib/arm64-v8a/libil2cpp.so"
)
PC_CARD_SOURCE: Final = "_research/gakumasu-diff/ProduceCard.yaml"
PC_EFFECT_SOURCE: Final = "_research/gakumasu-diff/ProduceExamEffect.yaml"
MASTER_SOURCE: Final = "var/master.sqlite3"

NATIVE_EVIDENCE: Final[dict[str, object]] = {
    "executor": "Campus.InGame.Exam.DebuffRecoverEffectExecutor",
    "effect_enum": {"ProduceExamEffectType_ExamDebuffRecover": 148},
    "constructor_va": "0x7E7658C",
    "execute_va": "0x7E76644",
    "candidate_filter": (
        "GetStatusEffectList().Where(status => "
        "GetEffectTargetType(GetEffectType(status.Type)) == Debuff)"
    ),
    "target_enum": {"None": 0, "Buff": 1, "Debuff": 2},
    "target_lookup": {
        "function_va": "0x680B478",
        "table_va": "0x273B4F0",
        "index": "ProduceExamEffectType - 1",
        "valid_input_range": "1..218",
        "debuff_values": list(NATIVE_DEBUFF_EFFECT_TYPE_VALUES),
        "buff_values": list(NATIVE_BUFF_EFFECT_TYPE_VALUES),
        "none_values": list(NATIVE_NONE_EFFECT_TYPE_VALUES),
        "unknown_values": "unnamed/altered values fail closed in this adapter",
    },
    "status_registry_order": [
        *STATUS_REGISTRY_ORDER,
    ],
    "candidate_rule": DEBUFF_CANDIDATE_RULE,
    "selection": {
        "value_ge_1": "Enumerable.Take(candidates, value1)",
        "value_lt_1": "no Take call; all candidates",
        "insufficient": "Take returns all available candidates",
        "random": False,
    },
    "removal": {
        "method": "ExamStatusEffectCollection.RemoveStatusEffect(predicate)",
        "predicate": "captured removeDebuffEffectList.Contains(status)",
        "unit": "whole status object; no layer decrement",
        "added_after_snapshot": "not selected",
        "disappeared_before_remove": "not removed; captured difference path remains selection-based",
    },
    "difference_and_callback": {
        "difference": "first selected status Type/IconValue -> DifferencePair(iconValue,0)",
        "flags": {"is_consume": False, "remove_buff_difference": True},
        "queue": "context.effectDifferenceList",
        "direct_executor_callback": False,
        "collection_callback": "owned by RemoveStatusEffect implementation, outside this executor model",
    },
}


def build_native_audit(
    catalog: Plan2DebuffRecoverCatalog | None = None,
) -> dict[str, object]:
    """Return a deterministic in-memory native audit; no central artifact write."""

    catalog = catalog or load_plan2_debuff_recover_catalog()
    return {
        "audit_id": "plan2_debuff_recover_native_audit",
        "scope": {
            "plan": "Plan2",
            "family": EFFECT_TYPE,
            "card_id": TARGET_CARD_ID,
            "target_upgrades": list(TARGET_UPGRADES),
            "bounded_standalone": True,
            "whole_card_unlocked": False,
            "runtime_agent_calls": False,
            "central_core_modified": False,
            "native_search_modified": False,
            "formal_coverage_modified": False,
            "plan3_modified": False,
            "gui_modified": False,
            "controller_modified": False,
            "retained_co_groups": list(CO_GAP_GROUPS),
        },
        "accounting": {
            "affected": BATCH_ACCOUNTING.affected,
            "direct": BATCH_ACCOUNTING.direct,
            "co_blocked": BATCH_ACCOUNTING.co_blocked,
            "co_groups": list(CO_GAP_GROUPS),
        },
        "master": {
            "source": MASTER_SOURCE,
            "versions": [version.to_dict() for version in catalog.versions],
            "slot_order": list(CARD_SLOT_ORDER_LABELS),
        },
        "native_semantics": NATIVE_EVIDENCE,
        "evidence": {
            "android_native": [
                ANDROID_NATIVE_SOURCE,
                ANDROID_DUMP_SOURCE,
                ANDROID_SCRIPT_SOURCE,
                ANDROID_EXTENSIONS_ISIL_SOURCE,
                ANDROID_MAPPING_SOURCE,
                ANDROID_ELF_SOURCE,
            ],
            "pc_metadata_cross_check": [PC_CARD_SOURCE, PC_EFFECT_SOURCE],
            "pc_native_body": {
                "available_in_local_research": False,
                "claim": "PC metadata/shape cross-check only; no PC native behaviour parity asserted",
            },
        },
        "execution_scope": {
            "mode_equivalence": list(PLAY_ORIGINS),
            "this_family": [
                "active status target classification",
                "newest-first bounded whole-object removal",
                "deferred status/remove difference record",
            ],
            "not_modeled_here": [
                "AggressiveValueMultiple executor",
                "Timer -> Block child executor",
                "card payment",
                "card history/count mutation",
                "card move mutation",
            ],
            "relative_slot_order": list(EXECUTION_ORDER),
        },
    }


native_audit = build_native_audit


__all__ = [
    "AGGRESSIVE_EFFECT_ID",
    "AGGRESSIVE_EFFECT_TYPE",
    "AGGRESSIVE_GROUP",
    "ANDROID_DUMP_SOURCE",
    "ANDROID_ELF_SOURCE",
    "ANDROID_EXTENSIONS_ISIL_SOURCE",
    "ANDROID_MAPPING_SOURCE",
    "ANDROID_NATIVE_SOURCE",
    "ANDROID_SCRIPT_SOURCE",
    "BATCH_ACCOUNTING",
    "BLOCK_EFFECT_TYPE",
    "BLOCK_GROUP",
    "CARD_ID",
    "CARD_SLOT_ORDER_LABELS",
    "CO_GAP_GROUPS",
    "CO_GROUPS",
    "DEBUFF_CANDIDATE_RULE",
    "DebuffRecoverCardVersion",
    "DebuffRecoverDifference",
    "DebuffRecoverEffectRow",
    "DebuffRecoverExecution",
    "DebuffRecoverPlan",
    "DebuffRecoverState",
    "EFFECT_ID_UPGRADE0",
    "EFFECT_ID_UPGRADE1_3",
    "EFFECT_TYPE",
    "ExamStatusEffect",
    "ExamStatusEffectTargetType",
    "GAP_AGGRESSIVE_MULTIPLE",
    "GAP_TIMER",
    "INT32_MAX",
    "INT32_MIN",
    "KNOWN_STATUS_EFFECT_TYPES",
    "MASTER_SOURCE",
    "NATIVE_BUFF_EFFECT_TYPES",
    "NATIVE_DEBUFF_EFFECT_TYPES",
    "NATIVE_EFFECT_TARGET_TYPE",
    "NATIVE_EVIDENCE",
    "NATIVE_NONE_EFFECT_TYPES",
    "NATIVE_TARGET_TYPE_VALUES",
    "PC_CARD_SOURCE",
    "PC_EFFECT_SOURCE",
    "PLAY_ORIGINS",
    "Plan2DebuffRecoverBatchAccounting",
    "Plan2DebuffRecoverCatalog",
    "Plan2DebuffRecoverContractError",
    "STATUS_TARGET_BUFF",
    "STATUS_TARGET_DEBUFF",
    "STATUS_TARGET_NONE",
    "STATUS_REGISTRY_ORDER",
    "StatusEffect",
    "TARGET_CARD_ID",
    "TARGET_CARD_NAME",
    "TARGET_EFFECT_IDS",
    "TARGET_SLOT_INDEX",
    "TARGET_UPGRADES",
    "TIMER_EFFECT_TYPE",
    "TIMER_GROUP",
    "apply_debuff_recover",
    "build_native_audit",
    "commit_debuff_recover",
    "execute_debuff_recover",
    "load_catalog",
    "load_plan2_debuff_recover_catalog",
    "native_audit",
    "resolve_effect",
    "resolve_plan2_debuff_recover",
    "resolve_status_target_type",
    "snapshot_debuff_recover",
]
