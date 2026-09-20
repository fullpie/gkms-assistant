"""Standalone exact runtime for Plan2's final ``輝きの到達点`` card.

This module deliberately owns only ``p_card-02-act-100_010`` upgrade 0.  It
closes the direct ``ExamLessonDependBlockConsumptionSum`` executor and the
card-attached Hand move effect ``e_effect-exam_block-0005``.  The card's
``e_trigger-none-remaining_turn-1`` predicate remains an explicit external
handoff, so the local formal accounting is affected=1, direct=0, co=1.

Android v3.2.3 proves two separate execution boundaries:

* the direct executor reads the signed, exam-long block-consumption sum once
  when the child executes, performs binary32 permille multiplication and one
  ceiling, then calls CalculateAddingParameter/AddParameter once; and
* an authoritative move into Hand raises a HandAdd difference callback.  The
  callback queues the card's Master move effect, sets the GUID-local
  once-per-turn flag, and then executes the Block child.  Playing -> Lost is
  not a Hand destination and cannot fire that move effect.

Unknown Master, trigger-handoff, zone, GUID, arithmetic, and transaction
shapes fail closed.  No process, agent, GUI, controller, or central runtime is
called from this leaf.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal, TypeAlias

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    AddBlockSettings,
    AddBlockStatus,
    AddingParameterSettings,
    AddingParameterStatus,
    ParameterApplication,
    ParameterApplicationStatus,
    apply_parameter_add,
    calculate_add_block,
    calculate_adding_parameter,
    ceil_f32_to_i32,
    f32,
    permille_to_f32,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


TARGET_CARD_ID: Final = "p_card-02-act-100_010"
TARGET_UPGRADE: Final = 0
TARGET_VERSION_REF: Final = f"{TARGET_CARD_ID}#{TARGET_UPGRADE}"
TARGET_CARD_NAME: Final = "輝きの到達点"

PLAN2: Final = "ProducePlanType_Plan2"
ACTIVE_SKILL: Final = "ProduceCardCategory_ActiveSkill"
UNKNOWN_COST: Final = "ExamCostType_Unknown"
PLAY_TRIGGER_ID: Final = "e_trigger-none-remaining_turn-1"
PLAY_MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
MOVE_TRIGGER_HAND: Final = "ProduceCardMoveEffectTriggerType_Hand"

DIRECT_EFFECT_ID: Final = (
    "e_effect-exam_lesson_depend_block_consumption_sum-4000-01"
)
DIRECT_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamLessonDependBlockConsumptionSum"
)
DIRECT_VALUE_PERMILLE: Final = 4000
DIRECT_COUNT: Final = 1
DIRECT_EFFECT_GROUPS: Final = (
    "effect_group-visible-exam_lesson_depend_block-000",
    "effect_group-visible-exam_lesson-000",
)

MOVE_EFFECT_ID: Final = "e_effect-exam_block-0005"
MOVE_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamBlock"
MOVE_BLOCK_VALUE: Final = 5
MOVE_EFFECT_GROUPS: Final = ("effect_group-visible-exam_block-000",)

FORMAL_AFFECTED_VERSION_REFS: Final = (TARGET_VERSION_REF,)
FORMAL_DIRECT_VERSION_REFS: Final = ()
FORMAL_CO_BLOCKED_VERSION_REFS: Final = (TARGET_VERSION_REF,)
FORMAL_AFFECTED_COUNT: Final = 1
FORMAL_DIRECT_COUNT: Final = 0
FORMAL_CO_BLOCKED_COUNT: Final = 1

PlayOrigin: TypeAlias = Literal["normal", "forced", "extra"]
StableZone: TypeAlias = Literal["hand", "deck", "grave", "lost", "hold"]
HandMoveReason: TypeAlias = Literal["draw", "replace", "hand-add", "card-move"]

STABLE_ZONES: Final = ("hand", "deck", "grave", "lost", "hold")
HAND_ADD_SOURCES: Final = frozenset(("deck", "grave", "hold"))
HAND_MOVE_REASONS: Final = frozenset(("draw", "replace", "hand-add", "card-move"))


ANDROID_NATIVE_EVIDENCE: Final = {
    "direct_executor": {
        "type": "Campus.InGame.Exam.LessonDependBlockConsumptionSumEffectExecutor",
        "ctor_va": "0x7E82058",
        "execute_va": "0x7E82170",
        "sum_getter_call": "0x7E821D4 -> 0x7EB8AD8",
        "float_from_permille_call": "0x7E821FC -> 0x701E940",
        "multiply": "0x7E82220 FMUL S0,S8,S0",
        "ceiling": "0x7E82224 FRINTP; 0x7E82228 FCVTPS",
        "calculate_lesson_call": "0x7E82258 -> 0x7E5DADC",
        "add_parameter_call": "0x7E82264 -> 0x7E5E4C8",
        "loop": "0x7E8223C..0x7E82274 repeats the already-computed request _count times",
    },
    "accumulator": {
        "field_offset": "0x36C",
        "getter_va": "0x7EB8AD8",
        "setter_va": "0x7EB8B20",
        "set_block_va": "0x7EBB524",
        "add_sum_va": "0x7EBB5C8",
        "set_block_fact": (
            "only isConsumption=true adds max(i32(oldBlock-newBlock),0); "
            "the sum uses wrapping signed Int32 addition"
        ),
    },
    "block_child": {
        "type": "Campus.InGame.Exam.BlockEffectExecutor",
        "execute_va": "0x7E728C4",
        "calculate_add_block_call": "0x7E7293C -> 0x7E5E6CC",
        "add_block_fix_call": "0x7E72948 -> 0x7E5EA94",
        "fact": (
            "value1=5 is buff-active, rate=1.0; AddBlockFix uses "
            "SetBlock(isConsumption=false), so the cumulative sum is unchanged"
        ),
    },
    "hand_move": {
        "insert_move_effect_va": "0x7ED8358",
        "insert_hand_add_va": "0x7ED7FF8",
        "isil_source": (
            "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
            "Assembly-CSharp/Campus/InGame/Exam/ExamSequence.txt"
        ),
        "isil_lines": [26922, 27529, 27977],
        "fact": (
            "post-commit HandAdd dispatch checks the per-card used flag, queues "
            "Master move effects in order, and sets the flag only after a "
            "positive insertion and before child execution"
        ),
    },
}


PC_NATIVE_METADATA: Final = {
    "LessonDependBlockConsumptionSumEffectExecutor": {
        "type_index": 3598,
        "fields": ["_value", "_count"],
        "ctor": {"method_index": 17964, "token": "0x0600462D"},
        "ExecuteEffect": {"method_index": 17965, "token": "0x0600462E"},
    },
    "BlockEffectExecutor": {
        "type_index": 3517,
        "fields": ["_value"],
        "ctor": {"method_index": 17786, "token": "0x0600457B"},
        "ExecuteEffect": {"method_index": 17787, "token": "0x0600457C"},
    },
    "ExamEffectUtility": {
        "CalculateAddingParameter4": {
            "method_index": 17583,
            "token": "0x060044B0",
        },
        "AddParameter": {"method_index": 17584, "token": "0x060044B1"},
    },
    "ExamParameterModel": {
        "get_BlockConsumptionSumCount": {
            "method_index": 19517,
            "token": "0x06004C3E",
        },
        "set_BlockConsumptionSumCount": {
            "method_index": 19518,
            "token": "0x06004C3F",
        },
        "SetBlock": {"method_index": 19620, "token": "0x06004CA5"},
        "AddBlockConsumptionSumCount": {
            "method_index": 19640,
            "token": "0x06004CB9",
        },
    },
    "ExamSequence": {
        "InsertMoveEffect": {"method_index": 19970, "token": "0x06004E03"},
        "InsertMoveEffectOnHandAdd": {
            "method_index": 19972,
            "token": "0x06004E05",
        },
    },
}


class Plan2FinalCardError(ValueError):
    """A supplied row, handoff, state, or event is outside this exact leaf."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _plain_i32(value: object, label: str) -> int:
    if type(value) is not int or not INT32_MIN <= value <= INT32_MAX:
        raise Plan2FinalCardError("invalid-int32", label)
    return value


