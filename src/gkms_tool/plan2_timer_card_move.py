"""Bounded Plan 2 Timer -> CardMove adapter for one card version.

This module owns only ``p_card-02-ido-100_041#0`` and its one remaining
Timer wrapper.  The Timer is installed at the fourth ordered play-effect
slot.  It stores a relative StartTurn boundary and a native listener identity;
it does not search or snapshot CardMove candidates.  At the next StartTurn
boundary the listener dispatches the exact child handoff to
``plan2_card_move_remaining``.  That module remains the sole implementation
of NotLost ordering, GUID rematching, Hand capacity, and Hand overflow.

The adapter is deliberately standalone.  It does not import the Plan 2 core
runtime, the native-search facade, Plan 3, GUI/controller code, coverage JSON,
or any agent/clock/hash/security service.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Final, Literal, TypeAlias

from .card_search import ProduceCardSearchRule
from .exam_native_rng import INT32_MAX
from .master_db import DEFAULT_DATABASE
from .plan2_card_move_remaining import (
    CARD_041,
    EFFECT_NOT_LOST_HAND,
    RemainingCardMoveContract,
    RemainingCardMoveError,
    RemainingCardMoveExecutionContext,
    RemainingCardMoveMutation,
    RemainingCardMoveResult,
    RemainingCardMoveState,
    RemainingCardMoveHandoff,
    SupportInputs,
    TIMER_041,
    load_remaining_card_move_contracts,
    simulate_remaining_card_move,
)
from .plan3_native_state import Plan3NativeCard


TARGET_CARD_ID: Final = CARD_041
TARGET_UPGRADE: Final = 0
TARGET_VERSION_REF: Final = f"{TARGET_CARD_ID}#{TARGET_UPGRADE}"

TIMER_EFFECT_ID: Final = TIMER_041
CHILD_EFFECT_ID: Final = EFFECT_NOT_LOST_HAND
TIMER_DELAY: Final = 1
TIMER_SLOT_INDEX: Final = 3
CHILD_EFFECT_INDEX: Final = 0

PLAN2: Final = "ProducePlanType_Plan2"
MENTAL_SKILL: Final = "ProduceCardCategory_MentalSkill"
MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
MOVE_HAND: Final = "ProduceCardMovePositionType_Hand"
PICK_ALL: Final = "ProducePickRangeType_All"
SEARCH_NOT_LOST: Final = "ProduceCardPositionType_NotLost"
SEARCH_NOT_LOST_ID: Final = "p_card_search-not_lost-p_card-02-ido-3_146"
SEARCH_CARD_ID: Final = "p_card-02-ido-3_146"
TIMER_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamEffectTimer"
CHILD_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamCardMove"
TIMER_EFFECT_GROUP_ID: Final = "effect_group-visible-exam_effect_timer-000"

ORDERED_PLAY_EFFECT_IDS: Final = (
    "e_effect-exam_review-0010",
    "e_effect-exam_playable_value_add-01",
    "e_effect-exam_card_create_id-p_card-02-ido-3_232-0-deck_random-5_5",
    TIMER_EFFECT_ID,
)
COMPLETED_BEFORE_TIMER_EFFECT_IDS: Final = ORDERED_PLAY_EFFECT_IDS[:TIMER_SLOT_INDEX]
CARD_EFFECT_GROUP_IDS: Final = (
    TIMER_EFFECT_GROUP_ID,
    "effect_group-visible-exam_playable_value_add-000",
    "effect_group-visible-exam_review-000",
)

TimerPlayOrigin: TypeAlias = Literal["normal", "forced", "extra"]
TimerParentSourceZone: TypeAlias = Literal["hand", "lost", "playing"]

# This is the local accounting for the single current formal baseline row.
# The six-row child primitive has wider affected/co-blocked accounting; this
# wrapper deliberately reports only the one Timer row it closes.
FORMAL_AFFECTED_COUNT: Final = 1
FORMAL_DIRECT_COUNT: Final = 1
FORMAL_CO_BLOCKED_COUNT: Final = 0


class Plan2TimerCardMoveError(ValueError):
    """Stable fail-closed error for this bounded adapter."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


