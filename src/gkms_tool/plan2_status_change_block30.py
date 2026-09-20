"""Bounded Plan2 listener for ``status_change-30-exam_block``.

This leaf owns only ``p_card-02-ido-3_130`` upgrades 0..3.  It validates the
Master graph, models the status-change listener boundary, and emits a small
handoff for the separate delay-1 Timer -> Block owner.  It intentionally does
not import the central Plan2 runtime, a native-search adapter, any Plan3
runtime, or a GUI/controller.

The native contract is deliberately narrow:

* ``ExamEffectDifferenceData`` stores one ``DifferencePair<int>`` per status
  effect entry.  The status-change dispatcher passes the signed 32-bit
  ``after - before`` difference for that entry to
  ``IsChangeEffectTrigger``.
* ``IsChangeEffectTrigger`` rejects when the trigger threshold is greater than
  that difference.  Equality therefore fires: ``diff >= 30``.
* The listener never reads current Block, BlockConsumptionSumCount,
  CurrentTurnTotalBlock, or a listener-local phase count as its metric.
* The card's status installer has unlimited total/per-turn count and a
  negative turn value.  Each successful play appends a distinct listener in
  active installation order; turns do not reset it.

Unknown snapshots, counter shapes, or Master shapes return a fail-closed
result or raise the contract error at the load boundary.  This module is a
pure offline model; no runtime agent or game-control call is made.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Final, Literal

from .master_db import DEFAULT_DATABASE


TARGET_CARD_ID: Final = "p_card-02-ido-3_130"
TARGET_UPGRADES: Final = (0, 1, 2, 3)
TARGET_CARD_NAME: Final = "憧れのアイドル"

PLAN2: Final = "ProducePlanType_Plan2"
CATEGORY_ACTIVE: Final = "ProduceCardCategory_ActiveSkill"
COST_UNKNOWN: Final = "ExamCostType_Unknown"
MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN: Final = "ProduceStepLessonType_Unknown"

PHASE_STATUS_CHANGE: Final = "ProduceExamPhaseType_ExamStatusChange"
INSTALL_PHASE: Final = "ExamCardPlay:direct-effect-slot-2"
EFFECT_BLOCK: Final = "ProduceExamEffectType_ExamBlock"
EFFECT_AGGRESSIVE: Final = "ProduceExamEffectType_ExamCardPlayAggressive"
EFFECT_TIMER: Final = "ProduceExamEffectType_ExamEffectTimer"
EFFECT_STATUS_ENCHANT: Final = "ProduceExamEffectType_ExamStatusEnchant"
EFFECT_LESSON_DEPEND_BLOCK: Final = (
    "ProduceExamEffectType_ExamLessonDependBlock"
)

TRIGGER_ID: Final = "e_trigger-exam_status_change-30-exam_block"
THRESHOLD: Final = 30

BLOCK_GROUP: Final = "effect_group-visible-exam_block-000"
TIMER_GROUP: Final = "effect_group-visible-exam_effect_timer-000"
LESSON_GROUP: Final = "effect_group-visible-exam_lesson-000"
LESSON_DEPEND_BLOCK_GROUP: Final = (
    "effect_group-visible-exam_lesson_depend_block-000"
)
STATUS_ENCHANT_GROUP: Final = "effect_group-visible-exam_status_enchant-000"
AGGRESSIVE_GROUP: Final = (
    "effect_group-visible-exam_card_play_aggressive-000"
)

PLAY_ORIGINS: Final = ("ordinary", "forced", "extra")
PERMANENT_TURN: Final = -1
UNLIMITED_COUNT: Final = -1
INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1

AGGRESSIVE_VALUES: Final = (2, 3, 5, 5)
TIMER_BLOCK_VALUES: Final = (3, 3, 3, 6)
STATUS_SUFFIXES: Final = ("enc02", "enc01", "enc01", "enc01")
STATUS_CHILD_VALUES: Final = (200, 300, 300, 300)

FORMAL_AFFECTED_VERSION_REFS: Final = tuple(
    f"{TARGET_CARD_ID}#{upgrade}" for upgrade in TARGET_UPGRADES
)
# The direct count is an expected downstream result after the separate
# TimerBlock central baseline/probe.  This leaf does not certify that central
# chaining result.
FORMAL_DIRECT_VERSION_REFS: Final = FORMAL_AFFECTED_VERSION_REFS
FORMAL_CO_BLOCKED_VERSION_REFS: Final = ()

ACCOUNTING: Final = {
    "affected_versions": 4,
    "direct_unblocked_expected_after_timer_block_central": 4,
    "co_blocked": 0,
    "direct_certified_by_this_leaf": False,
    "direct_basis": "TimerBlock central baseline/probe",
}


def _status_installer_id(suffix: str) -> str:
    return (
        "e_effect-exam_status_enchant-inf-"
        f"enchant-{TARGET_CARD_ID}-{suffix}"
    )


def _status_id(suffix: str) -> str:
    return f"enchant-{TARGET_CARD_ID}-{suffix}"


def _status_child_id(value: int) -> str:
    return f"e_effect-exam_lesson_depend_block-{value:04d}-01"


def _timer_id(value: int) -> str:
    return (
        "e_effect-exam_effect_timer-0001-01-e_effect-exam_block-"
        f"{value:04d}"
    )


def _timer_child_id(value: int) -> str:
    return f"e_effect-exam_block-{value:04d}"


def _aggressive_id(value: int) -> str:
    return f"e_effect-exam_card_play_aggressive-{value:04d}"


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30ContractError(ValueError):
    """Master/native/runtime shape is outside this bounded contract."""

    code: str
    detail: str = ""

    def __str__(self) -> str:
        return self.code if not self.detail else f"{self.code}:{self.detail}"


def _contract(code: str, detail: str = "") -> None:
    raise Plan2StatusChangeBlock30ContractError(code, detail)


def _plain_i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _contract("signed-int32-required", label)
    if not INT32_MIN <= value <= INT32_MAX:
        _contract("signed-int32-out-of-range", label)
    return value


def _i32(value: int) -> int:
    """The native W-register subtraction, interpreted as signed Int32."""

    wrapped = value & 0xFFFFFFFF
    return wrapped - 0x100000000 if wrapped & 0x80000000 else wrapped


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2StatusChangeBlock30ContractError(
            "invalid-master-json", label
        ) from error
    if not isinstance(parsed, dict):
        _contract("invalid-master-json-object", label)
    return parsed


def _json_array(value: object, label: str) -> list[object]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2StatusChangeBlock30ContractError(
            "invalid-master-json", label
        ) from error
    if not isinstance(parsed, list):
        _contract("invalid-master-json-array", label)
    return parsed


def _require_keys(raw: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(raw)
    if actual != expected:
        _contract(
            "master-key-shape-drift",
            f"{label}:missing={sorted(expected - actual)!r}:"
            f"extra={sorted(actual - expected)!r}",
        )


def _require(raw: Mapping[str, object], key: str, expected: object, label: str) -> None:
    if raw.get(key) != expected:
        _contract("master-value-shape-drift", f"{label}:{key}")


_CARD_RAW_KEYS: Final = {
    "assetId",
    "category",
    "costType",
    "costValue",
    "effectGroupIds",
    "evaluation",
    "forceStamina",
    "id",
    "isCharacterAsset",
    "isConversion",
    "isEndTurnLost",
    "isInitial",
    "isInitialDeckProduceCard",
    "isLimited",
    "isRestrict",
    "isReward",
    "libraryHidden",
    "maxCustomizeCount",
    "moveEffectTriggerType",
    "moveProduceExamEffectIds",
    "moveProduceExamTriggerIds",
    "name",
    "noDeckDuplication",
    "order",
    "originCharacterId",
    "originIdolCardId",
    "originPrimaStellaIdolCardId",
    "originSupportCardId",
    "planType",
    "playEffects",
    "playMovePositionType",
    "playProduceExamTriggerId",
    "produceCardCustomizeIds",
    "produceCardStatusEnchantId",
    "produceDescriptions",
    "rarity",
    "rentalUnlockProducerLevel",
    "searchTag",
    "stamina",
    "unlockProducerLevel",
    "upgradeCount",
    "viewStartTime",
    "voiceAssetId",
}
_EFFECT_RAW_KEYS: Final = {
    "chainProduceExamEffectId",
    "chainProduceExamEffectIds",
    "customizeProduceDescriptions",
    "effectCount",
    "effectGroupIds",
    "effectTurn",
    "effectType",
    "effectValue1",
    "effectValue2",
    "id",
    "movePositionType",
    "pickCountMax",
    "pickCountMax2",
    "pickCountMin",
    "pickCountMin2",
    "pickCountReferenceProduceCardSearchId",
    "pickCountReferenceProduceCardSearchId2",
    "pickCountType",
    "pickCountType2",
    "pickRangeType",
    "pickRangeType2",
    "produceCardGrowEffectIds",
    "produceCardSearchId",
    "produceCardSearchId2",
    "produceCardStatusEnchantId",
    "produceDescriptions",
    "produceExamStatusEnchantId",
    "targetExamEffectType",
    "targetProduceCardId",
    "targetUpgradeCount",
}
_STATUS_RAW_KEYS: Final = {
    "assetId",
    "id",
    "produceDescriptions",
    "produceExamEffectIds",
    "produceExamTriggerId",
}
_TRIGGER_RAW_KEYS: Final = {
    "cardMovePositionType",
    "effectTypes",
    "fieldStatusCheckTypes",
    "fieldStatusProduceCardSearchIds",
    "fieldStatusTypes",
    "fieldStatusValues",
    "id",
    "lessonType",
    "lowerSearchCount",
    "phaseTypes",
    "phaseValues",
    "playEffectProduceDescriptions",
    "playProduceDescriptions",
    "produceCardSearchId",
    "produceDescriptions",
    "upperSearchCount",
}
_DESCRIPTION_KEYS: Final = {
    "changeColor",
    "costValue",
    "effectCount",
    "effectValue1",
    "effectValue2",
    "examDescriptionType",
    "examEffectType",
    "isCost",
    "isOnlyOutGame",
    "originProduceCardStatusEnchantId",
    "originProduceExamEffectId",
    "originProduceExamTriggerId",
    "produceCardCategory",
    "produceCardGrowEffectType",
    "produceCardMovePositionType",
    "produceDescriptionSwapId",
    "produceDescriptionType",
    "produceStepBusinessType",
    "produceStepType",
    "targetId",
    "targetLevel",
    "text",
    "turn",
}


def _validate_descriptions(
    value: object, label: str, expected_length: int
) -> None:
    if not isinstance(value, list) or len(value) != expected_length:
        _contract("master-description-shape-drift", label)
    for index, description in enumerate(value):
        if not isinstance(description, dict):
            _contract("master-description-shape-drift", f"{label}:{index}")
        _require_keys(description, _DESCRIPTION_KEYS, f"{label}:{index}")


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30Effect:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    status_enchant_id: str = ""
    chain_effect_id: str = ""
    effect_groups: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30Trigger:
    trigger_id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...]
    field_status_check_types: tuple[str, ...]
    field_status_types: tuple[str, ...]
    field_status_values: tuple[int, ...]
    field_status_produce_card_search_ids: tuple[str, ...]
    produce_card_search_id: str
    upper_search_count: int
    lower_search_count: int
    card_move_position_type: str
    effect_types: tuple[str, ...]
    lesson_type: str

    @property
    def threshold(self) -> int:
        return self.phase_values[0]


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30Status:
    status_id: str
    trigger_id: str
    child_effect_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30CardVersion:
    card_id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    stamina: int
    force_stamina: int
    cost_type: str
    cost_value: int
    move_position_type: str
    play_effect_ids: tuple[str, ...]
    play_effect_types: tuple[str, ...]
    play_effect_values: tuple[int, ...]
    play_once_flags: tuple[bool, ...]
    aggressive_effect_id: str
    timer_effect_id: str
    status_installer_effect_id: str
    status_enchant_id: str
    status_child_effect_id: str
    status_child_value1: int
    timer_child_effect_id: str
    timer_child_value1: int
    installer_index: int = 2

    @property
    def ref(self) -> str:
        return f"{self.card_id}+{self.upgrade}"

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"

    @property
    def ordered_slots(self) -> tuple[str, ...]:
        return self.play_effect_ids


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30Program:
    cards: tuple[Plan2StatusChangeBlock30CardVersion, ...]
    trigger: Plan2StatusChangeBlock30Trigger
    statuses: tuple[Plan2StatusChangeBlock30Status, ...]
    effects: tuple[Plan2StatusChangeBlock30Effect, ...]
    listener_limit_count: int = UNLIMITED_COUNT
    listener_limit_count_in_turn: int = UNLIMITED_COUNT
    listener_turn: int = PERMANENT_TURN

    @property
    def trigger_id(self) -> str:
        return self.trigger.trigger_id

    @property
    def threshold(self) -> int:
        return self.trigger.threshold

    def card(self, upgrade: int) -> Plan2StatusChangeBlock30CardVersion:
        for card in self.cards:
            if card.upgrade == upgrade:
                return card
        _contract("unintegrated-card-version", f"{TARGET_CARD_ID}+{upgrade}")

    def status(self, status_id: str) -> Plan2StatusChangeBlock30Status:
        for status in self.statuses:
            if status.status_id == status_id:
                return status
        _contract("unintegrated-status", status_id)

    def effect(self, effect_id: str) -> Plan2StatusChangeBlock30Effect:
        for effect in self.effects:
            if effect.effect_id == effect_id:
                return effect
        _contract("unintegrated-effect", effect_id)


def _expected_card(upgrade: int) -> Plan2StatusChangeBlock30CardVersion:
    aggressive_value = AGGRESSIVE_VALUES[upgrade]
    timer_value = TIMER_BLOCK_VALUES[upgrade]
    suffix = STATUS_SUFFIXES[upgrade]
    status_value = STATUS_CHILD_VALUES[upgrade]
    aggressive_id = _aggressive_id(aggressive_value)
    timer_effect_id = _timer_id(timer_value)
    installer_id = _status_installer_id(suffix)
    return Plan2StatusChangeBlock30CardVersion(
        card_id=TARGET_CARD_ID,
        upgrade=upgrade,
        name=TARGET_CARD_NAME + ("+" * upgrade),
        plan_type=PLAN2,
        category=CATEGORY_ACTIVE,
        stamina=0,
        force_stamina=2,
        cost_type=COST_UNKNOWN,
        cost_value=0,
        move_position_type=MOVE_LOST,
        play_effect_ids=(aggressive_id, timer_effect_id, installer_id),
        play_effect_types=(EFFECT_AGGRESSIVE, EFFECT_TIMER, EFFECT_STATUS_ENCHANT),
        play_effect_values=(aggressive_value, 1, 0),
        play_once_flags=(False, False, False),
        aggressive_effect_id=aggressive_id,
        timer_effect_id=timer_effect_id,
        status_installer_effect_id=installer_id,
        status_enchant_id=_status_id(suffix),
        status_child_effect_id=_status_child_id(status_value),
        status_child_value1=status_value,
        timer_child_effect_id=_timer_child_id(timer_value),
        timer_child_value1=timer_value,
    )


def _expected_effects(
    cards: Sequence[Plan2StatusChangeBlock30CardVersion],
) -> dict[str, Plan2StatusChangeBlock30Effect]:
    expected: dict[str, Plan2StatusChangeBlock30Effect] = {}

    def add(effect: Plan2StatusChangeBlock30Effect) -> None:
        previous = expected.get(effect.effect_id)
        if previous is not None and previous != effect:
            _contract("duplicate-effect-shape-drift", effect.effect_id)
        expected[effect.effect_id] = effect

    for card in cards:
        add(
            Plan2StatusChangeBlock30Effect(
                card.aggressive_effect_id,
                EFFECT_AGGRESSIVE,
                card.play_effect_values[0],
                0,
                0,
                0,
                effect_groups=(AGGRESSIVE_GROUP,),
            )
        )
        add(
            Plan2StatusChangeBlock30Effect(
                card.timer_effect_id,
                EFFECT_TIMER,
                1,
                0,
                1,
                0,
                chain_effect_id=card.timer_child_effect_id,
                effect_groups=(BLOCK_GROUP, TIMER_GROUP),
            )
        )
        add(
            Plan2StatusChangeBlock30Effect(
                card.timer_child_effect_id,
                EFFECT_BLOCK,
                card.timer_child_value1,
                0,
                0,
                0,
                effect_groups=(BLOCK_GROUP,),
            )
        )
        add(
            Plan2StatusChangeBlock30Effect(
                card.status_installer_effect_id,
                EFFECT_STATUS_ENCHANT,
                0,
                0,
                0,
                PERMANENT_TURN,
                status_enchant_id=card.status_enchant_id,
                effect_groups=(
                    LESSON_DEPEND_BLOCK_GROUP,
                    LESSON_GROUP,
                    STATUS_ENCHANT_GROUP,
                ),
            )
        )
        add(
            Plan2StatusChangeBlock30Effect(
                card.status_child_effect_id,
                EFFECT_LESSON_DEPEND_BLOCK,
                card.status_child_value1,
                0,
                1,
                0,
                effect_groups=(LESSON_DEPEND_BLOCK_GROUP, LESSON_GROUP),
            )
        )
    return expected


def _expected_trigger() -> Plan2StatusChangeBlock30Trigger:
    return Plan2StatusChangeBlock30Trigger(
        trigger_id=TRIGGER_ID,
        phase_types=(PHASE_STATUS_CHANGE,),
        phase_values=(THRESHOLD,),
        field_status_check_types=(),
        field_status_types=(),
        field_status_values=(),
        field_status_produce_card_search_ids=(),
        produce_card_search_id="",
        upper_search_count=0,
        lower_search_count=0,
        card_move_position_type=MOVE_UNKNOWN,
        effect_types=(EFFECT_BLOCK,),
        lesson_type=LESSON_UNKNOWN,
    )


def _validate_card_raw(
    raw: Mapping[str, object], card: Plan2StatusChangeBlock30CardVersion
) -> None:
    _require_keys(raw, _CARD_RAW_KEYS, card.ref)
    for key, expected in (
        ("id", card.card_id),
        ("upgradeCount", card.upgrade),
        ("name", card.name),
        ("planType", PLAN2),
        ("category", CATEGORY_ACTIVE),
        ("stamina", 0),
        ("forceStamina", 2),
        ("costType", COST_UNKNOWN),
        ("costValue", 0),
        ("playProduceExamTriggerId", ""),
        ("playMovePositionType", MOVE_LOST),
        ("moveEffectTriggerType", "ProduceCardMoveEffectTriggerType_Unknown"),
        ("moveProduceExamEffectIds", []),
        ("moveProduceExamTriggerIds", []),
        ("produceCardStatusEnchantId", ""),
        ("produceCardCustomizeIds", []),
        ("maxCustomizeCount", 0),
        ("isInitial", False),
        ("isRestrict", False),
        ("isEndTurnLost", False),
        ("isConversion", False),
        ("isInitialDeckProduceCard", False),
        ("isLimited", False),
        ("isReward", False),
        ("libraryHidden", False),
        ("noDeckDuplication", True),
        (
            "effectGroupIds",
            [
                LESSON_DEPEND_BLOCK_GROUP,
                LESSON_GROUP,
                BLOCK_GROUP,
                STATUS_ENCHANT_GROUP,
                TIMER_GROUP,
                AGGRESSIVE_GROUP,
            ],
        ),
    ):
        _require(raw, key, expected, card.ref)
    play_effects = raw.get("playEffects")
    expected_play_effects = [
        {
            "produceExamTriggerId": "",
            "produceExamEffectId": effect_id,
            "hideIcon": False,
            "isOncePlayEffect": False,
        }
        for effect_id in card.play_effect_ids
    ]
    if play_effects != expected_play_effects:
        _contract("ordered-play-slot-drift", card.ref)
    _validate_descriptions(raw.get("produceDescriptions"), card.ref, 38)


def _validate_effect_raw(
    raw: Mapping[str, object], effect: Plan2StatusChangeBlock30Effect
) -> None:
    _require_keys(raw, _EFFECT_RAW_KEYS, effect.effect_id)
    for key, expected in (
        ("id", effect.effect_id),
        ("effectType", effect.effect_type),
        ("effectValue1", effect.value1),
        ("effectValue2", effect.value2),
        ("effectCount", effect.count),
        ("effectTurn", effect.turn),
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
        ("chainProduceExamEffectId", effect.chain_effect_id),
        ("chainProduceExamEffectIds", []),
        ("produceExamStatusEnchantId", effect.status_enchant_id),
        ("produceCardStatusEnchantId", ""),
        ("produceCardGrowEffectIds", []),
        ("effectGroupIds", list(effect.effect_groups)),
    ):
        _require(raw, key, expected, effect.effect_id)
    description_lengths = {
        EFFECT_AGGRESSIVE: (4, 5),
        EFFECT_TIMER: (6, 7),
        EFFECT_BLOCK: (4, 5),
        EFFECT_STATUS_ENCHANT: (13, 13),
        EFFECT_LESSON_DEPEND_BLOCK: (7, 8),
    }
    produce_length, customize_length = description_lengths[effect.effect_type]
    _validate_descriptions(
        raw.get("produceDescriptions"), effect.effect_id, produce_length
    )
    _validate_descriptions(
        raw.get("customizeProduceDescriptions"),
        f"{effect.effect_id}:customize",
        customize_length,
    )


def _validate_status_raw(
    raw: Mapping[str, object], status: Plan2StatusChangeBlock30Status
) -> None:
    _require_keys(raw, _STATUS_RAW_KEYS, status.status_id)
    _require(raw, "id", status.status_id, status.status_id)
    _require(raw, "assetId", "", status.status_id)
    _require(raw, "produceExamTriggerId", status.trigger_id, status.status_id)
    _require(raw, "produceExamEffectIds", list(status.child_effect_ids), status.status_id)
    _validate_descriptions(raw.get("produceDescriptions"), status.status_id, 14)


def _validate_trigger_raw(
    raw: Mapping[str, object], trigger: Plan2StatusChangeBlock30Trigger
) -> None:
    _require_keys(raw, _TRIGGER_RAW_KEYS, trigger.trigger_id)
    expected = {
        "id": trigger.trigger_id,
        "phaseTypes": list(trigger.phase_types),
        "phaseValues": list(trigger.phase_values),
        "fieldStatusCheckTypes": list(trigger.field_status_check_types),
        "fieldStatusTypes": list(trigger.field_status_types),
        "fieldStatusValues": list(trigger.field_status_values),
        "fieldStatusProduceCardSearchIds": list(
            trigger.field_status_produce_card_search_ids
        ),
        "produceCardSearchId": trigger.produce_card_search_id,
        "upperSearchCount": trigger.upper_search_count,
        "lowerSearchCount": trigger.lower_search_count,
        "cardMovePositionType": trigger.card_move_position_type,
        "effectTypes": list(trigger.effect_types),
        "lessonType": trigger.lesson_type,
    }
    for key, value in expected.items():
        _require(raw, key, value, trigger.trigger_id)
    _validate_descriptions(raw.get("produceDescriptions"), trigger.trigger_id, 4)
    _validate_descriptions(
        raw.get("playProduceDescriptions"),
        f"{trigger.trigger_id}:play",
        1,
    )
    _validate_descriptions(
        raw.get("playEffectProduceDescriptions"),
        f"{trigger.trigger_id}:play-effect",
        1,
    )


def _parse_tuple(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        _contract("master-array-shape-drift", label)
    return tuple(value)


def _read_connection(database: Path) -> sqlite3.Connection:
    if not database.is_file():
        _contract("master-database-missing", str(database))
    try:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as error:
        raise Plan2StatusChangeBlock30ContractError(
            "master-database-open-failed", str(database)
        ) from error


def load_plan2_status_change_block30_program(
    database: Path = DEFAULT_DATABASE,
) -> Plan2StatusChangeBlock30Program:
    """Load and validate the exact four-version Master slice."""

    cards = tuple(_expected_card(upgrade) for upgrade in TARGET_UPGRADES)
    trigger = _expected_trigger()
    expected_effects = _expected_effects(cards)
    statuses = tuple(
        Plan2StatusChangeBlock30Status(
            card.status_enchant_id,
            TRIGGER_ID,
            (card.status_child_effect_id,),
        )
        for card in cards
    )
    try:
        with closing(_read_connection(Path(database))) as connection:
            card_rows = connection.execute(
                "SELECT * FROM card WHERE id = ? ORDER BY upgrade_count",
                (TARGET_CARD_ID,),
            ).fetchall()
            if tuple(row["upgrade_count"] for row in card_rows) != TARGET_UPGRADES:
                _contract("card-upgrade-slice-drift", TARGET_CARD_ID)
            for row, card in zip(card_rows, cards, strict=True):
                if (
                    row["id"],
                    row["name"],
                    row["plan_type"],
                    row["category"],
                    row["stamina"],
                    row["cost_type"],
                    row["cost_value"],
                    row["play_trigger_id"],
                    row["move_position_type"],
                ) != (
                    card.card_id,
                    card.name,
                    PLAN2,
                    CATEGORY_ACTIVE,
                    0,
                    COST_UNKNOWN,
                    0,
                    "",
                    MOVE_LOST,
                ):
                    _contract("card-normalized-shape-drift", card.ref)
                refs = _json_array(row["play_effects_json"], f"{card.ref}:slots")
                expected_refs = [
                    {
                        "produceExamTriggerId": "",
                        "produceExamEffectId": effect_id,
                        "hideIcon": False,
                        "isOncePlayEffect": False,
                    }
                    for effect_id in card.play_effect_ids
                ]
                if refs != expected_refs:
                    _contract("ordered-play-slot-drift", card.ref)
                _validate_card_raw(_json_object(row["raw_json"], card.ref), card)

            for effect_id, expected in expected_effects.items():
                row = connection.execute(
                    "SELECT * FROM effect WHERE id = ?", (effect_id,)
                ).fetchone()
                if row is None:
                    _contract("master-effect-missing", effect_id)
                if (
                    row["id"],
                    row["effect_type"],
                    row["value1"],
                    row["value2"],
                    row["effect_count"],
                    row["effect_turn"],
                    row["status_enchant_id"],
                    row["chain_effect_id"],
                ) != (
                    expected.effect_id,
                    expected.effect_type,
                    expected.value1,
                    expected.value2,
                    expected.count,
                    expected.turn,
                    expected.status_enchant_id,
                    expected.chain_effect_id,
                ):
                    _contract("effect-normalized-shape-drift", effect_id)
                _validate_effect_raw(_json_object(row["raw_json"], effect_id), expected)

            for status in statuses:
                row = connection.execute(
                    "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
                    (status.status_id,),
                ).fetchone()
                if row is None:
                    _contract("master-status-missing", status.status_id)
                child_ids = tuple(_json_array(row["produce_exam_effect_ids_json"], status.status_id))
                if (
                    row["id"],
                    row["asset_id"],
                    row["produce_exam_trigger_id"],
                    child_ids,
                ) != (status.status_id, "", TRIGGER_ID, status.child_effect_ids):
                    _contract("status-normalized-shape-drift", status.status_id)
                _validate_status_raw(_json_object(row["raw_json"], status.status_id), status)

            row = connection.execute(
                "SELECT * FROM produce_exam_trigger WHERE id = ?", (TRIGGER_ID,)
            ).fetchone()
            if row is None:
                _contract("master-trigger-missing", TRIGGER_ID)
            normalized = (
                tuple(_json_array(row["phase_types_json"], TRIGGER_ID)),
                tuple(_json_array(row["phase_values_json"], TRIGGER_ID)),
                tuple(_json_array(row["field_status_check_types_json"], TRIGGER_ID)),
                tuple(_json_array(row["field_status_types_json"], TRIGGER_ID)),
                tuple(_json_array(row["field_status_values_json"], TRIGGER_ID)),
                tuple(_json_array(row["field_status_produce_card_search_ids_json"], TRIGGER_ID)),
                row["produce_card_search_id"],
                row["upper_search_count"],
                row["lower_search_count"],
                row["card_move_position_type"],
                tuple(_json_array(row["effect_types_json"], TRIGGER_ID)),
                row["lesson_type"],
            )
            expected_normalized = (
                trigger.phase_types,
                trigger.phase_values,
                trigger.field_status_check_types,
                trigger.field_status_types,
                trigger.field_status_values,
                trigger.field_status_produce_card_search_ids,
                trigger.produce_card_search_id,
                trigger.upper_search_count,
                trigger.lower_search_count,
                trigger.card_move_position_type,
                trigger.effect_types,
                trigger.lesson_type,
            )
            if normalized != expected_normalized:
                _contract("trigger-normalized-shape-drift", TRIGGER_ID)
            _validate_trigger_raw(_json_object(row["raw_json"], TRIGGER_ID), trigger)
    except sqlite3.Error as error:
        raise Plan2StatusChangeBlock30ContractError(
            "master-query-failed", TARGET_CARD_ID
        ) from error

    return Plan2StatusChangeBlock30Program(
        cards=cards,
        trigger=trigger,
        statuses=statuses,
        effects=tuple(expected_effects.values()),
    )


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30Listener:
    status_uid: int
    status_enchant_id: str
    install_sequence: int
    upgrade: int
    play_origin: str
    trigger_id: str = TRIGGER_ID
    remaining_count: int = UNLIMITED_COUNT
    remaining_count_in_turn: int = UNLIMITED_COUNT
    remaining_turns: int = PERMANENT_TURN
    phase_counts: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30Scheduler:
    listeners: tuple[Plan2StatusChangeBlock30Listener, ...] = ()
    turn: int = 0
    history: tuple[str, ...] = ()
    next_status_uid: int = 1


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30Install:
    before: Plan2StatusChangeBlock30Scheduler
    after: Plan2StatusChangeBlock30Scheduler
    card: Plan2StatusChangeBlock30CardVersion
    listener: Plan2StatusChangeBlock30Listener
    event_trace: tuple[str, ...]
    direct_effect_order: tuple[str, ...]
    move_position_type: str


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30Event:
    before_block: int
    after_block: int
    source_effect_type: str = EFFECT_BLOCK
    phase_type: str = PHASE_STATUS_CHANGE
    committed: bool = True
    is_consumption: bool = False
    post_commit_snapshot_known: bool = True
    restriction_active: bool | None = False
    cap_applied: bool | None = False
    turn: int = 0
    transaction_id: str = ""
    # These fields are accepted only as observations.  The trigger predicate
    # does not read them; they make the non-metric boundary explicit.
    current_block: int | None = None
    block_consumption_sum_count: int | None = None
    current_turn_total_block: int | None = None
    listener_local_count: int | None = None

    def __post_init__(self) -> None:
        _plain_i32(self.before_block, "before_block")
        _plain_i32(self.after_block, "after_block")
        _plain_i32(self.turn, "turn")
        if not isinstance(self.committed, bool):
            _contract("event-committed-shape-drift")
        if not isinstance(self.is_consumption, bool):
            _contract("event-consumption-shape-drift")
        if not isinstance(self.post_commit_snapshot_known, bool):
            _contract("event-snapshot-shape-drift")
        for label, value in (
            ("restriction_active", self.restriction_active),
            ("cap_applied", self.cap_applied),
        ):
            if value is not None and not isinstance(value, bool):
                _contract("event-flag-shape-drift", label)
        for label, value in (
            ("current_block", self.current_block),
            ("block_consumption_sum_count", self.block_consumption_sum_count),
            ("current_turn_total_block", self.current_turn_total_block),
            ("listener_local_count", self.listener_local_count),
        ):
            if value is not None:
                _plain_i32(value, label)

    @property
    def delta(self) -> int:
        return _i32(self.after_block - self.before_block)


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30ChildCommand:
    listener_uid: int
    status_enchant_id: str
    trigger_id: str
    child_effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    move_position_type: str = MOVE_UNKNOWN


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30Evaluation:
    before: Plan2StatusChangeBlock30Scheduler
    after: Plan2StatusChangeBlock30Scheduler
    event: Plan2StatusChangeBlock30Event | None
    resolved: bool
    triggered: bool
    delta: int | None
    fail_closed_reason: str | None
    queued_children: tuple[Plan2StatusChangeBlock30ChildCommand, ...]
    executed_children: tuple[Plan2StatusChangeBlock30ChildCommand, ...]
    fired_listener_uids: tuple[int, ...]
    event_trace: tuple[str, ...]
    history: tuple[str, ...]
    move_position_type: str | None


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30Transaction:
    before: Plan2StatusChangeBlock30Scheduler
    after: Plan2StatusChangeBlock30Scheduler
    event_results: tuple[Plan2StatusChangeBlock30Evaluation, ...]
    resolved: bool
    fail_closed_reason: str | None
    queued_children: tuple[Plan2StatusChangeBlock30ChildCommand, ...]
    executed_children: tuple[Plan2StatusChangeBlock30ChildCommand, ...]
    event_trace: tuple[str, ...]
    history: tuple[str, ...]


def _validate_program(program: Plan2StatusChangeBlock30Program) -> None:
    if not isinstance(program, Plan2StatusChangeBlock30Program):
        _contract("program-type-unsupported")
    if tuple(card.upgrade for card in program.cards) != TARGET_UPGRADES:
        _contract("program-card-slice-drift")
    if program.trigger != _expected_trigger():
        _contract("program-trigger-shape-drift")
    if (
        program.listener_limit_count,
        program.listener_limit_count_in_turn,
        program.listener_turn,
    ) != (UNLIMITED_COUNT, UNLIMITED_COUNT, PERMANENT_TURN):
        _contract("program-listener-counter-shape-drift")


def _validate_scheduler(
    scheduler: Plan2StatusChangeBlock30Scheduler,
    program: Plan2StatusChangeBlock30Program,
) -> None:
    if not isinstance(scheduler, Plan2StatusChangeBlock30Scheduler):
        _contract("scheduler-type-unsupported")
    _plain_i32(scheduler.turn, "scheduler.turn")
    if isinstance(scheduler.next_status_uid, bool) or scheduler.next_status_uid <= 0:
        _contract("scheduler-next-uid-shape-drift")
    expected_sequence = 1
    seen_uids: set[int] = set()
    for listener in scheduler.listeners:
        if not isinstance(listener, Plan2StatusChangeBlock30Listener):
            _contract("listener-type-unsupported")
        if listener.status_uid in seen_uids:
            _contract("listener-uid-duplicate")
        seen_uids.add(listener.status_uid)
        if (
            listener.status_uid,
            listener.install_sequence,
            listener.trigger_id,
            listener.remaining_count,
            listener.remaining_count_in_turn,
            listener.remaining_turns,
            listener.phase_counts,
        ) != (
            expected_sequence,
            expected_sequence,
            TRIGGER_ID,
            UNLIMITED_COUNT,
            UNLIMITED_COUNT,
            PERMANENT_TURN,
            (),
        ):
            _contract("listener-shape-drift", str(listener.status_uid))
        if listener.play_origin not in PLAY_ORIGINS:
            _contract("listener-play-origin-shape-drift", str(listener.status_uid))
        program.status(listener.status_enchant_id)
        program.card(listener.upgrade)
        expected_sequence += 1
    if scheduler.next_status_uid != expected_sequence:
        _contract("scheduler-next-uid-drift")


def _fail_closed_evaluation(
    scheduler: Plan2StatusChangeBlock30Scheduler,
    event: Plan2StatusChangeBlock30Event | None,
    reason: str,
    *,
    trace: tuple[str, ...] = (),
    delta: int | None = None,
) -> Plan2StatusChangeBlock30Evaluation:
    return Plan2StatusChangeBlock30Evaluation(
        before=scheduler,
        after=scheduler,
        event=event,
        resolved=False,
        triggered=False,
        delta=delta,
        fail_closed_reason=reason,
        queued_children=(),
        executed_children=(),
        fired_listener_uids=(),
        event_trace=trace + (f"fail-closed:{reason}",),
        history=scheduler.history,
        move_position_type=None,
    )


def install_plan2_status_change_block30_listener(
    scheduler: Plan2StatusChangeBlock30Scheduler,
    program: Plan2StatusChangeBlock30Program,
    *,
    card_id: str = TARGET_CARD_ID,
    upgrade: int,
    play_origin: Literal["ordinary", "forced", "extra"] = "ordinary",
) -> Plan2StatusChangeBlock30Install:
    """Install one fresh permanent listener after the three direct slots."""

    _validate_program(program)
    _validate_scheduler(scheduler, program)
    if card_id != TARGET_CARD_ID:
        _contract("unintegrated-card", card_id)
    if play_origin not in PLAY_ORIGINS:
        _contract("play-origin-unsupported", str(play_origin))
    card = program.card(upgrade)
    uid = scheduler.next_status_uid
    listener = Plan2StatusChangeBlock30Listener(
        status_uid=uid,
        status_enchant_id=card.status_enchant_id,
        install_sequence=uid,
        upgrade=upgrade,
        play_origin=play_origin,
    )
    after = replace(
        scheduler,
        listeners=scheduler.listeners + (listener,),
        next_status_uid=uid + 1,
        history=scheduler.history
        + (
            f"card-play:{play_origin}:{card.ref}:listener={uid}",
            f"card-move:{card.ref}:{MOVE_LOST}",
        ),
    )
    return Plan2StatusChangeBlock30Install(
        before=scheduler,
        after=after,
        card=card,
        listener=listener,
        event_trace=(
            "card-play:effect:0:ExamCardPlayAggressive",
            "card-play:effect:1:ExamEffectTimer",
            "card-play:effect:2:ExamStatusEnchant",
            f"listener:install-phase:{INSTALL_PHASE}",
            f"listener:install:{uid}:limit=-1:per-turn=-1:turn=-1",
        ),
        direct_effect_order=card.play_effect_ids,
        move_position_type=card.move_position_type,
    )


def _child_for_listener(
    listener: Plan2StatusChangeBlock30Listener,
    program: Plan2StatusChangeBlock30Program,
) -> Plan2StatusChangeBlock30ChildCommand:
    status = program.status(listener.status_enchant_id)
    if status.trigger_id != TRIGGER_ID or len(status.child_effect_ids) != 1:
        _contract("listener-status-shape-drift", listener.status_enchant_id)
    effect = program.effect(status.child_effect_ids[0])
    if (
        effect.effect_type != EFFECT_LESSON_DEPEND_BLOCK
        or effect.count != 1
        or effect.turn != 0
        or effect.value2 != 0
        or effect.status_enchant_id
        or effect.chain_effect_id
    ):
        _contract("listener-child-shape-drift", effect.effect_id)
    return Plan2StatusChangeBlock30ChildCommand(
        listener_uid=listener.status_uid,
        status_enchant_id=listener.status_enchant_id,
        trigger_id=TRIGGER_ID,
        child_effect_id=effect.effect_id,
        effect_type=effect.effect_type,
        value1=effect.value1,
        value2=effect.value2,
        count=effect.count,
        turn=effect.turn,
    )


def evaluate_plan2_status_change_block30(
    scheduler: Plan2StatusChangeBlock30Scheduler,
    event: Plan2StatusChangeBlock30Event,
    program: Plan2StatusChangeBlock30Program,
) -> Plan2StatusChangeBlock30Evaluation:
    """Evaluate exactly one committed status difference.

    A positive target event is evaluated per stored difference entry.  No
    accumulator or current-field value is consulted.  Unknown post-commit or
    restriction/cap snapshots do not produce a speculative child.
    """

    try:
        _validate_program(program)
        _validate_scheduler(scheduler, program)
        if not isinstance(event, Plan2StatusChangeBlock30Event):
            _contract("event-type-unsupported")
    except Plan2StatusChangeBlock30ContractError as error:
        return _fail_closed_evaluation(scheduler, event, str(error))

    delta = event.delta
    trace = (
        f"status-change:commit:{event.source_effect_type}:"
        f"{event.before_block}->{event.after_block}:delta={delta}",
    )
    if not event.committed:
        return Plan2StatusChangeBlock30Evaluation(
            scheduler,
            scheduler,
            event,
            True,
            False,
            delta,
            None,
            (),
            (),
            (),
            trace + ("status-change:uncommitted:no-dispatch",),
            scheduler.history,
            None,
        )
    if not event.post_commit_snapshot_known:
        return _fail_closed_evaluation(
            scheduler,
            event,
            "post-commit-snapshot-unknown",
            trace=trace,
            delta=delta,
        )
    if event.phase_type != PHASE_STATUS_CHANGE:
        return _fail_closed_evaluation(
            scheduler,
            event,
            "status-change-phase-unknown",
            trace=trace,
            delta=delta,
        )
    if event.restriction_active is None:
        return _fail_closed_evaluation(
            scheduler,
            event,
            "block-restriction-snapshot-unknown",
            trace=trace,
            delta=delta,
        )
    if event.cap_applied is None:
        return _fail_closed_evaluation(
            scheduler,
            event,
            "block-cap-snapshot-unknown",
            trace=trace,
            delta=delta,
        )
    if (
        event.source_effect_type == EFFECT_BLOCK
        and event.restriction_active
        and not event.is_consumption
        and delta != 0
    ):
        return _fail_closed_evaluation(
            scheduler,
            event,
            "restricted-block-nonzero-actual-difference",
            trace=trace,
            delta=delta,
        )
    if event.source_effect_type != EFFECT_BLOCK:
        return Plan2StatusChangeBlock30Evaluation(
            scheduler,
            scheduler,
            event,
            True,
            False,
            delta,
            None,
            (),
            (),
            (),
            trace + ("status-change:effect-filter:no-ExamBlock",),
            scheduler.history,
            None,
        )
    if event.is_consumption:
        if delta > 0:
            return _fail_closed_evaluation(
                scheduler,
                event,
                "consumption-positive-difference-shape-drift",
                trace=trace,
                delta=delta,
            )
        return Plan2StatusChangeBlock30Evaluation(
            scheduler,
            scheduler,
            event,
            True,
            False,
            delta,
            None,
            (),
            (),
            (),
            trace + ("status-change:consume-route:no-positive-change",),
            scheduler.history,
            None,
        )
    if delta < THRESHOLD:
        return Plan2StatusChangeBlock30Evaluation(
            scheduler,
            scheduler,
            event,
            True,
            False,
            delta,
            None,
            (),
            (),
            (),
            trace + (f"status-change:threshold:delta<{THRESHOLD}",),
            scheduler.history,
            None,
        )

    queued: list[Plan2StatusChangeBlock30ChildCommand] = []
    local_trace = list(trace) + [
        "status-change:dispatch:active-listener-install-order",
    ]
    fired: list[int] = []
    for listener in scheduler.listeners:
        child = _child_for_listener(listener, program)
        fired.append(listener.status_uid)
        queued.append(child)
        local_trace.extend(
            (
                f"status-change:listener:{listener.status_uid}:open",
                f"status-change:listener:{listener.status_uid}:queue:{child.child_effect_id}",
                f"status-change:listener:{listener.status_uid}:SpendCount:unlimited-no-op",
                f"status-change:listener:{listener.status_uid}:close",
            )
        )
    executed = tuple(queued)
    local_trace.extend(
        f"status-change:execute-child:{child.child_effect_id}:count={child.count}:turn={child.turn}"
        for child in executed
    )
    history = scheduler.history + (
        f"status-change:{EFFECT_BLOCK}:delta={delta}:listeners={tuple(fired)!r}",
        *(f"child-executed:{child.child_effect_id}" for child in executed),
    )
    after = replace(scheduler, history=history)
    return Plan2StatusChangeBlock30Evaluation(
        scheduler,
        after,
        event,
        True,
        bool(fired),
        delta,
        None,
        tuple(queued),
        executed,
        tuple(fired),
        tuple(local_trace),
        history,
        MOVE_UNKNOWN,
    )


def evaluate_plan2_status_change_block30_transaction(
    scheduler: Plan2StatusChangeBlock30Scheduler,
    events: Iterable[Plan2StatusChangeBlock30Event],
    program: Plan2StatusChangeBlock30Program,
) -> Plan2StatusChangeBlock30Transaction:
    """Process each stored difference entry independently and in order."""

    before = scheduler
    try:
        event_tuple = tuple(events)
    except TypeError:
        return Plan2StatusChangeBlock30Transaction(
            before,
            before,
            (),
            False,
            "event-sequence-unsupported",
            (),
            (),
            ("fail-closed:event-sequence-unsupported",),
            before.history,
        )
    current = scheduler
    results: list[Plan2StatusChangeBlock30Evaluation] = []
    queued: list[Plan2StatusChangeBlock30ChildCommand] = []
    executed: list[Plan2StatusChangeBlock30ChildCommand] = []
    trace: list[str] = []
    for event in event_tuple:
        result = evaluate_plan2_status_change_block30(current, event, program)
        results.append(result)
        trace.extend(result.event_trace)
        if not result.resolved:
            return Plan2StatusChangeBlock30Transaction(
                before,
                before,
                tuple(results),
                False,
                result.fail_closed_reason,
                (),
                (),
                tuple(trace),
                before.history,
            )
        current = result.after
        queued.extend(result.queued_children)
        executed.extend(result.executed_children)
    return Plan2StatusChangeBlock30Transaction(
        before,
        current,
        tuple(results),
        True,
        None,
        tuple(queued),
        tuple(executed),
        tuple(trace),
        current.history,
    )


def advance_plan2_status_change_block30_turn(
    scheduler: Plan2StatusChangeBlock30Scheduler,
    program: Plan2StatusChangeBlock30Program,
) -> Plan2StatusChangeBlock30Scheduler:
    """Advance the encounter turn without resetting this permanent listener."""

    _validate_program(program)
    _validate_scheduler(scheduler, program)
    next_turn = _plain_i32(scheduler.turn + 1, "next_turn")
    return replace(
        scheduler,
        turn=next_turn,
        history=scheduler.history
        + (f"turn-change:{scheduler.turn}->{next_turn}:listeners-persist",),
    )


@dataclass(frozen=True, slots=True)
class Plan2StatusChangeBlock30HandoffRow:
    version_ref: str
    upgrade: int
    ordered_direct_slots: tuple[str, ...]
    timer_effect_id: str
    timer_delay: int
    timer_child_effect_id: str
    timer_child_value1: int
    status_installer_effect_id: str
    status_trigger_id: str
    status_child_effect_id: str
    status_child_value1: int
    status_child_count: int
    status_child_turn: int
    card_move_position_type: str
    remaining_status_blockers: tuple[str, ...]
    direct_after_timer_block_central_expected: bool
    direct_baseline_probe_required: bool


def build_plan2_status_change_block30_handoff(
    program: Plan2StatusChangeBlock30Program,
) -> tuple[Plan2StatusChangeBlock30HandoffRow, ...]:
    """Return the four ordered rows handed to the TimerBlock central owner."""

    _validate_program(program)
    rows = []
    for card in program.cards:
        rows.append(
            Plan2StatusChangeBlock30HandoffRow(
                version_ref=card.version_ref,
                upgrade=card.upgrade,
                ordered_direct_slots=card.ordered_slots,
                timer_effect_id=card.timer_effect_id,
                timer_delay=1,
                timer_child_effect_id=card.timer_child_effect_id,
                timer_child_value1=card.timer_child_value1,
                status_installer_effect_id=card.status_installer_effect_id,
                status_trigger_id=TRIGGER_ID,
                status_child_effect_id=card.status_child_effect_id,
                status_child_value1=card.status_child_value1,
                status_child_count=1,
                status_child_turn=0,
                card_move_position_type=card.move_position_type,
                remaining_status_blockers=(),
                direct_after_timer_block_central_expected=True,
                direct_baseline_probe_required=True,
            )
        )
    return tuple(rows)


def exact_plan2_status_change_block30_rows(
    program: Plan2StatusChangeBlock30Program,
) -> tuple[dict[str, object], ...]:
    """Expose four ordered, audit-friendly rows without mutable runtime state."""

    _validate_program(program)
    return tuple(
        {
            "version_ref": card.version_ref,
            "upgrade": card.upgrade,
            "card_id": card.card_id,
            "name": card.name,
            "ordered_direct_slots": list(card.play_effect_ids),
            "aggressive_value1": card.play_effect_values[0],
            "timer_delay": 1,
            "timer_child_effect_id": card.timer_child_effect_id,
            "timer_child_value1": card.timer_child_value1,
            "status_installer_effect_id": card.status_installer_effect_id,
            "status_enchant_id": card.status_enchant_id,
            "status_trigger_id": TRIGGER_ID,
            "status_child_effect_id": card.status_child_effect_id,
            "status_child_value1": card.status_child_value1,
            "status_child_count": 1,
            "status_child_turn": 0,
            "listener_limit_count": UNLIMITED_COUNT,
            "listener_limit_count_in_turn": UNLIMITED_COUNT,
            "listener_turn": PERMANENT_TURN,
            "play_origins": list(PLAY_ORIGINS),
            "card_move_position_type": card.move_position_type,
            "remaining_status_blockers": [],
        }
        for card in program.cards
    )


ANDROID_STATUS_CHANGE_BLOCK30_EVIDENCE: Final = {
    "version": "Android v3.2.3",
    "difference_dispatch": {
        "set_block_va": "0x7EBB524",
        "set_status_difference_va": "0x7E56AE4",
        "set_trigger_status_difference_va": "0x7E56DE0",
        "change_effect_list_va": "0x7EA38E4",
        "change_effect_lambda_va": "0x7EA9250",
        "is_change_effect_trigger_va": "0x7E6ABD0",
        "get_insert_effect_result_difference_va": "0x7ED3B10",
        "single_entry_formula": "diffValue = signed_i32(after - before)",
        "entry_shape": "one DifferencePair<int> plus isConsume per stored entry",
        "not_metric": (
            "current Block, BlockConsumptionSumCount, CurrentTurnTotalBlock, "
            "and listener-local phase count are not used by this predicate"
        ),
    },
    "predicate": {
        "effect_filter": EFFECT_BLOCK,
        "threshold_source": "trigger.phaseValues[0] = 30",
        "comparison_va": "0x7E6AE14..0x7E6AE18",
        "comparison_instruction": "cmp w0, w21; b.gt reject",
        "comparison_operands": "w0=trigger threshold; w21=diffValue",
        "threshold_operator": ">=",
        "signed_i32": True,
        "equality_fires": True,
        "zero_or_negative_fails": True,
        "consumption_route": "GetConsumeEffectTriggerEffectList; not this positive-change trigger",
    },
    "block_snapshot": {
        "calculate_add_block_va": "0x7E5E6E4",
        "restriction_behavior": "IsBlockRestriction(isUse=true) returns actual add 0",
        "cap_behavior": "use the post-cap committed after-before difference",
        "unknown_restriction_or_cap_snapshot": "fail closed",
    },
    "listener": {
        "status_effect_executor_va": "0x7E8FA08",
        "try_add_trigger_effect_status_va": "0x7E8FEE0",
        "trigger_status_ctor_va": "0x7EADF04",
        "spend_count_va": "0x7EAE70C",
        "fields": {
            "turn_count": "0x88",
            "phase_count_dictionary": "0x90",
            "limit_count": "0x98",
            "limit_count_in_turn": "0x9C",
            "limit_count_in_turn_remain": "0xA0",
        },
        "master_installer": "effectCount=0, effectTurn=-1",
        "constructed_limits": "total=-1, per-turn=-1, turn=-1",
        "lifetime": "encounter-persistent; cross-turn listener remains installed",
        "stacking": "type 31 is appended; same status does not merge/replace",
        "play_origins": list(PLAY_ORIGINS),
        "same_transaction": "each DifferencePair entry is dispatched independently",
    },
    "queue_order": [
        "direct card slot 0: ExamCardPlayAggressive",
        "direct card slot 1: ExamEffectTimer (delay 1; owned by TimerBlock leaf)",
        "direct card slot 2: ExamStatusEnchant installer",
        "later Timer->Block child commit",
        "ExamBlock post-commit DifferencePair dispatch",
        "stable active-listener scan",
        "queue status child in effect order",
        "SpendCount (unlimited no-op)",
        "execute queued ExamLessonDependBlock child",
    ],
    "native_order_methods": {
        "get_trigger_effect_list_command_va": "0x7ECF230",
        "get_insert_trigger_effect_list_command_va": "0x7ED0690",
        "spend_count_va": "0x7EAE70C",
    },
}

PC_STATUS_CHANGE_BLOCK30_METADATA: Final = {
    "source": "PC GameAssembly/IL2CPP targeted-metadata-index.json",
    "types": {
        "ExamEffectDifferenceData": {
            "type_index": 3478,
            "token": 33557910,
            "methods": {
                "get_BlockDifference": {"method_index": 17492, "token": 100680789},
                "get_CurrentBlock": {"method_index": 17495, "token": 100680792},
                "get_ExamStatusDifferenceDictionary": {
                    "method_index": 17501,
                    "token": 100680798,
                },
                "get_DifferenceStatusTypeList": {
                    "method_index": 17524,
                    "token": 100680821,
                },
                "SetStatusDifference": {"method_index": 17543, "token": 100680840},
                "SetTriggerStatusDifference": {
                    "method_index": 17545,
                    "token": 100680842,
                },
            },
        },
        "ExamStatusEffectCollection": {
            "type_index": 3790,
            "token": 33558136,
            "methods": {
                "GetPhaseTriggerUpEffectList": {
                    "method_index": 18484,
                    "token": 100681781,
                },
                "GetPhaseEffectList": {"method_index": 18485, "token": 100681782},
                "GetChangeEffectTriggerEffectList": {
                    "method_index": 18497,
                    "token": 100681794,
                },
                "GetConsumeEffectTriggerEffectList": {
                    "method_index": 18498,
                    "token": 100681795,
                },
                "GetTriggerEffectList": {"method_index": 18499, "token": 100681796},
            },
        },
        "TriggerEffectStatusEffect": {
            "type_index": 3854,
            "token": 33558286,
            "methods": {
                "SpendCount": {"method_index": 19075, "token": 100682372},
                "OnTurnChange": {"method_index": 19077, "token": 100682374},
                "GetPhaseCount": {"method_index": 19078, "token": 100682375},
                "IncrementPhaseCount": {"method_index": 19080, "token": 100682377},
                "ClearPhaseCount": {"method_index": 19081, "token": 100682378},
            },
        },
        "ExamParameterModel": {
            "type_index": 3893,
            "token": 33558320,
            "methods": {
                "get_Block": {"method_index": 19481, "token": 100682778},
                "set_Block": {"method_index": 19482, "token": 100682779},
                "get_BlockConsumptionSumCount": {
                    "method_index": 19517,
                    "token": 100682814,
                },
                "get_CurrentTurnTotalBlock": {
                    "method_index": 19580,
                    "token": 100682877,
                },
                "SetBlock": {"method_index": 19620, "token": 100682917},
                "AddBlockConsumptionSumCount": {
                    "method_index": 19640,
                    "token": 100682937,
                },
                "AddCurrentTurnBlock": {
                    "method_index": 19706,
                    "token": 100683003,
                },
            },
        },
        "ExamSequence": {
            "type_index": 3932,
            "token": 33558337,
            "methods": {
                "GetTriggerEffectListCommand": {
                    "method_index": 19963,
                    "token": 100683260,
                },
                "GetInsertTriggerEffectListCommand": {
                    "method_index": 19966,
                    "token": 100683263,
                },
                "GetInsertEffectResultTriggerCommand_difference": {
                    "method_index": 19969,
                    "token": 100683266,
                },
            },
        },
    },
}


__all__ = [
    "ACCOUNTING",
    "ANDROID_STATUS_CHANGE_BLOCK30_EVIDENCE",
    "EFFECT_BLOCK",
    "FORMAL_AFFECTED_VERSION_REFS",
    "FORMAL_CO_BLOCKED_VERSION_REFS",
    "FORMAL_DIRECT_VERSION_REFS",
    "INSTALL_PHASE",
    "PC_STATUS_CHANGE_BLOCK30_METADATA",
    "PERMANENT_TURN",
    "PLAY_ORIGINS",
    "Plan2StatusChangeBlock30CardVersion",
    "Plan2StatusChangeBlock30ChildCommand",
    "Plan2StatusChangeBlock30ContractError",
    "Plan2StatusChangeBlock30Effect",
    "Plan2StatusChangeBlock30Event",
    "Plan2StatusChangeBlock30Evaluation",
    "Plan2StatusChangeBlock30HandoffRow",
    "Plan2StatusChangeBlock30Install",
    "Plan2StatusChangeBlock30Listener",
    "Plan2StatusChangeBlock30Program",
    "Plan2StatusChangeBlock30Scheduler",
    "Plan2StatusChangeBlock30Status",
    "Plan2StatusChangeBlock30Transaction",
    "Plan2StatusChangeBlock30Trigger",
    "TARGET_CARD_ID",
    "TARGET_UPGRADES",
    "TRIGGER_ID",
    "THRESHOLD",
    "advance_plan2_status_change_block30_turn",
    "build_plan2_status_change_block30_handoff",
    "evaluate_plan2_status_change_block30",
    "evaluate_plan2_status_change_block30_transaction",
    "exact_plan2_status_change_block30_rows",
    "install_plan2_status_change_block30_listener",
    "load_plan2_status_change_block30_program",
]