def _counter_increment(value: object, label: str) -> int:
    current = _plain_i32(value, label)
    if current < 0 or current == INT32_MAX:
        raise Plan2FinalCardError("counter-overflow-or-negative", label)
    return current + 1


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _json_object(raw: object, label: str) -> dict[str, object]:
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2FinalCardError("master-json", label) from error
    if not isinstance(value, dict):
        raise Plan2FinalCardError("master-json-object", label)
    return value


def _json_array(raw: object, label: str) -> list[object]:
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2FinalCardError("master-json", label) from error
    if not isinstance(value, list):
        raise Plan2FinalCardError("master-json-array", label)
    return value


def _exact(actual: object, expected: object, label: str) -> None:
    if type(actual) is not type(expected) or actual != expected:
        raise Plan2FinalCardError(
            "master-shape", f"{label}={actual!r};expected={expected!r}"
        )


@dataclass(frozen=True, slots=True)
class ExactEffectContract:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    effect_group_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FinalCardContract:
    card_id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    stamina: int
    force_stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    play_destination: str
    play_slots: tuple[tuple[str, str, bool, bool], ...]
    move_trigger_type: str
    move_effect_ids: tuple[str, ...]
    move_trigger_ids: tuple[str, ...]
    direct_effect: ExactEffectContract
    move_effect: ExactEffectContract

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"