# A descriptive alias is useful to callers that distinguish Master contract
# errors from runtime/queue errors while keeping one exact exception type.
Plan2TimerCardMoveContractError = Plan2TimerCardMoveError


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMoveAccounting:
    affected: int = FORMAL_AFFECTED_COUNT
    direct: int = FORMAL_DIRECT_COUNT
    co_blocked: int = FORMAL_CO_BLOCKED_COUNT

    def __post_init__(self) -> None:
        for name in ("affected", "direct", "co_blocked"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise Plan2TimerCardMoveError("invalid-accounting", name)


FORMAL_ACCOUNTING: Final = Plan2TimerCardMoveAccounting()
ACCOUNTING: Final = FORMAL_ACCOUNTING


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMoveEffectSlot:
    """One exact entry in the card's ordered play-effect array."""

    slot_index: int
    effect_id: str
    trigger_id: str = ""
    hide_icon: bool = False
    is_once_play_effect: bool = False

    def __post_init__(self) -> None:
        if type(self.slot_index) is not int or self.slot_index < 0:
            raise Plan2TimerCardMoveError("invalid-effect-slot", str(self.slot_index))
        if type(self.effect_id) is not str or not self.effect_id:
            raise Plan2TimerCardMoveError("invalid-effect-id", str(self.slot_index))
        if type(self.trigger_id) is not str:
            raise Plan2TimerCardMoveError("invalid-effect-trigger", self.effect_id)
        if type(self.hide_icon) is not bool or type(self.is_once_play_effect) is not bool:
            raise Plan2TimerCardMoveError("invalid-effect-flags", self.effect_id)


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMoveTimerContract:
    """The exact parent Timer row and its one chained child."""

    effect_id: str
    delay: int
    effect_count: int
    effect_turn: int
    child_effect_id: str

    def __post_init__(self) -> None:
        if type(self.effect_id) is not str or not self.effect_id:
            raise Plan2TimerCardMoveError("invalid-timer-id")
        if type(self.delay) is not int or self.delay < 0:
            raise Plan2TimerCardMoveError("invalid-timer-delay", self.effect_id)
        for name in ("effect_count", "effect_turn"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise Plan2TimerCardMoveError("invalid-timer-field", f"{self.effect_id}:{name}")
        if type(self.child_effect_id) is not str or not self.child_effect_id:
            raise Plan2TimerCardMoveError("invalid-timer-child", self.effect_id)


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMoveContract:
    """Strict Master contract for 041#0 and its Timer child handoff."""

    card_id: str
    upgrade: int
    plan_type: str
    category: str
    move_position_type: str
    effect_group_ids: tuple[str, ...]
    ordered_slots: tuple[Plan2TimerCardMoveEffectSlot, ...]
    timer: Plan2TimerCardMoveTimerContract
    child_contract: RemainingCardMoveContract
    accounting: Plan2TimerCardMoveAccounting = FORMAL_ACCOUNTING

    def __post_init__(self) -> None:
        _validate_contract(self)

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.ordered_slots)


def _strict_equal(actual: object, expected: object, label: str) -> None:
    """Compare JSON values without accepting bool/int or list/dict drift."""

    if type(actual) is not type(expected):
        raise Plan2TimerCardMoveError("master-shape", label)
    if isinstance(expected, dict):
        if set(actual) != set(expected):  # type: ignore[arg-type]
            raise Plan2TimerCardMoveError("master-shape", label)
        for key in expected:
            _strict_equal(actual[key], expected[key], f"{label}.{key}")  # type: ignore[index]
        return
    if isinstance(expected, list):
        if len(actual) != len(expected):  # type: ignore[arg-type]
            raise Plan2TimerCardMoveError("master-shape", label)
        for index, (left, right) in enumerate(zip(actual, expected)):  # type: ignore[arg-type]
            _strict_equal(left, right, f"{label}[{index}]")
        return
    if actual != expected:
        raise Plan2TimerCardMoveError("master-shape", label)


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2TimerCardMoveError("master-json", label) from error
    if not isinstance(decoded, dict):
        raise Plan2TimerCardMoveError("master-json-object", label)
    return decoded


def _json_list(value: object, label: str) -> list[object]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2TimerCardMoveError("master-json", label) from error
    if not isinstance(decoded, list):
        raise Plan2TimerCardMoveError("master-json-list", label)
    return decoded


def _validate_contract(contract: Plan2TimerCardMoveContract) -> None:
    """Reject a hand-built or mutated contract before it reaches the child."""

    if not isinstance(contract, Plan2TimerCardMoveContract):
        raise Plan2TimerCardMoveError("invalid-contract-type")
    if (contract.card_id, contract.upgrade, contract.plan_type, contract.category, contract.move_position_type) != (
        TARGET_CARD_ID,
        TARGET_UPGRADE,
        PLAN2,
        MENTAL_SKILL,
        MOVE_LOST,
    ):
        raise Plan2TimerCardMoveError("card-contract-drift", contract.version_ref)
    if contract.effect_group_ids != CARD_EFFECT_GROUP_IDS:
        raise Plan2TimerCardMoveError("card-effect-group-drift", contract.version_ref)
    expected_slots = tuple(
        Plan2TimerCardMoveEffectSlot(index, effect_id)
        for index, effect_id in enumerate(ORDERED_PLAY_EFFECT_IDS)
    )
    if contract.ordered_slots != expected_slots:
        raise Plan2TimerCardMoveError("card-effect-order-drift", contract.version_ref)
    if contract.timer != Plan2TimerCardMoveTimerContract(
        TIMER_EFFECT_ID, TIMER_DELAY, 1, 0, CHILD_EFFECT_ID
    ):
        raise Plan2TimerCardMoveError("timer-contract-drift", TIMER_EFFECT_ID)
    if contract.accounting != FORMAL_ACCOUNTING:
        raise Plan2TimerCardMoveError("accounting-drift", contract.version_ref)

    child = contract.child_contract
    if not isinstance(child, RemainingCardMoveContract):
        raise Plan2TimerCardMoveError("invalid-child-contract", contract.version_ref)
    if (
        child.card_id,
        child.upgrade,
        child.effect_id,
        child.destination,
        child.pick,
        child.count_min,
        child.count_max,
        child.handoff_kind,
        child.parent_id,
        child.effect_index,
        child.co_blockers,
    ) != (
        TARGET_CARD_ID,
        TARGET_UPGRADE,
        CHILD_EFFECT_ID,
        MOVE_HAND,
        PICK_ALL,
        0,
        0,
        "timer-child",
        TIMER_EFFECT_ID,
        CHILD_EFFECT_INDEX,
        (TIMER_EFFECT_TYPE,),
    ):
        raise Plan2TimerCardMoveError("child-contract-drift", CHILD_EFFECT_ID)
    search = child.search
    expected_search = {
        "id": SEARCH_NOT_LOST_ID,
        "card_position_type": SEARCH_NOT_LOST,
        "produce_card_ids": (SEARCH_CARD_ID,),
        "card_categories": (),
        "upgrade_counts": (),
        "card_rarities": (),
        "plan_type": "ProducePlanType_Unknown",
        "card_status_type": "ProduceCardSearchStatusType_Unknown",
        "order_type": "ProduceCardOrderType_Unknown",
        "card_search_tag": "",
        "produce_card_random_pool_id": "",
        "limit_count": 0,
        "stamina_min_max_type": "ConditionMinMaxType_Unknown",
        "stamina_min": 0,
        "stamina_max": 0,
        "exam_effect_type": "ProduceExamEffectType_Unknown",
        "effect_group_ids": (),
        "is_self": False,
        "produce_card_pool_id": "",
        "cost_type": "ExamCostType_Unknown",
        "is_customized": False,
    }
    for name, expected in expected_search.items():
        if getattr(search, name) != expected:
            raise Plan2TimerCardMoveError("search-contract-drift", f"{SEARCH_NOT_LOST_ID}:{name}")


def _read_only_connection(database: Path) -> sqlite3.Connection:
    path = Path(database)
    if not path.is_file():
        raise Plan2TimerCardMoveError("missing-master-database", str(path))
    try:
        resolved = path.resolve()
        connection = sqlite3.connect(
            f"file:{resolved.as_posix()}?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as error:
        raise Plan2TimerCardMoveError("master-read", str(path)) from error


def _validate_effect_row(
    row: sqlite3.Row,
    *,
    effect_id: str,
    effect_type: str,
    value1: int,
    value2: int,
    effect_count: int,
    effect_turn: int,
    chain_effect_id: str,
    raw_expected: dict[str, object],
) -> None:
    columns = {
        "id": row["id"],
        "effect_type": row["effect_type"],
        "value1": row["value1"],
        "value2": row["value2"],
        "effect_count": row["effect_count"],
        "effect_turn": row["effect_turn"],
        "status_enchant_id": row["status_enchant_id"],
        "chain_effect_id": row["chain_effect_id"],
    }
    expected_columns = {
        "id": effect_id,
        "effect_type": effect_type,
        "value1": value1,
        "value2": value2,
        "effect_count": effect_count,
        "effect_turn": effect_turn,
        "status_enchant_id": "",
        "chain_effect_id": chain_effect_id,
    }
    for name, expected in expected_columns.items():
        if columns[name] != expected or type(columns[name]) is not type(expected):
            raise Plan2TimerCardMoveError("effect-contract-drift", f"{effect_id}:{name}")
    raw = _json_object(row["raw_json"], effect_id)
    for name, expected in raw_expected.items():
        if name not in raw:
            raise Plan2TimerCardMoveError("effect-contract-drift", f"{effect_id}:{name}")
        _strict_equal(raw[name], expected, f"{effect_id}:{name}")


def _timer_raw_expected() -> dict[str, object]:
    return {
        "id": TIMER_EFFECT_ID,
        "effectType": TIMER_EFFECT_TYPE,
        "effectValue1": TIMER_DELAY,
        "effectValue2": 0,
        "effectCount": 1,
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
        "chainProduceExamEffectId": CHILD_EFFECT_ID,
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": "",
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
        "effectGroupIds": [TIMER_EFFECT_GROUP_ID],
    }


def _child_raw_expected() -> dict[str, object]:
    return {
        "id": CHILD_EFFECT_ID,
        "effectType": CHILD_EFFECT_TYPE,
        "effectValue1": 0,
        "effectValue2": 0,
        "effectCount": 0,
        "effectTurn": 0,
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": "ProduceExamEffectType_Unknown",
        "produceCardSearchId": SEARCH_NOT_LOST_ID,
        "movePositionType": MOVE_HAND,
        "pickRangeType": PICK_ALL,
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
        "effectGroupIds": [],
    }


def _play_slots(value: object, label: str) -> tuple[Plan2TimerCardMoveEffectSlot, ...]:
    raw = _json_list(value, label)
    result: list[Plan2TimerCardMoveEffectSlot] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {
            "produceExamTriggerId",
            "produceExamEffectId",
            "hideIcon",
            "isOncePlayEffect",
        }:
            raise Plan2TimerCardMoveError("unsupported-play-effect-shape", label)
        result.append(
            Plan2TimerCardMoveEffectSlot(
                index,
                item["produceExamEffectId"],  # type: ignore[arg-type]
                item["produceExamTriggerId"],  # type: ignore[arg-type]
                item["hideIcon"],  # type: ignore[arg-type]
                item["isOncePlayEffect"],  # type: ignore[arg-type]
            )
        )
    return tuple(result)


def load_plan2_timer_card_move_contract(
    database: Path = DEFAULT_DATABASE,
) -> Plan2TimerCardMoveContract:
    """Load and strictly validate the one current Timer wrapper."""

    database = Path(database)
    try:
        child_contracts = load_remaining_card_move_contracts(database)
    except RemainingCardMoveError as error:
        raise Plan2TimerCardMoveError("child-contract-drift", str(error)) from error
    child = next(
        (value for value in child_contracts if value.version_ref == TARGET_VERSION_REF),
        None,
    )
    if child is None:
        raise Plan2TimerCardMoveError("missing-child-contract", TARGET_VERSION_REF)

    try:
        with closing(_read_only_connection(database)) as connection:
            card = connection.execute(
                "SELECT * FROM card WHERE id = ? AND upgrade_count = ?",
                (TARGET_CARD_ID, TARGET_UPGRADE),
            ).fetchone()
            if card is None:
                raise Plan2TimerCardMoveError("missing-card-version", TARGET_VERSION_REF)
            card_columns = {
                "plan_type": card["plan_type"],
                "category": card["category"],
                "move_position_type": card["move_position_type"],
                "cost_type": card["cost_type"],
                "cost_value": card["cost_value"],
                "stamina": card["stamina"],
                "play_trigger_id": card["play_trigger_id"],
            }
            expected_columns = {
                "plan_type": PLAN2,
                "category": MENTAL_SKILL,
                "move_position_type": MOVE_LOST,
                "cost_type": "ExamCostType_Unknown",
                "cost_value": 0,
                "stamina": 0,
                "play_trigger_id": "",
            }
            for name, expected in expected_columns.items():
                if card_columns[name] != expected or type(card_columns[name]) is not type(expected):
                    raise Plan2TimerCardMoveError("card-contract-drift", f"{TARGET_VERSION_REF}:{name}")

            slots = _play_slots(card["play_effects_json"], TARGET_VERSION_REF)
            raw_card = _json_object(card["raw_json"], TARGET_VERSION_REF)
            raw_card_expected = {
                "id": TARGET_CARD_ID,
                "upgradeCount": TARGET_UPGRADE,
                "planType": PLAN2,
                "category": MENTAL_SKILL,
                "playMovePositionType": MOVE_LOST,
                "stamina": 0,
                "costType": "ExamCostType_Unknown",
                "costValue": 0,
                "effectGroupIds": list(CARD_EFFECT_GROUP_IDS),
                "playProduceExamTriggerId": "",
                "moveEffectTriggerType": "ProduceCardMoveEffectTriggerType_Unknown",
                "moveProduceExamEffectIds": [],
                "moveProduceExamTriggerIds": [],
                "forceStamina": 0,
                "isInitial": True,
                "isInitialDeckProduceCard": False,
                "noDeckDuplication": True,
                "playEffects": [
                    {
                        "produceExamTriggerId": "",
                        "produceExamEffectId": effect_id,
                        "hideIcon": False,
                        "isOncePlayEffect": False,
                    }
                    for effect_id in ORDERED_PLAY_EFFECT_IDS
                ],
            }
            for name, expected in raw_card_expected.items():
                if name not in raw_card:
                    raise Plan2TimerCardMoveError("card-contract-drift", f"{TARGET_VERSION_REF}:{name}")
                _strict_equal(raw_card[name], expected, f"{TARGET_VERSION_REF}:{name}")
            if slots != tuple(
                Plan2TimerCardMoveEffectSlot(index, effect_id)
                for index, effect_id in enumerate(ORDERED_PLAY_EFFECT_IDS)
            ):
                raise Plan2TimerCardMoveError("card-effect-order-drift", TARGET_VERSION_REF)

            timer_row = connection.execute(
                "SELECT * FROM effect WHERE id = ?", (TIMER_EFFECT_ID,)
            ).fetchone()
            if timer_row is None:
                raise Plan2TimerCardMoveError("missing-timer-parent", TIMER_EFFECT_ID)
            _validate_effect_row(
                timer_row,
                effect_id=TIMER_EFFECT_ID,
                effect_type=TIMER_EFFECT_TYPE,
                value1=TIMER_DELAY,
                value2=0,
                effect_count=1,
                effect_turn=0,
                chain_effect_id=CHILD_EFFECT_ID,
                raw_expected=_timer_raw_expected(),
            )
            child_row = connection.execute(
                "SELECT * FROM effect WHERE id = ?", (CHILD_EFFECT_ID,)
            ).fetchone()
            if child_row is None:
                raise Plan2TimerCardMoveError("missing-child", CHILD_EFFECT_ID)
            _validate_effect_row(
                child_row,
                effect_id=CHILD_EFFECT_ID,
                effect_type=CHILD_EFFECT_TYPE,
                value1=0,
                value2=0,
                effect_count=0,
                effect_turn=0,
                chain_effect_id="",
                raw_expected=_child_raw_expected(),
            )
    except Plan2TimerCardMoveError:
        raise
    except sqlite3.Error as error:
        raise Plan2TimerCardMoveError("master-read", TARGET_VERSION_REF) from error

    contract = Plan2TimerCardMoveContract(
        TARGET_CARD_ID,
        TARGET_UPGRADE,
        PLAN2,
        MENTAL_SKILL,
        MOVE_LOST,
        CARD_EFFECT_GROUP_IDS,
        slots,
        Plan2TimerCardMoveTimerContract(
            TIMER_EFFECT_ID, TIMER_DELAY, 1, 0, CHILD_EFFECT_ID
        ),
        child,
    )
    _validate_contract(contract)
    return contract


resolve_plan2_timer_card_move_contract = load_plan2_timer_card_move_contract
load_timer_card_move_contract = load_plan2_timer_card_move_contract


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMovePlayRequest:
    """The authoritative parent-card play boundary that installs the Timer."""

    source_card: Plan3NativeCard
    play_origin: TimerPlayOrigin
    source_zone: TimerParentSourceZone = "hand"
    completed_effect_ids: tuple[str, ...] = COMPLETED_BEFORE_TIMER_EFFECT_IDS
    ordered_effect_ids: tuple[str, ...] = ORDERED_PLAY_EFFECT_IDS
    timer_effect_slot_index: int = TIMER_SLOT_INDEX
    card_play_count_before: int | None = None
    global_card_play_count_before: int | None = None
    turn_card_play_count_before: int | None = None
    transaction_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source_card, Plan3NativeCard):
            raise Plan2TimerCardMoveError("invalid-source-card")
        if self.source_card.card_id != TARGET_CARD_ID or self.source_card.effective_upgrade != TARGET_UPGRADE:
            raise Plan2TimerCardMoveError("unsupported-source-card", self.source_card.card_id)
        if self.play_origin not in {"normal", "forced", "extra"}:
            raise Plan2TimerCardMoveError("invalid-play-origin", str(self.play_origin))
        if self.source_zone not in {"hand", "lost", "playing"}:
            raise Plan2TimerCardMoveError("invalid-source-zone", str(self.source_zone))
        completed = tuple(self.completed_effect_ids)
        ordered = tuple(self.ordered_effect_ids)
        object.__setattr__(self, "completed_effect_ids", completed)
        object.__setattr__(self, "ordered_effect_ids", ordered)
        if completed != COMPLETED_BEFORE_TIMER_EFFECT_IDS:
            raise Plan2TimerCardMoveError("card-effect-order-drift", "completed-before-timer")
        if ordered != ORDERED_PLAY_EFFECT_IDS or self.timer_effect_slot_index != TIMER_SLOT_INDEX:
            raise Plan2TimerCardMoveError("card-effect-order-drift", "ordered-slots")
        if type(self.timer_effect_slot_index) is not int:
            raise Plan2TimerCardMoveError("invalid-timer-slot")
        if self.transaction_id and (type(self.transaction_id) is not str or not self.transaction_id.strip()):
            raise Plan2TimerCardMoveError("invalid-transaction-id")
        for name in (
            "card_play_count_before",
            "global_card_play_count_before",
            "turn_card_play_count_before",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= INT32_MAX):
                raise Plan2TimerCardMoveError("invalid-play-count", name)

    @property
    def source_guid(self) -> str:
        return self.source_card.guid


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMoveParentMove:
    """The parent 041 play's final ordered move ledger entry."""

    card_ref: str
    source_guid: str
    source_zone: TimerParentSourceZone
    destination_zone: str
    play_origin: TimerPlayOrigin
    play_count_after: int
    physically_committed: bool

    def __post_init__(self) -> None:
        if self.card_ref != TARGET_VERSION_REF or not self.source_guid:
            raise Plan2TimerCardMoveError("invalid-parent-move")
        if self.source_zone not in {"hand", "lost", "playing"} or self.destination_zone != "lost":
            raise Plan2TimerCardMoveError("invalid-parent-move-zone")
        if self.play_origin not in {"normal", "forced", "extra"}:
            raise Plan2TimerCardMoveError("invalid-parent-move-origin")
        if type(self.play_count_after) is not int or not 0 <= self.play_count_after <= INT32_MAX:
            raise Plan2TimerCardMoveError("invalid-play-count", self.source_guid)
        if type(self.physically_committed) is not bool:
            raise Plan2TimerCardMoveError("invalid-parent-move-commit")


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMovePlayHistory:
    """Counts, ordered slots, origin, Timer identity, and parent move."""

    transaction_id: str
    card_ref: str
    source_guid: str
    play_origin: TimerPlayOrigin
    ordered_effect_ids: tuple[str, ...]
    timer_effect_slot_index: int
    timer_instance_id: str
    source_zone: TimerParentSourceZone
    destination_zone: str
    card_play_count_before: int
    card_play_count_after: int
    global_card_play_count_before: int
    global_card_play_count_after: int
    turn_card_play_count_before: int
    turn_card_play_count_after: int
    parent_move: Plan2TimerCardMoveParentMove
    phases: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.transaction_id or self.card_ref != TARGET_VERSION_REF:
            raise Plan2TimerCardMoveError("invalid-play-history")
        if self.source_guid != self.parent_move.source_guid or self.timer_instance_id == "":
            raise Plan2TimerCardMoveError("invalid-play-history-identity")
        if self.ordered_effect_ids != ORDERED_PLAY_EFFECT_IDS or self.timer_effect_slot_index != TIMER_SLOT_INDEX:
            raise Plan2TimerCardMoveError("invalid-play-history-order")
        for name in (
            "card_play_count_before", "card_play_count_after",
            "global_card_play_count_before", "global_card_play_count_after",
            "turn_card_play_count_before", "turn_card_play_count_after",
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= INT32_MAX:
                raise Plan2TimerCardMoveError("invalid-play-history-count", name)
        if self.card_play_count_after != self.card_play_count_before + 1:
            raise Plan2TimerCardMoveError("invalid-play-history-count", "card")
        if self.global_card_play_count_after != self.global_card_play_count_before + 1:
            raise Plan2TimerCardMoveError("invalid-play-history-count", "global")
        if self.turn_card_play_count_after != self.turn_card_play_count_before + 1:
            raise Plan2TimerCardMoveError("invalid-play-history-count", "turn")


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMoveChildHistory:
    """A due child result copied from the exact existing CardMove primitive."""

    timer_instance_id: str
    card_ref: str
    due_start_turn_boundary: int
    child_effect_id: str
    selected_guids: tuple[str, ...]
    moved_guids: tuple[str, ...]
    skipped_guids: tuple[str, ...]
    mutations: tuple[RemainingCardMoveMutation, ...]
    callbacks: tuple[str, ...]
    phases: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.timer_instance_id or self.card_ref != TARGET_VERSION_REF:
            raise Plan2TimerCardMoveError("invalid-child-history")
        if self.child_effect_id != CHILD_EFFECT_ID or type(self.due_start_turn_boundary) is not int or self.due_start_turn_boundary < 1:
            raise Plan2TimerCardMoveError("invalid-child-history-shape")
        if not set(self.moved_guids).issubset(set(self.selected_guids)):
            raise Plan2TimerCardMoveError("invalid-child-history-guid-lineage")


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMoveQueuedTimer:
    """One native one-shot listener; no search candidates are stored here."""

    instance_id: str
    source_guid: str
    card_ref: str
    play_origin: TimerPlayOrigin
    install_sequence: int
    installed_start_turn_boundary: int
    contract: Plan2TimerCardMoveContract

    def __post_init__(self) -> None:
        _validate_contract(self.contract)
        if not self.instance_id or not self.source_guid or self.card_ref != TARGET_VERSION_REF:
            raise Plan2TimerCardMoveError("invalid-timer-identity")
        if self.contract.timer.effect_id != TIMER_EFFECT_ID:
            raise Plan2TimerCardMoveError("invalid-timer-identity", self.instance_id)
        if self.play_origin not in {"normal", "forced", "extra"}:
            raise Plan2TimerCardMoveError("invalid-play-origin", self.instance_id)
        for name in ("install_sequence", "installed_start_turn_boundary"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise Plan2TimerCardMoveError("invalid-timer-sequence", self.instance_id)
        if self.install_sequence == 0:
            raise Plan2TimerCardMoveError("invalid-timer-sequence", self.instance_id)

    @property
    def timer(self) -> Plan2TimerCardMoveTimerContract:
        return self.contract.timer

    @property
    def due_start_turn_boundary(self) -> int:
        return self.installed_start_turn_boundary + self.timer.delay

    @property
    def handoff(self) -> RemainingCardMoveHandoff:
        return RemainingCardMoveHandoff(
            "timer-child",
            TIMER_EFFECT_ID,
            "timer-expiry-child",
            CHILD_EFFECT_INDEX,
            "callback",
            1,
        )


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMoveRuntime:
    """Standalone immutable Timer queue plus the shared native GUID state."""

    native_state: RemainingCardMoveState
    turns_remaining: int = 1
    start_turn_boundaries: int = 0
    queue: tuple[Plan2TimerCardMoveQueuedTimer, ...] = ()
    next_install_sequence: int = 1
    contract: Plan2TimerCardMoveContract | None = None
    global_card_play_count: int = 0
    turn_card_play_count: int = 0
    card_play_counts: tuple[tuple[str, int], ...] = ()
    history: tuple[Plan2TimerCardMovePlayHistory | Plan2TimerCardMoveChildHistory, ...] = ()
    parent_moves: tuple[Plan2TimerCardMoveParentMove, ...] = ()
    child_dispatch_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.native_state, RemainingCardMoveState):
            raise Plan2TimerCardMoveError("invalid-native-state")
        for name in ("turns_remaining", "start_turn_boundaries", "next_install_sequence", "global_card_play_count", "turn_card_play_count", "child_dispatch_count"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= INT32_MAX:
                raise Plan2TimerCardMoveError("invalid-runtime-counter", name)
        if self.next_install_sequence == 0:
            raise Plan2TimerCardMoveError("invalid-runtime-counter", "next_install_sequence")
        if self.contract is not None:
            _validate_contract(self.contract)
        queue = tuple(self.queue)
        object.__setattr__(self, "queue", queue)
        if queue and self.contract is None:
            object.__setattr__(self, "contract", queue[0].contract)
        active_contract = self.contract
        previous_sequence = 0
        instance_ids: set[str] = set()
        for item in queue:
            if not isinstance(item, Plan2TimerCardMoveQueuedTimer):
                raise Plan2TimerCardMoveError("invalid-timer-queue-item")
            if active_contract is not None and item.contract != active_contract:
                raise Plan2TimerCardMoveError("timer-queue-contract-drift", item.instance_id)
            if item.install_sequence <= previous_sequence or item.instance_id in instance_ids:
                raise Plan2TimerCardMoveError("timer-queue-order-drift", item.instance_id)
            previous_sequence = item.install_sequence
            instance_ids.add(item.instance_id)
        if queue and self.next_install_sequence <= previous_sequence:
            raise Plan2TimerCardMoveError("invalid-runtime-counter", "next_install_sequence")

        counts = tuple(self.card_play_counts)
        normalized_counts: list[tuple[str, int]] = []
        seen_guids: set[str] = set()
        for pair in counts:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise Plan2TimerCardMoveError("invalid-card-play-counts")
            guid, value = pair
            if type(guid) is not str or not guid or guid in seen_guids or type(value) is not int or not 0 <= value <= INT32_MAX:
                raise Plan2TimerCardMoveError("invalid-card-play-counts")
            seen_guids.add(guid)
            normalized_counts.append((guid, value))
        if not normalized_counts:
            normalized_counts = [(card.guid, card.play_count) for card in self.native_state.all_cards]
        object.__setattr__(self, "card_play_counts", tuple(normalized_counts))

        history = tuple(self.history)
        if any(not isinstance(event, (Plan2TimerCardMovePlayHistory, Plan2TimerCardMoveChildHistory)) for event in history):
            raise Plan2TimerCardMoveError("invalid-history")
        object.__setattr__(self, "history", history)
        moves = tuple(self.parent_moves)
        if any(not isinstance(event, Plan2TimerCardMoveParentMove) for event in moves):
            raise Plan2TimerCardMoveError("invalid-parent-move-history")
        object.__setattr__(self, "parent_moves", moves)

    @property
    def terminal(self) -> bool:
        return self.turns_remaining == 0

    @property
    def queue_instance_ids(self) -> tuple[str, ...]:
        return tuple(item.instance_id for item in self.queue)

    @property
    def play_history(self) -> tuple[Plan2TimerCardMovePlayHistory, ...]:
        return tuple(event for event in self.history if isinstance(event, Plan2TimerCardMovePlayHistory))

    @property
    def move_history(self) -> tuple[Plan2TimerCardMoveChildHistory, ...]:
        return tuple(event for event in self.history if isinstance(event, Plan2TimerCardMoveChildHistory))

    @property
    def move_count(self) -> int:
        """Number of child GUID relocations, as reported by the primitive."""

        return sum(len(event.moved_guids) for event in self.move_history)

    @property
    def card_play_count(self) -> int:
        return len(self.play_history)

    def card_play_count_for_guid(self, guid: str) -> int:
        return dict(self.card_play_counts).get(guid, 0)


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMoveTurnRequest:
    extra_turns: int = 0
    hand_limit: int = 5
    lesson_type: str = "ProduceStepLessonType_LessonVocal"
    support_upgrades: SupportInputs = ()
    support_card_searches: Mapping[str, ProduceCardSearchRule] | None = None

    def __post_init__(self) -> None:
        if type(self.extra_turns) is not int or not 0 <= self.extra_turns <= INT32_MAX:
            raise Plan2TimerCardMoveError("invalid-extra-turns")
        try:
            RemainingCardMoveExecutionContext(
                self.hand_limit,
                self.lesson_type,
                self.support_upgrades,
                self.support_card_searches,
            )
        except RemainingCardMoveError as error:
            raise Plan2TimerCardMoveError("invalid-child-context", str(error)) from error


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMoveInstallResult:
    before: Plan2TimerCardMoveRuntime
    after: Plan2TimerCardMoveRuntime
    queued_timer: Plan2TimerCardMoveQueuedTimer
    history: Plan2TimerCardMovePlayHistory
    trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2TimerCardMoveTurnResult:
    before: Plan2TimerCardMoveRuntime
    after: Plan2TimerCardMoveRuntime
    extra_turns: int
    due_timer_instance_ids: tuple[str, ...]
    child_results: tuple[RemainingCardMoveResult, ...]
    removed_timer_instance_ids: tuple[str, ...]
    trace: tuple[str, ...]

    @property
    def terminal(self) -> bool:
        return self.after.terminal

    @property
    def moved_guids(self) -> tuple[str, ...]:
        return tuple(guid for child in self.child_results for guid in child.moved_guids)

    @property
    def terminal_stranded_instance_ids(self) -> tuple[str, ...]:
        return self.after.queue_instance_ids if self.after.terminal else ()


def _validate_runtime_contract(
    runtime: Plan2TimerCardMoveRuntime,
    database: Path,
) -> Plan2TimerCardMoveContract:
    current = load_plan2_timer_card_move_contract(database)
    if runtime.contract is not None and runtime.contract != current:
        raise Plan2TimerCardMoveError("runtime-contract-stale", TARGET_VERSION_REF)
    if runtime.queue and any(item.contract != current for item in runtime.queue):
        raise Plan2TimerCardMoveError("timer-queue-contract-drift", TARGET_VERSION_REF)
    return current


def _counter_after(value: int, label: str) -> int:
    if value >= INT32_MAX:
        raise Plan2TimerCardMoveError("counter-overflow", label)
    return value + 1


def _commit_parent_move_if_present(
    state: RemainingCardMoveState,
    request: Plan2TimerCardMovePlayRequest,
    play_count_after: int,
) -> tuple[RemainingCardMoveState, bool]:
    """Commit 041 to Lost only when the caller supplied that native GUID.

    The Timer leaf does not invent a source card or a second CardMove search.
    A parent card already present in the shared state is moved with exact GUID
    identity; a caller that keeps the parent play state outside this child
    state still gets the explicit parent-move ledger event.
    """

    found: list[tuple[str, Plan3NativeCard]] = []
    for zone in ("hand", "lost", "deck", "grave", "hold"):
        for card in getattr(state, zone):
            if card.guid == request.source_guid:
                found.append((zone, card))
    if state.playing is not None and state.playing.guid == request.source_guid:
        found.append(("playing", state.playing))
    if len(found) > 1:
        raise Plan2TimerCardMoveError("parent-guid-duplicate", request.source_guid)
    if not found:
        return state, False
    source_zone, current = found[0]
    if source_zone != request.source_zone:
        raise Plan2TimerCardMoveError(
            "parent-source-zone-drift",
            f"{request.source_guid}:{source_zone}!={request.source_zone}",
        )
    if current.card_id != TARGET_CARD_ID or current.effective_upgrade != TARGET_UPGRADE:
        raise Plan2TimerCardMoveError("parent-card-shape-drift", request.source_guid)
    if request.card_play_count_before is not None and current.play_count != request.card_play_count_before:
        raise Plan2TimerCardMoveError("parent-play-count-drift", request.source_guid)
    moved = replace(current, play_count=play_count_after)
    changes: dict[str, object] = {
        name: tuple(card for card in getattr(state, name) if card.guid != request.source_guid)
        for name in ("hand", "deck", "grave", "lost", "hold")
    }
    changes["playing"] = None if source_zone == "playing" else state.playing
    lost = changes["lost"]
    assert isinstance(lost, tuple)
    changes["lost"] = (*lost, moved)
    return replace(state, **changes), True


def install_plan2_timer_card_move(
    runtime: Plan2TimerCardMoveRuntime,
    request: Plan2TimerCardMovePlayRequest,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2TimerCardMoveInstallResult:
    """Complete 041's ordered play slots and install its one-shot Timer.

    No child search is performed here.  ``source_zone``/counts are explicit
    parent-play metadata; the shared state is changed only for an already
    present source GUID, preserving the native card move boundary.
    """

    if not isinstance(runtime, Plan2TimerCardMoveRuntime):
        raise Plan2TimerCardMoveError("invalid-runtime")
    if not isinstance(request, Plan2TimerCardMovePlayRequest):
        raise Plan2TimerCardMoveError("invalid-play-request")
    if runtime.terminal:
        raise Plan2TimerCardMoveError("terminal-runtime")
    contract = _validate_runtime_contract(runtime, Path(database))

    counts = dict(runtime.card_play_counts)
    prior_card_count = counts.get(request.source_guid, request.source_card.play_count)
    if request.card_play_count_before is not None and request.card_play_count_before != prior_card_count:
        raise Plan2TimerCardMoveError("play-count-baseline-drift", request.source_guid)
    if request.source_guid in counts and request.source_card.play_count != prior_card_count:
        raise Plan2TimerCardMoveError("source-card-count-drift", request.source_guid)
    prior_global = runtime.global_card_play_count
    prior_turn = runtime.turn_card_play_count
    if request.global_card_play_count_before is not None and request.global_card_play_count_before != prior_global:
        raise Plan2TimerCardMoveError("global-count-baseline-drift")
    if request.turn_card_play_count_before is not None and request.turn_card_play_count_before != prior_turn:
        raise Plan2TimerCardMoveError("turn-count-baseline-drift")
    after_card_count = _counter_after(prior_card_count, "card_play_count")
    after_global = _counter_after(prior_global, "global_card_play_count")
    after_turn = _counter_after(prior_turn, "turn_card_play_count")

    sequence = runtime.next_install_sequence
    instance_id = f"{TIMER_EFFECT_ID}|{request.source_guid}|{sequence}"
    queued = Plan2TimerCardMoveQueuedTimer(
        instance_id,
        request.source_guid,
        TARGET_VERSION_REF,
        request.play_origin,
        sequence,
        runtime.start_turn_boundaries,
        contract,
    )
    native_after, physical = _commit_parent_move_if_present(
        runtime.native_state, request, after_card_count
    )
    parent_move = Plan2TimerCardMoveParentMove(
        TARGET_VERSION_REF,
        request.source_guid,
        request.source_zone,
        "lost",
        request.play_origin,
        after_card_count,
        physical,
    )
    transaction_id = request.transaction_id or f"card-play:{request.source_guid}:{sequence}"
    history = Plan2TimerCardMovePlayHistory(
        transaction_id,
        TARGET_VERSION_REF,
        request.source_guid,
        request.play_origin,
        ORDERED_PLAY_EFFECT_IDS,
        TIMER_SLOT_INDEX,
        instance_id,
        request.source_zone,
        "lost",
        prior_card_count,
        after_card_count,
        prior_global,
        after_global,
        prior_turn,
        after_turn,
        parent_move,
        (
            "card-play:ordered-slot-0",
            "card-play:ordered-slot-1",
            "card-play:ordered-slot-2",
            "card-play:ordered-slot-3",
            "ExamEffectTimer.install",
            "parent-card-move:Lost",
            "card-play-count:update",
        ),
    )
    counts[request.source_guid] = after_card_count
    after = replace(
        runtime,
        native_state=native_after,
        queue=(*runtime.queue, queued),
        next_install_sequence=sequence + 1,
        contract=contract,
        global_card_play_count=after_global,
        turn_card_play_count=after_turn,
        card_play_counts=tuple(counts.items()),
        history=(*runtime.history, history),
        parent_moves=(*runtime.parent_moves, parent_move),
    )
    return Plan2TimerCardMoveInstallResult(
        runtime,
        after,
        queued,
        history,
        history.phases,
    )


def install_timer_card_move(
    runtime: Plan2TimerCardMoveRuntime,
    source_card: Plan3NativeCard,
    *,
    play_origin: TimerPlayOrigin = "normal",
    source_zone: TimerParentSourceZone | None = None,
    database: Path = DEFAULT_DATABASE,
    card_play_count_before: int | None = None,
    global_card_play_count_before: int | None = None,
    turn_card_play_count_before: int | None = None,
    transaction_id: str = "",
) -> Plan2TimerCardMoveInstallResult:
    """Convenience form of :func:`install_plan2_timer_card_move`."""

    if source_zone is None:
        located = [
            zone
            for zone in ("hand", "lost")
            if any(card.guid == source_card.guid for card in getattr(runtime.native_state, zone))
        ]
        if runtime.native_state.playing is not None and runtime.native_state.playing.guid == source_card.guid:
            located.append("playing")
        if len(located) > 1:
            raise Plan2TimerCardMoveError("parent-guid-duplicate", source_card.guid)
        source_zone = located[0] if located else "hand"
    request = Plan2TimerCardMovePlayRequest(
        source_card,
        play_origin,
        source_zone,
        card_play_count_before=card_play_count_before,
        global_card_play_count_before=global_card_play_count_before,
        turn_card_play_count_before=turn_card_play_count_before,
        transaction_id=transaction_id,
    )
    return install_plan2_timer_card_move(runtime, request, database=database)


def _turn_request(
    request: Plan2TimerCardMoveTurnRequest | None,
    *,
    extra_turns: int | None,
    hand_limit: int | None,
    lesson_type: str | None,
    support_upgrades: SupportInputs | None,
    support_card_searches: Mapping[str, ProduceCardSearchRule] | None,
) -> Plan2TimerCardMoveTurnRequest:
    if request is not None:
        if not isinstance(request, Plan2TimerCardMoveTurnRequest):
            raise Plan2TimerCardMoveError("invalid-turn-request")
        if any(value is not None for value in (extra_turns, hand_limit, lesson_type, support_upgrades, support_card_searches)):
            raise Plan2TimerCardMoveError("duplicate-turn-request-options")
        return request
    return Plan2TimerCardMoveTurnRequest(
        0 if extra_turns is None else extra_turns,
        5 if hand_limit is None else hand_limit,
        "ProduceStepLessonType_LessonVocal" if lesson_type is None else lesson_type,
        () if support_upgrades is None else support_upgrades,
        support_card_searches,
    )


def advance_plan2_timer_card_move(
    runtime: Plan2TimerCardMoveRuntime,
    request: Plan2TimerCardMoveTurnRequest | None = None,
    *,
    extra_turns: int | None = None,
    hand_limit: int | None = None,
    lesson_type: str | None = None,
    support_upgrades: SupportInputs | None = None,
    support_card_searches: Mapping[str, ProduceCardSearchRule] | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2TimerCardMoveTurnResult:
    """End the current turn and run due Timer children at the next boundary."""

    if not isinstance(runtime, Plan2TimerCardMoveRuntime):
        raise Plan2TimerCardMoveError("invalid-runtime")
    turn = _turn_request(
        request,
        extra_turns=extra_turns,
        hand_limit=hand_limit,
        lesson_type=lesson_type,
        support_upgrades=support_upgrades,
        support_card_searches=support_card_searches,
    )
    _validate_runtime_contract(runtime, Path(database))
    if runtime.terminal:
        return Plan2TimerCardMoveTurnResult(
            runtime,
            runtime,
            turn.extra_turns,
            (),
            (),
            (),
            ("terminal:already-terminal", "terminal:no-StartTurn:no-TurnTimer"),
        )

    if runtime.turns_remaining > INT32_MAX - turn.extra_turns:
        raise Plan2TimerCardMoveError("counter-overflow", "turns_remaining")
    granted = runtime.turns_remaining + turn.extra_turns
    after_end = granted - 1
    trace: list[str] = [f"EndTurn:extra-turns:{turn.extra_turns}", "EndTurn:decrement"]
    if after_end == 0:
        trace.append("terminal:no-StartTurn:no-TurnTimer")
        terminal = replace(runtime, turns_remaining=0)
        return Plan2TimerCardMoveTurnResult(
            runtime,
            terminal,
            turn.extra_turns,
            (),
            (),
            (),
            tuple(trace),
        )

    if runtime.start_turn_boundaries >= INT32_MAX:
        raise Plan2TimerCardMoveError("counter-overflow", "start_turn_boundaries")
    boundary = runtime.start_turn_boundaries + 1
    trace.extend((f"StartTurn:boundary:{boundary}", "StartPlay"))
    working = replace(
        runtime,
        turns_remaining=after_end,
        start_turn_boundaries=boundary,
        turn_card_play_count=0,
    )

    due: list[Plan2TimerCardMoveQueuedTimer] = []
    for queued in runtime.queue:
        age = boundary - queued.installed_start_turn_boundary
        if age < 0:
            raise Plan2TimerCardMoveError("timer-relative-boundary-drift", queued.instance_id)
        if age == queued.timer.delay:
            due.append(queued)
        elif age > queued.timer.delay:
            raise Plan2TimerCardMoveError("stale-timer", queued.instance_id)
    trace.append(
        "TurnTimer:" + ("due-in-queue-order" if due else "none")
    )

    context = RemainingCardMoveExecutionContext(
        turn.hand_limit,
        turn.lesson_type,
        turn.support_upgrades,
        turn.support_card_searches,
    )
    current_state = working.native_state
    child_results: list[RemainingCardMoveResult] = []
    history: list[Plan2TimerCardMovePlayHistory | Plan2TimerCardMoveChildHistory] = list(working.history)
    for queued in due:
        # This call captures the NotLost list now, at expiry.  Nothing in the
        # queue item contains candidates from installation time.
        result = simulate_remaining_card_move(
            current_state,
            queued.contract.child_contract,
            queued.handoff,
            context,
            database=Path(database),
        )
        child_results.append(result)
        history.append(
            Plan2TimerCardMoveChildHistory(
                queued.instance_id,
                TARGET_VERSION_REF,
                boundary,
                CHILD_EFFECT_ID,
                result.snapshot.selected_guids,
                result.moved_guids,
                result.skipped_guids,
                result.mutations,
                result.callbacks,
                result.phases,
            )
        )
        current_state = result.after
        trace.append(f"TimerChild:{queued.instance_id}:CardMove")
    due_ids = tuple(item.instance_id for item in due)
    remaining_queue = tuple(item for item in working.queue if item.instance_id not in set(due_ids))
    after = replace(
        working,
        native_state=current_state,
        queue=remaining_queue,
        history=tuple(history),
        child_dispatch_count=working.child_dispatch_count + len(child_results),
    )
    if due_ids:
        trace.append("RemoveTurnTimerTriggeredStatus:after-child-queue")
    trace.append("return-to-caller-queue")
    return Plan2TimerCardMoveTurnResult(
        runtime,
        after,
        turn.extra_turns,
        due_ids,
        tuple(child_results),
        due_ids,
        tuple(trace),
    )


advance_timer_card_move_turn = advance_plan2_timer_card_move


__all__ = [
    "ACCOUNTING",
    "CARD_EFFECT_GROUP_IDS",
    "CHILD_EFFECT_ID",
    "COMPLETED_BEFORE_TIMER_EFFECT_IDS",
    "FORMAL_ACCOUNTING",
    "FORMAL_AFFECTED_COUNT",
    "FORMAL_CO_BLOCKED_COUNT",
    "FORMAL_DIRECT_COUNT",
    "MENTAL_SKILL",
    "MOVE_HAND",
    "MOVE_LOST",
    "ORDERED_PLAY_EFFECT_IDS",
    "PICK_ALL",
    "Plan2TimerCardMoveAccounting",
    "Plan2TimerCardMoveChildHistory",
    "Plan2TimerCardMoveContract",
    "Plan2TimerCardMoveContractError",
    "Plan2TimerCardMoveEffectSlot",
    "Plan2TimerCardMoveError",
    "Plan2TimerCardMoveInstallResult",
    "Plan2TimerCardMoveParentMove",
    "Plan2TimerCardMovePlayHistory",
    "Plan2TimerCardMovePlayRequest",
    "Plan2TimerCardMoveQueuedTimer",
    "Plan2TimerCardMoveRuntime",
    "Plan2TimerCardMoveTimerContract",
    "Plan2TimerCardMoveTurnRequest",
    "Plan2TimerCardMoveTurnResult",
    "SEARCH_NOT_LOST",
    "SEARCH_NOT_LOST_ID",
    "SEARCH_CARD_ID",
    "TARGET_CARD_ID",
    "TARGET_UPGRADE",
    "TARGET_VERSION_REF",
    "TIMER_DELAY",
    "TIMER_EFFECT_ID",
    "TIMER_SLOT_INDEX",
    "advance_plan2_timer_card_move",
    "advance_timer_card_move_turn",
    "load_plan2_timer_card_move_contract",
    "load_timer_card_move_contract",
    "resolve_plan2_timer_card_move_contract",
    "install_plan2_timer_card_move",
    "install_timer_card_move",
]