_EFFECT_RAW_KEYS: Final = {
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


def _validate_effect_row(
    row: sqlite3.Row,
    *,
    effect_id: str,
    effect_type: str,
    value1: int,
    count: int,
    groups: tuple[str, ...],
    description_counts: tuple[int, int],
) -> ExactEffectContract:
    normalized = {
        "id": effect_id,
        "effect_type": effect_type,
        "value1": value1,
        "value2": 0,
        "effect_count": count,
        "effect_turn": 0,
        "status_enchant_id": "",
        "chain_effect_id": "",
    }
    for key, expected in normalized.items():
        _exact(row[key], expected, f"{effect_id}.{key}")
    raw = _json_object(row["raw_json"], effect_id)
    _exact(set(raw), _EFFECT_RAW_KEYS, f"{effect_id}.keys")
    expected_raw = {
        "id": effect_id,
        "effectType": effect_type,
        "effectValue1": value1,
        "effectValue2": 0,
        "effectCount": count,
        "effectTurn": 0,
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": "ProduceExamEffectType_Unknown",
        "produceCardSearchId": "",
        "movePositionType": "ProduceCardMovePositionType_Unknown",
        "pickRangeType": "ProducePickRangeType_Unknown",
        "pickCountReferenceProduceCardSearchId": "",
        "pickCountType": "ProducePickCountType_Unknown",
        "pickCountMin": 0,
        "pickCountMax": 0,
        "produceCardSearchId2": "",
        "pickRangeType2": "ProducePickRangeType_Unknown",
        "pickCountReferenceProduceCardSearchId2": "",
        "pickCountType2": "ProducePickCountType_Unknown",
        "pickCountMin2": 0,
        "pickCountMax2": 0,
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": "",
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
        "effectGroupIds": list(groups),
    }
    for key, expected in expected_raw.items():
        _exact(raw[key], expected, f"{effect_id}.raw.{key}")
    for key, expected_count in zip(
        ("produceDescriptions", "customizeProduceDescriptions"),
        description_counts,
        strict=True,
    ):
        value = raw[key]
        if not isinstance(value, list) or len(value) != expected_count:
            raise Plan2FinalCardError("master-description-shape", f"{effect_id}.{key}")
    return ExactEffectContract(effect_id, effect_type, value1, 0, count, 0, groups)


def load_plan2_final_card_contract(
    database: Path = DEFAULT_DATABASE,
) -> FinalCardContract:
    """Load and strictly validate the only admitted Master card/effect rows."""

    database = Path(database).resolve()
    try:
        with closing(
            sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            card = connection.execute(
                "SELECT * FROM card WHERE id=? AND upgrade_count=?",
                (TARGET_CARD_ID, TARGET_UPGRADE),
            ).fetchone()
            direct = connection.execute(
                "SELECT * FROM effect WHERE id=?", (DIRECT_EFFECT_ID,)
            ).fetchone()
            move = connection.execute(
                "SELECT * FROM effect WHERE id=?", (MOVE_EFFECT_ID,)
            ).fetchone()
    except sqlite3.Error as error:
        raise Plan2FinalCardError("master-read", TARGET_VERSION_REF) from error
    if card is None or direct is None or move is None:
        raise Plan2FinalCardError("master-slice-incomplete", TARGET_VERSION_REF)

    normalized = {
        "id": TARGET_CARD_ID,
        "upgrade_count": TARGET_UPGRADE,
        "name": TARGET_CARD_NAME,
        "plan_type": PLAN2,
        "category": ACTIVE_SKILL,
        "stamina": 1,
        "cost_type": UNKNOWN_COST,
        "cost_value": 0,
        "play_trigger_id": PLAY_TRIGGER_ID,
        "move_position_type": PLAY_MOVE_LOST,
    }
    for key, expected in normalized.items():
        _exact(card[key], expected, f"{TARGET_VERSION_REF}.{key}")

    expected_slot = {
        "produceExamTriggerId": "",
        "produceExamEffectId": DIRECT_EFFECT_ID,
        "hideIcon": False,
        "isOncePlayEffect": False,
    }
    play_slots = _json_array(card["play_effects_json"], "card.play_effects_json")
    _exact(play_slots, [expected_slot], "card.play_effects_json")

    raw = _json_object(card["raw_json"], TARGET_VERSION_REF)
    expected_card_raw = {
        "id": TARGET_CARD_ID,
        "upgradeCount": 0,
        "name": TARGET_CARD_NAME,
        "assetId": "img_general_skillcard_act-100_010",
        "isCharacterAsset": True,
        "voiceAssetId": "",
        "rarity": "ProduceCardRarity_Legend",
        "planType": PLAN2,
        "category": ACTIVE_SKILL,
        "stamina": 1,
        "forceStamina": 0,
        "costType": UNKNOWN_COST,
        "costValue": 0,
        "playProduceExamTriggerId": PLAY_TRIGGER_ID,
        "playEffects": [expected_slot],
        "playMovePositionType": PLAY_MOVE_LOST,
        "moveEffectTriggerType": MOVE_TRIGGER_HAND,
        "moveProduceExamEffectIds": [MOVE_EFFECT_ID],
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
        "effectGroupIds": [*DIRECT_EFFECT_GROUPS, *MOVE_EFFECT_GROUPS],
        "produceCardCustomizeIds": [],
        "maxCustomizeCount": 0,
        "isConversion": False,
        "moveProduceExamTriggerIds": [],
        "originCharacterId": "",
        "originPrimaStellaIdolCardId": "",
        "viewStartTime": "0",
        "isLimited": False,
        "order": "91020000780010",
    }
    if set(raw) != {*expected_card_raw, "produceDescriptions"}:
        raise Plan2FinalCardError("master-shape", "card.raw.keys")
    for key, expected in expected_card_raw.items():
        _exact(raw[key], expected, f"card.raw.{key}")
    descriptions = raw["produceDescriptions"]
    if not isinstance(descriptions, list) or len(descriptions) != 28:
        raise Plan2FinalCardError("master-description-shape", "card.produceDescriptions")

    direct_contract = _validate_effect_row(
        direct,
        effect_id=DIRECT_EFFECT_ID,
        effect_type=DIRECT_EFFECT_TYPE,
        value1=DIRECT_VALUE_PERMILLE,
        count=DIRECT_COUNT,
        groups=DIRECT_EFFECT_GROUPS,
        description_counts=(8, 9),
    )
    move_contract = _validate_effect_row(
        move,
        effect_id=MOVE_EFFECT_ID,
        effect_type=MOVE_EFFECT_TYPE,
        value1=MOVE_BLOCK_VALUE,
        count=0,
        groups=MOVE_EFFECT_GROUPS,
        description_counts=(4, 5),
    )
    return FinalCardContract(
        TARGET_CARD_ID,
        0,
        TARGET_CARD_NAME,
        PLAN2,
        ACTIVE_SKILL,
        1,
        0,
        UNKNOWN_COST,
        0,
        PLAY_TRIGGER_ID,
        PLAY_MOVE_LOST,
        (("", DIRECT_EFFECT_ID, False, False),),
        MOVE_TRIGGER_HAND,
        (MOVE_EFFECT_ID,),
        (),
        direct_contract,
        move_contract,
    )


load_final_card_contract = load_plan2_final_card_contract


@dataclass(frozen=True, slots=True)
class RemainingTurnOneHandoff:
    """External trigger decision consumed here without reimplementing it."""

    trigger_id: str
    card_id: str
    upgrade: int
    guid: str
    play_origin: PlayOrigin
    remaining_turns: int
    admitted: bool
    phase_type: str = "ProduceExamPhaseType_None"


def validate_remaining_turn_one_handoff(
    handoff: RemainingTurnOneHandoff,
    *,
    guid: str,
    play_origin: PlayOrigin,
) -> None:
    """Validate identity/admission only; trigger evaluation belongs elsewhere."""

    if not isinstance(handoff, RemainingTurnOneHandoff):
        raise Plan2FinalCardError("remaining-turn-handoff-missing-or-unknown")
    expected = (
        (handoff.trigger_id, PLAY_TRIGGER_ID, "trigger-id"),
        (handoff.card_id, TARGET_CARD_ID, "card-id"),
        (handoff.upgrade, TARGET_UPGRADE, "upgrade"),
        (handoff.guid, guid, "guid"),
        (handoff.play_origin, play_origin, "play-origin"),
        (handoff.remaining_turns, 1, "remaining-turns"),
        (handoff.phase_type, "ProduceExamPhaseType_None", "phase-type"),
        (handoff.admitted, True, "admission"),
    )
    for actual, wanted, label in expected:
        if type(actual) is not type(wanted) or actual != wanted:
            raise Plan2FinalCardError("remaining-turn-handoff-drift", label)


@dataclass(frozen=True, slots=True)
class FinalCardPlayHistory:
    transaction_id: str
    guid: str
    play_origin: PlayOrigin
    source_zone: StableZone
    destination_zone: Literal["lost"]
    stamina_before: int
    stamina_after: int
    cost_paid: int
    card_play_count_before: int
    card_play_count_after: int
    global_play_count_before: int
    global_play_count_after: int
    turn_play_count_before: int
    turn_play_count_after: int
    block_consumption_sum_snapshot: int
    requested_lesson: int
    calculated_lesson: int
    actual_lesson: int
    phases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FinalCardMoveHistory:
    guid: str
    source_zone: StableZone | Literal["playing"]
    destination_zone: StableZone
    reason: str
    same_guid: bool
    move_effect_triggered: bool
    move_effect_executed: bool


@dataclass(frozen=True, slots=True)
class FinalCardRuntime:
    """Leaf-local scalars plus the shared immutable five-zone GUID state."""

    native_state: Plan3NativeState
    stamina: int = 0
    block: int = 0
    block_consumption_sum_count: int = 0
    adding_status: AddingParameterStatus = AddingParameterStatus()
    adding_settings: AddingParameterSettings = AddingParameterSettings()
    application_status: ParameterApplicationStatus = ParameterApplicationStatus(
        judge_parameter=0
    )
    add_block_status: AddBlockStatus = AddBlockStatus()
    add_block_settings: AddBlockSettings = AddBlockSettings()
    global_play_count: int = 0
    turn_play_count: int = 0
    committed_transactions: tuple[str, ...] = ()
    play_history: tuple[FinalCardPlayHistory, ...] = ()
    move_history: tuple[FinalCardMoveHistory, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.native_state, Plan3NativeState):
            raise TypeError("native_state must be Plan3NativeState")
        for label in ("stamina", "block", "block_consumption_sum_count"):
            _plain_i32(getattr(self, label), label)
        if self.stamina < 0:
            raise Plan2FinalCardError("invalid-stamina")
        for label in ("global_play_count", "turn_play_count"):
            value = _plain_i32(getattr(self, label), label)
            if value < 0:
                raise Plan2FinalCardError("invalid-counter", label)
        if not isinstance(self.adding_status, AddingParameterStatus):
            raise TypeError("adding_status must be AddingParameterStatus")
        if not isinstance(self.adding_settings, AddingParameterSettings):
            raise TypeError("adding_settings must be AddingParameterSettings")
        if not isinstance(self.application_status, ParameterApplicationStatus):
            raise TypeError("application_status must be ParameterApplicationStatus")
        if not isinstance(self.add_block_status, AddBlockStatus):
            raise TypeError("add_block_status must be AddBlockStatus")
        if not isinstance(self.add_block_settings, AddBlockSettings):
            raise TypeError("add_block_settings must be AddBlockSettings")
        transactions = tuple(self.committed_transactions)
        if any(type(value) is not str or not value for value in transactions):
            raise Plan2FinalCardError("invalid-transaction-history")
        if len(set(transactions)) != len(transactions):
            raise Plan2FinalCardError("duplicate-transaction-history")
        object.__setattr__(self, "committed_transactions", transactions)
        histories = tuple(self.play_history)
        moves = tuple(self.move_history)
        if any(not isinstance(value, FinalCardPlayHistory) for value in histories):
            raise Plan2FinalCardError("invalid-play-history")
        if any(not isinstance(value, FinalCardMoveHistory) for value in moves):
            raise Plan2FinalCardError("invalid-move-history")
        object.__setattr__(self, "play_history", histories)
        object.__setattr__(self, "move_history", moves)

    @property
    def lesson_parameter(self) -> int:
        return self.application_status.judge_parameter


@dataclass(frozen=True, slots=True)
class BlockConsumptionTransition:
    before: FinalCardRuntime
    after: FinalCardRuntime
    requested_consumption: int
    actual_consumption: int
    block_before: int
    block_after: int
    sum_before: int
    sum_after: int
    is_consumption: bool


def set_final_card_block(
    runtime: FinalCardRuntime,
    block_after: int,
    *,
    is_consumption: bool,
    requested_consumption: int = 0,
) -> BlockConsumptionTransition:
    """Project native SetBlock and its signed exam-long consumption counter."""

    if not isinstance(runtime, FinalCardRuntime):
        raise TypeError("runtime must be FinalCardRuntime")
    block_after = _plain_i32(block_after, "block_after")
    requested_consumption = _plain_i32(
        requested_consumption, "requested_consumption"
    )
    if type(is_consumption) is not bool:
        raise TypeError("is_consumption must be bool")
    if requested_consumption < 0:
        raise Plan2FinalCardError("negative-requested-consumption")
    actual = max(_i32(runtime.block - block_after), 0) if is_consumption else 0
    if is_consumption and actual > requested_consumption:
        raise Plan2FinalCardError(
            "actual-consumption-exceeds-request",
            f"actual={actual}:requested={requested_consumption}",
        )
    sum_after = (
        _i32(runtime.block_consumption_sum_count + actual)
        if is_consumption
        else runtime.block_consumption_sum_count
    )
    after = replace(
        runtime,
        block=block_after,
        block_consumption_sum_count=sum_after,
    )
    return BlockConsumptionTransition(
        runtime,
        after,
        requested_consumption,
        actual,
        runtime.block,
        block_after,
        runtime.block_consumption_sum_count,
        sum_after,
        is_consumption,
    )


def consume_final_card_block(
    runtime: FinalCardRuntime, requested_consumption: int
) -> BlockConsumptionTransition:
    """Consume up to the current non-negative Block and record actual loss."""

    requested = _plain_i32(requested_consumption, "requested_consumption")
    if requested < 0 or runtime.block < 0:
        raise Plan2FinalCardError("unsupported-consumption-domain")
    actual = min(runtime.block, requested)
    return set_final_card_block(
        runtime,
        runtime.block - actual,
        is_consumption=True,
        requested_consumption=requested,
    )


@dataclass(frozen=True, slots=True)
class LessonDependTransition:
    before: FinalCardRuntime
    after: FinalCardRuntime
    effect: ExactEffectContract
    block_consumption_sum_snapshot: int
    ratio_float32: float
    requested_lesson: int
    calculated_lesson: int
    application: ParameterApplication
    trace: tuple[str, ...]

    @property
    def actual_lesson(self) -> int:
        return self.application.actual_parameter


def _updated_application_status(
    status: ParameterApplicationStatus, application: ParameterApplication
) -> ParameterApplicationStatus:
    return replace(
        status,
        judge_parameter=application.after,
        current_turn_total_add_parameter=(
            application.current_turn_total_add_parameter
        ),
        judge_parameter_vocal=application.judge_parameter_vocal,
        judge_parameter_dance=application.judge_parameter_dance,
        judge_parameter_visual=application.judge_parameter_visual,
    )


def execute_lesson_depend_block_consumption_sum(
    runtime: FinalCardRuntime,
    effect: ExactEffectContract,
) -> LessonDependTransition:
    """Execute the exact direct child with one execution-time sum snapshot."""

    if not isinstance(runtime, FinalCardRuntime):
        raise TypeError("runtime must be FinalCardRuntime")
    if not isinstance(effect, ExactEffectContract) or effect != ExactEffectContract(
        DIRECT_EFFECT_ID,
        DIRECT_EFFECT_TYPE,
        DIRECT_VALUE_PERMILLE,
        0,
        DIRECT_COUNT,
        0,
        DIRECT_EFFECT_GROUPS,
    ):
        raise Plan2FinalCardError("unknown-direct-effect-shape")
    snapshot = runtime.block_consumption_sum_count
    ratio = permille_to_f32(effect.value1)
    requested = ceil_f32_to_i32(f32(ratio * f32(snapshot)))
    calculated = calculate_adding_parameter(
        requested,
        is_buff_active=True,
        status=runtime.adding_status,
        settings=runtime.adding_settings,
        additional=None,
    )
    application = apply_parameter_add(calculated, status=runtime.application_status)
    after = replace(
        runtime,
        application_status=_updated_application_status(
            runtime.application_status, application
        ),
    )
    return LessonDependTransition(
        runtime,
        after,
        effect,
        snapshot,
        ratio,
        requested,
        calculated,
        application,
        (
            "direct:read-BlockConsumptionSumCount-once-at-child-execution",
            "direct:FloatFromPermil(value1=4000)-binary32",
            "direct:FMUL-binary32(sum-snapshot)",
            "direct:FRINTP-FCVTPS-ceil-to-Int32",
            "direct:CalculateAddingParameter(isBuffActive=true,additional=null)",
            "direct:AddParameter-once",
        ),
    )


@dataclass(frozen=True, slots=True)
class BlockFiveTransition:
    before: FinalCardRuntime
    after: FinalCardRuntime
    effect: ExactEffectContract
    requested_block: int
    calculated_block: int
    fixed_block_delta: int
    block_before: int
    block_after: int
    sum_before: int
    sum_after: int
    trace: tuple[str, ...]


def execute_move_block_five(
    runtime: FinalCardRuntime,
    effect: ExactEffectContract,
) -> BlockFiveTransition:
    """Execute exact ``ExamBlock`` +5 without consumption-counter mutation."""

    if not isinstance(runtime, FinalCardRuntime):
        raise TypeError("runtime must be FinalCardRuntime")
    if not isinstance(effect, ExactEffectContract) or effect != ExactEffectContract(
        MOVE_EFFECT_ID,
        MOVE_EFFECT_TYPE,
        MOVE_BLOCK_VALUE,
        0,
        0,
        0,
        MOVE_EFFECT_GROUPS,
    ):
        raise Plan2FinalCardError("unknown-move-effect-shape")
    calculated = calculate_add_block(
        effect.value1,
        is_buff_active=True,
        status=runtime.add_block_status,
        settings=runtime.add_block_settings,
        additional_multiple_aggressive_rate=1.0,
    )
    fixed_delta = max(_i32(-runtime.block), calculated)
    block_after = _i32(runtime.block + fixed_delta)
    after = replace(runtime, block=block_after)
    return BlockFiveTransition(
        runtime,
        after,
        effect,
        effect.value1,
        calculated,
        fixed_delta,
        runtime.block,
        block_after,
        runtime.block_consumption_sum_count,
        after.block_consumption_sum_count,
        (
            "move-child:CalculateAddBlock(value=5,isBuffActive=true,rate=1.0)",
            "move-child:AddBlockFix",
            "move-child:SetBlock(isConsumption=false)",
            "move-child:append-block-difference-once",
            "move-child:append-effect-difference(statusEffectType=3)-once",
        ),
    )


def _target_card(state: Plan3NativeState, guid: str) -> tuple[StableZone, Plan3NativeCard]:
    if type(guid) is not str or not guid:
        raise Plan2FinalCardError("invalid-guid")
    found = tuple(
        (zone, card)
        for zone in STABLE_ZONES
        for card in getattr(state, zone)
        if card.guid == guid
    )
    if len(found) != 1:
        raise Plan2FinalCardError("guid-zone-count", f"{guid}:{len(found)}")
    zone, card = found[0]
    if (
        card.card_id != TARGET_CARD_ID
        or card.base_upgrade != TARGET_UPGRADE
        or card.temporary_upgrade != 0
        or card.effective_upgrade != TARGET_UPGRADE
        or card.support_upgrade_ids
        or card.runtime_grow_effect_ids
        or card.runtime_customize_ids
        or card.runtime_grow_status is not None
        or card.runtime_lesson_add
        or card.runtime_full_power_point_add
        or card.runtime_full_power_point_cost_add
        or card.runtime_customize_lesson_add
        or card.runtime_lesson_count_add
        or card.runtime_block_add
        or card.runtime_cost_reduce
        or card.runtime_cost_add
        or card.runtime_cost_penetrate_reduce
        or card.runtime_cost_penetrate_add
    ):
        raise Plan2FinalCardError("unsupported-card-instance-shape", guid)
    return zone, card


def _relocate(
    state: Plan3NativeState,
    guid: str,
    source: StableZone,
    destination: StableZone,
    *,
    replacement_card: Plan3NativeCard | None = None,
) -> Plan3NativeState:
    card = next((value for value in getattr(state, source) if value.guid == guid), None)
    if card is None or any(value.guid == guid for value in getattr(state, destination)):
        raise Plan2FinalCardError("move-guid-zone-drift", guid)
    moved = card if replacement_card is None else replacement_card
    if moved.guid != guid:
        raise Plan2FinalCardError("move-guid-identity-drift", guid)
    changes = {
        source: tuple(value for value in getattr(state, source) if value.guid != guid),
        destination: (*getattr(state, destination), moved),
    }
    return replace(state, **changes)


@dataclass(frozen=True, slots=True)
class HandMoveTransition:
    before: FinalCardRuntime
    after: FinalCardRuntime
    contract: FinalCardContract
    guid: str
    source_zone: StableZone
    destination_zone: Literal["hand"]
    reason: HandMoveReason
    authoritative_commit: bool
    move_effect_triggered: bool
    move_effect_executed: bool
    child: BlockFiveTransition | None
    trace: tuple[str, ...]


def move_final_card_to_hand(
    runtime: FinalCardRuntime,
    guid: str,
    *,
    source_zone: StableZone,
    reason: HandMoveReason,
    database: Path = DEFAULT_DATABASE,
) -> HandMoveTransition:
    """Commit one proven Hand arrival and dispatch the attached Block +5."""

    if not isinstance(runtime, FinalCardRuntime):
        raise TypeError("runtime must be FinalCardRuntime")
    if source_zone not in HAND_ADD_SOURCES:
        raise Plan2FinalCardError("unknown-hand-add-source", str(source_zone))
    if reason not in HAND_MOVE_REASONS:
        raise Plan2FinalCardError("unknown-hand-move-reason", str(reason))
    actual_source, card = _target_card(runtime.native_state, guid)
    if actual_source != source_zone:
        raise Plan2FinalCardError(
            "declared-source-zone-drift", f"{source_zone}!={actual_source}"
        )
    contract = load_plan2_final_card_contract(database)
    committed_state = _relocate(runtime.native_state, guid, source_zone, "hand")
    committed = replace(runtime, native_state=committed_state)
    base_trace = (
        "authoritative-zone-commit:same-guid",
        f"HandAdd-difference:{source_zone}->hand:{reason}",
    )
    if card.move_effect_used_in_turn:
        history = FinalCardMoveHistory(
            guid, source_zone, "hand", reason, True, True, False
        )
        after = replace(committed, move_history=(*committed.move_history, history))
        return HandMoveTransition(
            runtime,
            after,
            contract,
            guid,
            source_zone,
            "hand",
            reason,
            True,
            True,
            False,
            None,
            (*base_trace, "move-effect:skip-used-in-turn"),
        )

    marked = replace(card, move_effect_used_in_turn=True)
    marked_state = replace(
        committed.native_state,
        hand=tuple(marked if value.guid == guid else value for value in committed.native_state.hand),
    )
    marked_runtime = replace(committed, native_state=marked_state)
    child = execute_move_block_five(marked_runtime, contract.move_effect)
    history = FinalCardMoveHistory(
        guid, source_zone, "hand", reason, True, True, True
    )
    after = replace(child.after, move_history=(*child.after.move_history, history))
    trace = (
        *base_trace,
        f"move-effect:queue:{MOVE_EFFECT_ID}",
        "move-effect:set-GUID-used-in-turn-before-child",
        *child.trace,
        "move-effect:complete-exactly-once",
    )
    return HandMoveTransition(
        runtime,
        after,
        contract,
        guid,
        source_zone,
        "hand",
        reason,
        True,
        True,
        True,
        child,
        trace,
    )


@dataclass(frozen=True, slots=True)
class FinalCardPlayRequest:
    transaction_id: str
    guid: str
    play_origin: PlayOrigin
    source_zone: StableZone
    remaining_turn_handoff: RemainingTurnOneHandoff


@dataclass(frozen=True, slots=True)
class FinalCardPlayTransition:
    before: FinalCardRuntime
    after: FinalCardRuntime
    request: FinalCardPlayRequest
    contract: FinalCardContract
    direct: LessonDependTransition
    history: FinalCardPlayHistory
    final_move: FinalCardMoveHistory
    trace: tuple[str, ...]


def _play_source_is_valid(origin: PlayOrigin, source: StableZone) -> bool:
    if origin in {"normal", "extra"}:
        return source == "hand"
    if origin == "forced":
        return source in {"hand", "lost"}
    return False


def play_plan2_final_card(
    runtime: FinalCardRuntime,
    request: FinalCardPlayRequest,
    *,
    database: Path = DEFAULT_DATABASE,
) -> FinalCardPlayTransition:
    """Execute one admitted normal/forced/extra transaction atomically."""

    if not isinstance(runtime, FinalCardRuntime):
        raise TypeError("runtime must be FinalCardRuntime")
    if not isinstance(request, FinalCardPlayRequest):
        raise TypeError("request must be FinalCardPlayRequest")
    if type(request.transaction_id) is not str or not request.transaction_id:
        raise Plan2FinalCardError("invalid-transaction-id")
    if request.transaction_id in runtime.committed_transactions:
        raise Plan2FinalCardError("transaction-already-committed", request.transaction_id)
    if request.play_origin not in {"normal", "forced", "extra"}:
        raise Plan2FinalCardError("unknown-play-origin", str(request.play_origin))
    if request.source_zone not in STABLE_ZONES or not _play_source_is_valid(
        request.play_origin, request.source_zone
    ):
        raise Plan2FinalCardError(
            "play-origin-source-shape", f"{request.play_origin}:{request.source_zone}"
        )
    source_zone, card = _target_card(runtime.native_state, request.guid)
    if source_zone != request.source_zone:
        raise Plan2FinalCardError("declared-source-zone-drift", request.guid)
    validate_remaining_turn_one_handoff(
        request.remaining_turn_handoff,
        guid=request.guid,
        play_origin=request.play_origin,
    )
    contract = load_plan2_final_card_contract(database)

    cost_paid = contract.stamina if request.play_origin == "normal" else 0
    if runtime.stamina < cost_paid:
        raise Plan2FinalCardError("insufficient-stamina")
    card_play_after = _counter_increment(card.play_count, "card-play-count")
    global_after = _counter_increment(runtime.global_play_count, "global-play-count")
    turn_after = _counter_increment(runtime.turn_play_count, "turn-play-count")

    paid = replace(runtime, stamina=runtime.stamina - cost_paid)
    direct = execute_lesson_depend_block_consumption_sum(
        paid, contract.direct_effect
    )
    played_card = replace(card, play_count=card_play_after)
    settled_state = _relocate(
        direct.after.native_state,
        request.guid,
        request.source_zone,
        "lost",
        replacement_card=played_card,
    ) if request.source_zone != "lost" else replace(
        direct.after.native_state,
        lost=tuple(
            played_card if value.guid == request.guid else value
            for value in direct.after.native_state.lost
        ),
    )

    final_move = FinalCardMoveHistory(
        request.guid,
        "playing",
        "lost",
        f"{request.play_origin}-play-settlement",
        True,
        False,
        False,
    )
    phases = (
        "validate-external-RemainingTurn-1-handoff",
        "bind-same-GUID-to-Playing",
        "normal:pay-stamina-1" if request.play_origin == "normal" else f"{request.play_origin}:cost-free-UsePool",
        "direct-slot-0:begin-after-Playing",
        *direct.trace,
        "direct-slot-0:end-before-final-move",
        "Playing->Lost:same-GUID:once",
        "Hand-move-effect:not-fired-destination-Lost",
        "increment-card-global-turn-counts:once",
        "append-play-and-move-history:once",
    )
    history = FinalCardPlayHistory(
        request.transaction_id,
        request.guid,
        request.play_origin,
        request.source_zone,
        "lost",
        runtime.stamina,
        direct.after.stamina,
        cost_paid,
        card.play_count,
        card_play_after,
        runtime.global_play_count,
        global_after,
        runtime.turn_play_count,
        turn_after,
        direct.block_consumption_sum_snapshot,
        direct.requested_lesson,
        direct.calculated_lesson,
        direct.actual_lesson,
        phases,
    )
    after = replace(
        direct.after,
        native_state=settled_state,
        global_play_count=global_after,
        turn_play_count=turn_after,
        committed_transactions=(
            *direct.after.committed_transactions,
            request.transaction_id,
        ),
        play_history=(*direct.after.play_history, history),
        move_history=(*direct.after.move_history, final_move),
    )
    return FinalCardPlayTransition(
        runtime, after, request, contract, direct, history, final_move, phases
    )


def start_final_card_turn(runtime: FinalCardRuntime) -> FinalCardRuntime:
    """Reset turn-local totals/flags while retaining the exam-long sum."""

    if not isinstance(runtime, FinalCardRuntime):
        raise TypeError("runtime must be FinalCardRuntime")
    application = replace(
        runtime.application_status, current_turn_total_add_parameter=0
    )
    return replace(
        runtime,
        native_state=runtime.native_state.reset_turn_used_supports(),
        application_status=application,
        turn_play_count=0,
    )


def build_plan2_final_card_native_audit() -> dict[str, object]:
    """Return the checked-in standalone audit payload."""

    return {
        "schemaVersion": 1,
        "auditId": "plan2-lesson-depend-block-consumption-final-native-audit",
        "scope": {
            "cardVersions": [TARGET_VERSION_REF],
            "standaloneOnly": True,
            "centralRuntimeModified": False,
            "nativeSearchModified": False,
            "formalCoverageIntegrated": False,
            "plan3Modified": False,
            "guiControllerModified": False,
        },
        "accounting": {
            "affected": FORMAL_AFFECTED_COUNT,
            "direct": FORMAL_DIRECT_COUNT,
            "co": FORMAL_CO_BLOCKED_COUNT,
            "coBlocker": PLAY_TRIGGER_ID,
            "handoffOnly": True,
        },
        "master": {
            "cardId": TARGET_CARD_ID,
            "upgrade": 0,
            "name": TARGET_CARD_NAME,
            "plan": PLAN2,
            "category": ACTIVE_SKILL,
            "cost": {"stamina": 1, "costType": UNKNOWN_COST, "costValue": 0},
            "playTriggerId": PLAY_TRIGGER_ID,
            "playDestination": PLAY_MOVE_LOST,
            "orderedPlaySlots": [DIRECT_EFFECT_ID],
            "moveTrigger": MOVE_TRIGGER_HAND,
            "orderedMoveEffects": [MOVE_EFFECT_ID],
            "moveTriggerIds": [],
        },
        "androidNativeEvidence": ANDROID_NATIVE_EVIDENCE,
        "pcNativeMetadata": PC_NATIVE_METADATA,
        "runtimeContract": {
            "accumulator": (
                "signed Int32 exam-long BlockConsumptionSumCount; no turn reset; "
                "only actual positive Block loss from consumption is accumulated"
            ),
            "directFormula": (
                "snapshot=sum; ratio=FloatFromPermil(4000); "
                "requested=ceil_f32(f32(ratio*f32(snapshot))); "
                "CalculateAddingParameter(requested,true,context,null); AddParameter once"
            ),
            "directOrder": (
                "external trigger handoff -> same GUID Playing -> cost policy -> "
                "direct child -> same GUID Playing-to-Lost -> counts/history"
            ),
            "lessonModifiers": (
                "shared native restriction, additive, multiplier, binary32 and cap path; "
                "requested/calculated/actual values remain distinct"
            ),
            "moveEffect": (
                "destination Hand only after authoritative deck/grave/hold-to-Hand commit; "
                "card-attached lifetime, no listener installation; at most once per GUID "
                "per turn; flag set before Block5 execution"
            ),
            "blockChild": (
                "CalculateAddBlock(5,true,rate=1.0) -> AddBlockFix -> "
                "SetBlock(isConsumption=false); sum unchanged"
            ),
            "playModes": {
                "normal": "Hand source, pay stamina 1",
                "forced": "Hand or Lost source, no cost/playable consumption",
                "extra": "Hand source, no cost/playable consumption",
            },
            "settlement": (
                "normal/forced/extra each append one card/global/turn count, one play "
                "history and one Playing-to-Lost move for the same GUID; destination Lost "
                "does not fire the Hand move effect"
            ),
            "unknownShapes": "fail-closed without partial mutation",
        },
        "verification": {
            "focusedOnly": True,
            "fullSuite": "not-performed",
            "security": "not-performed",
            "hashes": "not-performed",
            "clock": "not-performed",
        },
    }


execute_final_card = play_plan2_final_card
apply_hand_move_effect = move_final_card_to_hand


__all__ = [
    "ANDROID_NATIVE_EVIDENCE",
    "DIRECT_EFFECT_ID",
    "DIRECT_EFFECT_TYPE",
    "FORMAL_AFFECTED_COUNT",
    "FORMAL_AFFECTED_VERSION_REFS",
    "FORMAL_CO_BLOCKED_COUNT",
    "FORMAL_CO_BLOCKED_VERSION_REFS",
    "FORMAL_DIRECT_COUNT",
    "FORMAL_DIRECT_VERSION_REFS",
    "MOVE_EFFECT_ID",
    "MOVE_EFFECT_TYPE",
    "PC_NATIVE_METADATA",
    "PLAY_TRIGGER_ID",
    "TARGET_CARD_ID",
    "TARGET_UPGRADE",
    "TARGET_VERSION_REF",
    "BlockConsumptionTransition",
    "BlockFiveTransition",
    "ExactEffectContract",
    "FinalCardContract",
    "FinalCardMoveHistory",
    "FinalCardPlayHistory",
    "FinalCardPlayRequest",
    "FinalCardPlayTransition",
    "FinalCardRuntime",
    "HandMoveTransition",
    "LessonDependTransition",
    "Plan2FinalCardError",
    "RemainingTurnOneHandoff",
    "apply_hand_move_effect",
    "build_plan2_final_card_native_audit",
    "consume_final_card_block",
    "execute_final_card",
    "execute_lesson_depend_block_consumption_sum",
    "execute_move_block_five",
    "load_final_card_contract",
    "load_plan2_final_card_contract",
    "move_final_card_to_hand",
    "play_plan2_final_card",
    "set_final_card_block",
    "start_final_card_turn",
    "validate_remaining_turn_one_handoff",
]
