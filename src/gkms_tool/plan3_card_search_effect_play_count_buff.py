"""Standalone Plan 3 ``ExamCardSearchEffectPlayCountBuff`` runtime.

This module owns only the exact Common effect row
``e_effect-exam_card_search_effect_play_count_buff-0001-01-inf-p_card_search-``
``n-r-sr-ssr-playing-all-0_0``.  The Android executor adds one immutable
status layer.  ``UsePlayCountBuff`` consumes one matching layer immediately
before the normal card-effect loop; its returned ``N`` makes that loop run
``N + 1`` times.  The normal card command still owns cost, history, play
count, movement, and trigger/listener side effects.

The resolver deliberately uses the existing Master card loader, the shared
card-search shape validator, and :class:`Plan3NativeState`.  It never creates
card GUIDs or advances RNG.  A changed Master/raw shape, an unavailable
runtime-mutated effect list, or an unsupported status turn fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType

from .card_search import (
    ProduceCardSearchRule,
    exact_playing_card_search_mismatches,
    load_produce_card_search,
)
from .master_db import DEFAULT_DATABASE
from .plan3_force_play_card_search import load_force_play_target_card_master
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


EFFECT_TYPE = "ProduceExamEffectType_ExamCardSearchEffectPlayCountBuff"
EFFECT_TYPE_VALUE = 38
EFFECT_ID = (
    "e_effect-exam_card_search_effect_play_count_buff-0001-01-inf-"
    "p_card_search-n-r-sr-ssr-playing-all-0_0"
)
SEARCH_ID = "p_card_search-n-r-sr-ssr-playing"
EFFECT_GROUP_ID = "effect_group-visible-exam_card_search_effect_play_count_buff-000"

CARD_ID = "p_card-03-ido-3_193"
CARD_UPGRADES = (0, 1, 2, 3)
AFFECTED_CARD_VERSIONS = tuple((CARD_ID, upgrade) for upgrade in CARD_UPGRADES)
AFFECTED_CARD_VERSION_COUNT = len(AFFECTED_CARD_VERSIONS)

FORCE_PLAY_EFFECT_ID = (
    "e_effect-exam_force_play_card_search-p_card_search-deck_grave-select-1_1"
)
PLAYABLE_VALUE_EFFECT_ID = "e_effect-exam_playable_value_add-01"
ORDERED_AFFECTED_CARD_EFFECT_IDS = (
    PLAYABLE_VALUE_EFFECT_ID,
    EFFECT_ID,
    FORCE_PLAY_EFFECT_ID,
)

PLAN_TYPE_PLAN3 = "ProducePlanType_Plan3"
CATEGORY_MENTAL_SKILL = "ProduceCardCategory_MentalSkill"
COST_UNKNOWN = "ExamCostType_Unknown"
MOVE_LOST = "ProduceCardMovePositionType_Lost"
RARITY_SSR = "ProduceCardRarity_Ssr"
SEARCH_RARITIES = (
    "ProduceCardRarity_N",
    "ProduceCardRarity_R",
    "ProduceCardRarity_Sr",
    "ProduceCardRarity_Ssr",
)

ANDROID_VERSION = "Android v3.2.3"
ANDROID_EXECUTOR_CTOR = "0x7E8BA7C"
ANDROID_EXECUTOR_EXECUTE = "0x7E8BC5C"
ANDROID_TRY_ADD = "0x7E9B64C"
ANDROID_GET = "0x7E9B824"
ANDROID_USE = "0x7E9CDFC"
ANDROID_EXECUTE_CARD_COMMAND_IMPL = "0x7ECEC00"
ANDROID_EXECUTE_CARD_COMMAND_METHOD = "0x7ECE628"

ANDROID_NATIVE_ADDRESSES = MappingProxyType(
    {
        "executor_ctor": ANDROID_EXECUTOR_CTOR,
        "executor_execute": ANDROID_EXECUTOR_EXECUTE,
        "try_add_play_count_buff_status": ANDROID_TRY_ADD,
        "get_play_count_buff": ANDROID_GET,
        "use_play_count_buff": ANDROID_USE,
        "execute_card_command_impl_callsite": ANDROID_EXECUTE_CARD_COMMAND_IMPL,
    }
)

# This is the sequence the core hook must preserve.  The standalone adapter
# emits no command and applies none of the post-effect transitions itself.
NATIVE_CARD_TRANSACTION_ORDER = (
    "accepted-play-and-is-playable-gate",
    "cost-paid-once-before-execute-card-command",
    "set-playing-card-transient",
    "use-play-count-buff-once-before-direct-effect-iteration",
    "direct-effect-pass-0-in-master-order",
    "once-only-effects-skipped-on-passes-after-zero",
    "play-effect-trigger-resolution-per-direct-effect-pass",
    "card-play-count-once-after-all-direct-effect-passes",
    "use-card-after-check-history-event-once",
    "move-play-card-once-after-card-play-count",
    "separate-activity-and-after-move-settlement",
)

CORE_HOOK_REQUIREMENTS = (
    "call from ExecuteCardCommandImpl at 0x7ECEC00 after IsPlayable succeeds",
    "bind the live current Playing card; do not search a copied zone snapshot",
    "call UsePlayCountBuff exactly once before PlayProduceExamEffectList iteration",
    "interpret return N as N additional normal-effect passes (total N+1)",
    "run isOncePlayEffect entries only on pass zero",
    "pay cost once before the hook; never pay once per repeated pass",
    "increment card/global play count and append history once after all passes",
    "delegate the single settled transition to the existing plan3_card_history primitive",
    "settle the Master move once, then run post-move trigger/listener callbacks",
    "on rejection or unknown shape, consume no status and emit no card side effect",
)

INT32_MAX = 2**31 - 1


class CardSearchEffectPlayCountBuffError(ValueError):
    """Base fail-closed error with a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class CardSearchEffectPlayCountBuffResolutionError(
    CardSearchEffectPlayCountBuffError
):
    """Master, card, metadata, or native-shape resolution failed."""


class CardSearchEffectPlayCountBuffInputError(CardSearchEffectPlayCountBuffError):
    """A typed runtime input is malformed."""


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CardSearchEffectPlayCountBuffInputError("invalid-nonnegative-int", label)
    return value


def _strict_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise CardSearchEffectPlayCountBuffInputError("invalid-bool", label)
    return value


def _strict_equal(actual: object, expected: object) -> bool:
    """Compare JSON values without allowing ``True == 1`` coercion."""

    if type(actual) is not type(expected):
        return False
    return actual == expected


def _resolution_detail(error: BaseException) -> tuple[str, str]:
    if isinstance(error, CardSearchEffectPlayCountBuffError):
        return error.code, error.detail or error.code
    return "resolution-failed", str(error) or error.__class__.__name__


@dataclass(frozen=True, slots=True)
class CardSearchEffectPlayCountBuffEffectRow:
    id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    search_id: str
    move_position_type: str
    pick_range_type: str
    pick_count_reference_search_id: str
    pick_count_type: str
    pick_count_min: int
    pick_count_max: int
    search_id2: str
    pick_range_type2: str
    pick_count_reference_search_id2: str
    pick_count_type2: str
    pick_count_min2: int
    pick_count_max2: int
    chain_effect_ids: tuple[str, ...]
    card_status_enchant_id: str
    card_grow_effect_ids: tuple[str, ...]
    effect_group_ids: tuple[str, ...]


EXACT_EFFECT_ROW = CardSearchEffectPlayCountBuffEffectRow(
    id=EFFECT_ID,
    effect_type=EFFECT_TYPE,
    value1=1,
    value2=0,
    effect_count=1,
    effect_turn=-1,
    status_enchant_id="",
    chain_effect_id="",
    search_id=SEARCH_ID,
    move_position_type="ProduceCardMovePositionType_Unknown",
    pick_range_type="ProducePickRangeType_All",
    pick_count_reference_search_id="",
    pick_count_type="ProducePickCountType_Unknown",
    pick_count_min=0,
    pick_count_max=0,
    search_id2="",
    pick_range_type2="ProducePickRangeType_Unknown",
    pick_count_reference_search_id2="",
    pick_count_type2="ProducePickCountType_Unknown",
    pick_count_min2=0,
    pick_count_max2=0,
    chain_effect_ids=(),
    card_status_enchant_id="",
    card_grow_effect_ids=(),
    effect_group_ids=(EFFECT_GROUP_ID,),
)
EFFECT_BY_ID = MappingProxyType({EFFECT_ID: EXACT_EFFECT_ROW})

_MASTER_RAW_FIELDS = MappingProxyType(
    {
        "effectType": "effect_type",
        "effectValue1": "value1",
        "effectValue2": "value2",
        "effectCount": "effect_count",
        "effectTurn": "effect_turn",
        "produceCardSearchId": "search_id",
        "movePositionType": "move_position_type",
        "pickRangeType": "pick_range_type",
        "pickCountReferenceProduceCardSearchId": "pick_count_reference_search_id",
        "pickCountType": "pick_count_type",
        "pickCountMin": "pick_count_min",
        "pickCountMax": "pick_count_max",
        "produceCardSearchId2": "search_id2",
        "pickRangeType2": "pick_range_type2",
        "pickCountReferenceProduceCardSearchId2": "pick_count_reference_search_id2",
        "pickCountType2": "pick_count_type2",
        "pickCountMin2": "pick_count_min2",
        "pickCountMax2": "pick_count_max2",
        "chainProduceExamEffectId": "chain_effect_id",
        "chainProduceExamEffectIds": "chain_effect_ids",
        "produceExamStatusEnchantId": "status_enchant_id",
        "produceCardStatusEnchantId": "card_status_enchant_id",
        "produceCardGrowEffectIds": "card_grow_effect_ids",
        "effectGroupIds": "effect_group_ids",
    }
)
_NEUTRAL_MASTER_RAW_FIELDS = MappingProxyType(
    {
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": "ProduceExamEffectType_Unknown",
    }
)


@dataclass(frozen=True, slots=True)
class CardSearchEffectPlayCountBuffContract:
    row: CardSearchEffectPlayCountBuffEffectRow
    search: ProduceCardSearchRule

    @property
    def executable(self) -> bool:
        return self.row.id == EFFECT_ID and self.search.id == SEARCH_ID

    @property
    def unresolved_reasons(self) -> tuple[str, ...]:
        return () if self.executable else ("contract-shape",)


@dataclass(frozen=True, slots=True)
class CardSearchEffectPlayCountBuffUnresolvedInput:
    field: str
    reason: str


def load_master_card_search_effect_play_count_buff_rows(
    database: Path = DEFAULT_DATABASE,
) -> tuple[dict[str, object], ...]:
    """Load only rows with this native effect type from the local Master DB."""

    try:
        with sqlite3.connect(Path(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM effect WHERE effect_type = ? ORDER BY rowid",
                (EFFECT_TYPE,),
            ).fetchall()
    except sqlite3.Error as error:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "effect-master-load-failed", str(database)
        ) from error
    return tuple(dict(row) for row in rows)


def _effect_row_values(
    values: Mapping[str, object], payload: Mapping[str, object]
) -> CardSearchEffectPlayCountBuffEffectRow:
    try:
        row = CardSearchEffectPlayCountBuffEffectRow(
            id=str(values["id"]),
            effect_type=str(values["effect_type"]),
            value1=int(values["value1"]),
            value2=int(values["value2"]),
            effect_count=int(values["effect_count"]),
            effect_turn=int(values["effect_turn"]),
            status_enchant_id=str(values["status_enchant_id"]),
            chain_effect_id=str(values["chain_effect_id"]),
            search_id=str(payload["produceCardSearchId"]),
            move_position_type=str(payload["movePositionType"]),
            pick_range_type=str(payload["pickRangeType"]),
            pick_count_reference_search_id=str(
                payload["pickCountReferenceProduceCardSearchId"]
            ),
            pick_count_type=str(payload["pickCountType"]),
            pick_count_min=int(payload["pickCountMin"]),
            pick_count_max=int(payload["pickCountMax"]),
            search_id2=str(payload["produceCardSearchId2"]),
            pick_range_type2=str(payload["pickRangeType2"]),
            pick_count_reference_search_id2=str(
                payload["pickCountReferenceProduceCardSearchId2"]
            ),
            pick_count_type2=str(payload["pickCountType2"]),
            pick_count_min2=int(payload["pickCountMin2"]),
            pick_count_max2=int(payload["pickCountMax2"]),
            chain_effect_ids=tuple(payload["chainProduceExamEffectIds"]),
            card_status_enchant_id=str(payload["produceCardStatusEnchantId"]),
            card_grow_effect_ids=tuple(payload["produceCardGrowEffectIds"]),
            effect_group_ids=tuple(payload["effectGroupIds"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "effect-master-row-shape"
        ) from error
    return row


def _validate_effect_row(
    effect_row: Mapping[str, object] | sqlite3.Row,
) -> CardSearchEffectPlayCountBuffEffectRow:
    try:
        values = dict(effect_row)
    except (TypeError, ValueError) as error:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "invalid-master-row"
        ) from error
    if values.get("id") != EFFECT_ID:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "unknown-effect-id", str(values.get("id", ""))
        )
    raw_value = values.get("raw_json")
    if not isinstance(raw_value, str):
        raise CardSearchEffectPlayCountBuffResolutionError(
            "missing-master-raw-json"
        )
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "invalid-master-raw-json"
        ) from error
    if not isinstance(payload, Mapping) or payload.get("id") != EFFECT_ID:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "master-id-mismatch"
        )
    row = _effect_row_values(values, payload)
    for raw_name, expected_value in _NEUTRAL_MASTER_RAW_FIELDS.items():
        if not _strict_equal(payload.get(raw_name), expected_value):
            raise CardSearchEffectPlayCountBuffResolutionError(
                "master-structural-mismatch", raw_name
            )
    for raw_name, field_name in _MASTER_RAW_FIELDS.items():
        expected_value = getattr(row, field_name)
        if isinstance(expected_value, tuple):
            expected_value = list(expected_value)
        if not _strict_equal(payload.get(raw_name), expected_value):
            raise CardSearchEffectPlayCountBuffResolutionError(
                "master-structural-mismatch", raw_name
            )
    for column, expected_value in (
        ("id", EFFECT_ID),
        ("effect_type", EFFECT_TYPE),
        ("value1", 1),
        ("value2", 0),
        ("effect_count", 1),
        ("effect_turn", -1),
        ("status_enchant_id", ""),
        ("chain_effect_id", ""),
    ):
        if not _strict_equal(values.get(column), expected_value):
            raise CardSearchEffectPlayCountBuffResolutionError(
                "effect-master-column-mismatch", column
            )
    if row != EXACT_EFFECT_ROW:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "effect-master-row-mismatch", row.id
        )
    return row


def _search_shape_reasons(rule: ProduceCardSearchRule) -> tuple[str, ...]:
    if not isinstance(rule, ProduceCardSearchRule) or rule.id != SEARCH_ID:
        return ("search-id",)
    # Reuse the shared exact Playing predicate.  This effect is the one
    # audited exception where the native row has four explicit rarities.
    mismatches = exact_playing_card_search_mismatches(
        rule,
        expected_categories=(),
        expected_effect_group_ids=(),
    )
    reasons = [
        f"search-shape:{field}"
        for field in mismatches
        if field != "card_rarities"
    ]
    if rule.card_rarities != SEARCH_RARITIES:
        reasons.append("search-shape:card_rarities")
    return tuple(dict.fromkeys(reasons))


def contract_for_card_search_effect_play_count_buff(
    effect_id: str = EFFECT_ID,
    *,
    database: Path = DEFAULT_DATABASE,
) -> CardSearchEffectPlayCountBuffContract:
    if effect_id != EFFECT_ID:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "unknown-effect-id", str(effect_id)
        )
    try:
        search = load_produce_card_search(SEARCH_ID, Path(database))
    except (KeyError, ValueError, sqlite3.Error) as error:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "search-load-failed", SEARCH_ID
        ) from error
    reasons = _search_shape_reasons(search)
    if reasons:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "search-shape", ",".join(reasons)
        )
    return CardSearchEffectPlayCountBuffContract(EXACT_EFFECT_ROW, search)


def resolve_card_search_effect_play_count_buff_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
) -> CardSearchEffectPlayCountBuffContract:
    _validate_effect_row(effect_row)
    return contract_for_card_search_effect_play_count_buff(database=database)


def load_card_search_effect_play_count_buff_contract(
    database: Path = DEFAULT_DATABASE,
) -> CardSearchEffectPlayCountBuffContract:
    rows = load_master_card_search_effect_play_count_buff_rows(database)
    target = next((row for row in rows if row.get("id") == EFFECT_ID), None)
    if target is None:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "effect-master-row-missing", EFFECT_ID
        )
    return resolve_card_search_effect_play_count_buff_contract(
        target, database=database
    )


def try_resolve_card_search_effect_play_count_buff_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
) -> CardSearchEffectPlayCountBuffContract | CardSearchEffectPlayCountBuffUnresolvedInput:
    try:
        return resolve_card_search_effect_play_count_buff_contract(
            effect_row, database=database
        )
    except CardSearchEffectPlayCountBuffResolutionError as error:
        return CardSearchEffectPlayCountBuffUnresolvedInput(
            error.code, error.detail or error.code
        )


@dataclass(frozen=True, slots=True)
class MasterCardShape:
    id: str
    upgrade: int
    plan_type: str
    category: str
    stamina_cost: int
    cost_type: str
    cost_value: int
    move_position_type: str


@dataclass(frozen=True, slots=True)
class CardPlayEffectSlot:
    effect_id: str
    is_once_play_effect: bool

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id:
            raise CardSearchEffectPlayCountBuffResolutionError(
                "card-effect-id"
            )
        if not isinstance(self.is_once_play_effect, bool):
            raise CardSearchEffectPlayCountBuffResolutionError(
                "card-effect-once-shape", self.effect_id
            )


@dataclass(frozen=True, slots=True)
class CardSearchBuffCardMaster:
    card: MasterCardShape
    rarity: str
    effect_slots: tuple[CardPlayEffectSlot, ...]


def load_card_search_buff_card_master(
    card: Plan3NativeCard,
    database: Path = DEFAULT_DATABASE,
) -> CardSearchBuffCardMaster:
    """Load the current typed card/effect list used by the native card command."""

    if not isinstance(card, Plan3NativeCard):
        raise CardSearchEffectPlayCountBuffInputError("invalid-card")
    if card.runtime_customize_ids or card.runtime_grow_effect_ids:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "runtime-card-effect-list-unavailable", card.guid
        )
    try:
        native_master = load_force_play_target_card_master(card, Path(database))
        with sqlite3.connect(Path(database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT id, upgrade_count, plan_type, category, stamina, "
                "cost_type, cost_value, move_position_type, raw_json, "
                "play_effects_json FROM card "
                "WHERE id = ? AND upgrade_count = ?",
                (card.card_id, card.effective_upgrade),
            ).fetchone()
    except (KeyError, ValueError, sqlite3.Error) as error:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "card-master-load-failed", f"{card.card_id}+{card.effective_upgrade}"
        ) from error
    if row is None:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "card-master-missing", f"{card.card_id}+{card.effective_upgrade}"
        )
    try:
        raw = json.loads(str(row["raw_json"]))
        entries = json.loads(str(row["play_effects_json"]))
    except json.JSONDecodeError as error:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "card-master-json", f"{card.card_id}+{card.effective_upgrade}"
        ) from error
    if not isinstance(raw, Mapping) or not isinstance(raw.get("rarity"), str):
        raise CardSearchEffectPlayCountBuffResolutionError(
            "card-master-rarity-shape", card.card_id
        )
    if not isinstance(entries, list) or len(entries) != len(native_master.play_effect_ids):
        raise CardSearchEffectPlayCountBuffResolutionError(
            "card-master-effect-list-shape", card.card_id
        )
    try:
        card_shape = MasterCardShape(
            id=str(row["id"]),
            upgrade=int(row["upgrade_count"]),
            plan_type=str(row["plan_type"]),
            category=str(row["category"]),
            stamina_cost=int(row["stamina"]),
            cost_type=str(row["cost_type"]),
            cost_value=int(row["cost_value"]),
            move_position_type=str(row["move_position_type"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "card-master-column-shape", card.card_id
        ) from error
    slots: list[CardPlayEffectSlot] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise CardSearchEffectPlayCountBuffResolutionError(
                "card-master-effect-entry", f"{card.card_id}#{index}"
            )
        effect_id = entry.get("produceExamEffectId")
        trigger_id = entry.get("produceExamTriggerId")
        once = entry.get("isOncePlayEffect")
        if (
            not isinstance(effect_id, str)
            or not isinstance(trigger_id, str)
            or not isinstance(once, bool)
            or effect_id != native_master.play_effect_ids[index]
        ):
            raise CardSearchEffectPlayCountBuffResolutionError(
                "card-master-effect-entry", f"{card.card_id}#{index}"
            )
        slots.append(CardPlayEffectSlot(effect_id, once))
    if tuple(slot.effect_id for slot in slots) != native_master.play_effect_ids:
        raise CardSearchEffectPlayCountBuffResolutionError(
            "card-master-effect-order", card.card_id
        )
    return CardSearchBuffCardMaster(card_shape, str(raw["rarity"]), tuple(slots))


def _affected_card_shape_errors(master: CardSearchBuffCardMaster) -> tuple[str, ...]:
    card = master.card
    errors: list[str] = []
    if card.id != CARD_ID:
        errors.append("card-id")
    if card.upgrade not in CARD_UPGRADES:
        errors.append("card-upgrade")
    if (
        card.plan_type,
        card.category,
        card.stamina_cost,
        card.cost_type,
        card.cost_value,
        card.move_position_type,
    ) != (PLAN_TYPE_PLAN3, CATEGORY_MENTAL_SKILL, 0, COST_UNKNOWN, 0, MOVE_LOST):
        errors.append("card-shape")
    if master.rarity != RARITY_SSR:
        errors.append("card-rarity")
    if tuple(slot.effect_id for slot in master.effect_slots) != (
        ORDERED_AFFECTED_CARD_EFFECT_IDS
    ):
        errors.append("card-effect-order")
    if any(slot.is_once_play_effect for slot in master.effect_slots):
        errors.append("card-once-shape")
    return tuple(errors)


def matches_affected_card_master(master: object) -> bool:
    return isinstance(master, CardSearchBuffCardMaster) and not _affected_card_shape_errors(
        master
    )


def matches_affected_card_version(
    card: Plan3NativeCard,
    database: Path = DEFAULT_DATABASE,
) -> bool:
    if not isinstance(card, Plan3NativeCard):
        return False
    if (card.card_id, card.effective_upgrade) not in AFFECTED_CARD_VERSIONS:
        return False
    try:
        master = load_card_search_buff_card_master(card, database)
    except CardSearchEffectPlayCountBuffError:
        return False
    return matches_affected_card_master(master)


def load_affected_card_versions(
    database: Path = DEFAULT_DATABASE,
) -> tuple[CardSearchBuffCardMaster, ...]:
    result: list[CardSearchBuffCardMaster] = []
    for upgrade in CARD_UPGRADES:
        card = Plan3NativeCard(CARD_ID, CARD_ID, upgrade, 0, upgrade)
        master = load_card_search_buff_card_master(card, database)
        errors = _affected_card_shape_errors(master)
        if errors:
            raise CardSearchEffectPlayCountBuffResolutionError(
                "affected-card-shape", f"{CARD_ID}+{upgrade}:{','.join(errors)}"
            )
        result.append(master)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CardSearchBuffMatch:
    playing_guid: str
    card: Plan3NativeCard | None
    master: CardSearchBuffCardMaster | None
    matched: bool
    unresolved: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved


def match_playing_card_search(
    state: Plan3NativeState,
    playing_guid: str,
    contract: CardSearchEffectPlayCountBuffContract,
    *,
    database: Path = DEFAULT_DATABASE,
) -> CardSearchBuffMatch:
    if not isinstance(state, Plan3NativeState):
        raise CardSearchEffectPlayCountBuffInputError("invalid-native-state")
    if not isinstance(playing_guid, str) or not playing_guid:
        raise CardSearchEffectPlayCountBuffInputError("invalid-playing-guid")
    if not isinstance(contract, CardSearchEffectPlayCountBuffContract):
        raise CardSearchEffectPlayCountBuffInputError("invalid-contract")
    if not contract.executable:
        return CardSearchBuffMatch(playing_guid, None, None, False, ("contract-shape",))
    if sum(card.guid == playing_guid for card in state.hand) != 1:
        return CardSearchBuffMatch(
            playing_guid, None, None, False, ("playing-card-not-in-hand",)
        )
    try:
        card = state.card_by_guid(playing_guid)
        master = load_card_search_buff_card_master(card, database)
    except Exception as error:
        code, detail = _resolution_detail(error)
        return CardSearchBuffMatch(playing_guid, None, None, False, (f"{code}:{detail}",))
    matched = master.rarity in contract.search.card_rarities
    return CardSearchBuffMatch(playing_guid, card, master, matched)


@dataclass(frozen=True, slots=True)
class PlayCountBuffStatus:
    """One native ``PlayCountBuffStatusEffect`` layer.

    ``limit`` is the executor's value1/play-count increment.  ``remaining_count``
    is the status constructor's effectCount and is spent once per accepted
    matching card.  ``native_uid`` is an observed identity supplied by a core
    adapter when available; this module never invents one.
    """

    limit: int
    remaining_count: int
    search_id: str = SEARCH_ID
    turn: int = -1
    native_uid: int = 0
    is_passing_turn_start: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 0 <= self.limit <= INT32_MAX
        ):
            raise CardSearchEffectPlayCountBuffInputError("invalid-status-limit")
        if (
            isinstance(self.remaining_count, bool)
            or not isinstance(self.remaining_count, int)
            or not 0 <= self.remaining_count <= INT32_MAX
        ):
            raise CardSearchEffectPlayCountBuffInputError(
                "invalid-status-count"
            )
        if not isinstance(self.search_id, str) or not self.search_id:
            raise CardSearchEffectPlayCountBuffInputError("invalid-status-search")
        if (
            isinstance(self.turn, bool)
            or not isinstance(self.turn, int)
            or self.turn < -1
        ):
            raise CardSearchEffectPlayCountBuffInputError("invalid-status-turn")
        if (
            isinstance(self.native_uid, bool)
            or not isinstance(self.native_uid, int)
            or self.native_uid < 0
        ):
            raise CardSearchEffectPlayCountBuffInputError("invalid-status-uid")
        if not isinstance(self.is_passing_turn_start, bool):
            raise CardSearchEffectPlayCountBuffInputError(
                "invalid-status-passing-turn-start"
            )


@dataclass(frozen=True, slots=True)
class PlayCountBuffRuntime:
    statuses: tuple[PlayCountBuffStatus, ...] = ()

    def __post_init__(self) -> None:
        statuses = tuple(self.statuses)
        if not all(isinstance(status, PlayCountBuffStatus) for status in statuses):
            raise CardSearchEffectPlayCountBuffInputError("invalid-status-runtime")
        object.__setattr__(self, "statuses", statuses)


@dataclass(frozen=True, slots=True)
class PlayCountBuffStatusInstall:
    before: PlayCountBuffRuntime
    after: PlayCountBuffRuntime
    status: PlayCountBuffStatus
    accepted: bool
    merged: bool
    existing_matching_status_count: int
    rejected_reason: str = ""

    @property
    def state_unchanged(self) -> bool:
        return self.before is self.after


def install_card_search_effect_play_count_buff(
    runtime: PlayCountBuffRuntime,
    contract: CardSearchEffectPlayCountBuffContract,
    *,
    native_status_add_blocked: bool,
) -> PlayCountBuffStatusInstall:
    """Model ``TryAddPlayCountBuffStatus`` without manufacturing identity.

    Native ``TryAdd`` has no PlayCount-specific merge path: after the common
    ``IsBlockAddStatus`` gate it constructs and calls generic ``AddStatus``.
    Therefore an existing same-search layer is appended, not merged.  A true
    common block gate rejects the add and leaves the tuple untouched.
    """

    if not isinstance(runtime, PlayCountBuffRuntime):
        raise CardSearchEffectPlayCountBuffInputError("invalid-status-runtime")
    if not isinstance(contract, CardSearchEffectPlayCountBuffContract):
        raise CardSearchEffectPlayCountBuffInputError("invalid-contract")
    blocked = _strict_bool(native_status_add_blocked, "native_status_add_blocked")
    if not contract.executable:
        raise CardSearchEffectPlayCountBuffResolutionError("contract-shape")
    row = contract.row
    status = PlayCountBuffStatus(
        limit=row.value1,
        remaining_count=row.effect_count,
        search_id=contract.search.id,
        turn=row.effect_turn,
    )
    existing = sum(
        item.search_id == status.search_id for item in runtime.statuses
    )
    if blocked:
        return PlayCountBuffStatusInstall(
            runtime,
            runtime,
            status,
            accepted=False,
            merged=False,
            existing_matching_status_count=existing,
            rejected_reason="native-status-add-blocked",
        )
    return PlayCountBuffStatusInstall(
        runtime,
        PlayCountBuffRuntime((*runtime.statuses, status)),
        status,
        accepted=True,
        merged=False,
        existing_matching_status_count=existing,
    )


@dataclass(frozen=True, slots=True)
class PlayCountBuffUse:
    before: PlayCountBuffRuntime
    after: PlayCountBuffRuntime
    selected_status_index: int | None
    repeat_count: int
    unresolved: tuple[str, ...] = ()

    @property
    def consumed(self) -> bool:
        return self.selected_status_index is not None and not self.unresolved


@dataclass(frozen=True, slots=True)
class PlayCountBuffTurnStartTransition:
    before: PlayCountBuffRuntime
    after: PlayCountBuffRuntime
    fresh_status_uids: tuple[int, ...] = ()
    spent_status_uids: tuple[int, ...] = ()
    expired_status_uids: tuple[int, ...] = ()
    permanent_status_uids: tuple[int, ...] = ()


def use_matching_play_count_buff(
    runtime: PlayCountBuffRuntime,
    *,
    matching_search_ids: Sequence[str],
) -> PlayCountBuffUse:
    """Consume the newest active layer matching the Playing card.

    This is the generic native ``UsePlayCountBuff`` boundary.  The caller
    compiles the Playing-card predicates from Master and supplies the exact
    matching search IDs; this function owns only active-list order and
    SpendCount.  Finite-turn layers remain usable until their TurnStart
    lifetime expires.
    """

    if not isinstance(runtime, PlayCountBuffRuntime):
        raise CardSearchEffectPlayCountBuffInputError("invalid-status-runtime")
    search_ids = tuple(matching_search_ids)
    if any(not isinstance(value, str) or not value for value in search_ids):
        raise CardSearchEffectPlayCountBuffInputError("invalid-status-search")
    if len(set(search_ids)) != len(search_ids):
        raise CardSearchEffectPlayCountBuffInputError(
            "duplicate-matching-status-search"
        )
    selected_index: int | None = None
    for index in range(len(runtime.statuses) - 1, -1, -1):
        status = runtime.statuses[index]
        if status.search_id not in search_ids or status.remaining_count <= 0:
            continue
        if status.limit <= 0:
            return PlayCountBuffUse(
                runtime, runtime, None, 0, ("status-limit-zero",)
            )
        selected_index = index
        break
    if selected_index is None:
        return PlayCountBuffUse(runtime, runtime, None, 0)
    status = runtime.statuses[selected_index]
    spent = replace(status, remaining_count=status.remaining_count - 1)
    after_statuses = (
        (
            *runtime.statuses[:selected_index],
            *runtime.statuses[selected_index + 1 :],
        )
        if spent.remaining_count == 0
        else (
            *runtime.statuses[:selected_index],
            spent,
            *runtime.statuses[selected_index + 1 :],
        )
    )
    return PlayCountBuffUse(
        runtime,
        PlayCountBuffRuntime(after_statuses),
        selected_index,
        status.limit,
    )


def advance_play_count_buff_turn_start(
    runtime: PlayCountBuffRuntime,
) -> PlayCountBuffTurnStartTransition:
    """Spend finite PlayCountBuff lifetimes at native TurnStart.

    A newly installed layer survives its first TurnStart and becomes passing;
    subsequent TurnStarts decrement its signed turn count.  Permanent layers
    remain active.  SpendCount removal is owned separately by
    :func:`use_matching_play_count_buff`.
    """

    if not isinstance(runtime, PlayCountBuffRuntime):
        raise CardSearchEffectPlayCountBuffInputError("invalid-status-runtime")
    after_statuses: list[PlayCountBuffStatus] = []
    fresh: list[int] = []
    spent: list[int] = []
    expired: list[int] = []
    permanent: list[int] = []
    for status in runtime.statuses:
        if status.turn == -1:
            after_statuses.append(
                replace(status, is_passing_turn_start=True)
            )
            permanent.append(status.native_uid)
            continue
        if not status.is_passing_turn_start:
            if status.turn <= 0:
                expired.append(status.native_uid)
                continue
            after_statuses.append(
                replace(status, is_passing_turn_start=True)
            )
            fresh.append(status.native_uid)
            continue
        next_turn = status.turn - 1
        spent.append(status.native_uid)
        if next_turn <= 0:
            expired.append(status.native_uid)
        else:
            after_statuses.append(replace(status, turn=next_turn))
    return PlayCountBuffTurnStartTransition(
        runtime,
        PlayCountBuffRuntime(tuple(after_statuses)),
        tuple(fresh),
        tuple(spent),
        tuple(expired),
        tuple(permanent),
    )


def get_play_count_buff(
    runtime: PlayCountBuffRuntime,
    *,
    search_id: str = SEARCH_ID,
) -> int:
    """Return native GetPlayCountBuff's matching PlayCount sum."""

    if not isinstance(runtime, PlayCountBuffRuntime):
        raise CardSearchEffectPlayCountBuffInputError("invalid-status-runtime")
    if search_id != SEARCH_ID:
        raise CardSearchEffectPlayCountBuffInputError("invalid-status-search")
    total = 0
    for status in runtime.statuses:
        if status.search_id != search_id or status.remaining_count <= 0:
            continue
        if status.turn != -1:
            raise CardSearchEffectPlayCountBuffResolutionError(
                "status-turn-unsupported", str(status.turn)
            )
        total += status.limit
    return total


def use_play_count_buff(
    runtime: PlayCountBuffRuntime,
    *,
    search_matches: bool,
    search_id: str = SEARCH_ID,
) -> PlayCountBuffUse:
    """Consume the newest matching layer exactly once, like native Use.

    ``UsePlayCountBuff`` selects one indexed matching status, records its UID,
    spends its count, and removes it only when the count reaches zero.  The
    indexed selection is why duplicate layers are not merged on installation.
    """

    if not isinstance(runtime, PlayCountBuffRuntime):
        raise CardSearchEffectPlayCountBuffInputError("invalid-status-runtime")
    if search_id != SEARCH_ID:
        raise CardSearchEffectPlayCountBuffInputError("invalid-status-search")
    matches = _strict_bool(search_matches, "search_matches")
    if not matches:
        return PlayCountBuffUse(runtime, runtime, None, 0)
    selected_index: int | None = None
    for index in range(len(runtime.statuses) - 1, -1, -1):
        status = runtime.statuses[index]
        if status.search_id != search_id or status.remaining_count <= 0:
            continue
        if status.turn != -1:
            return PlayCountBuffUse(
                runtime,
                runtime,
                None,
                0,
                (f"status-turn-unsupported:{status.turn}",),
            )
        if status.limit <= 0:
            return PlayCountBuffUse(
                runtime, runtime, None, 0, ("status-limit-zero",)
            )
        selected_index = index
        break
    if selected_index is None:
        return PlayCountBuffUse(runtime, runtime, None, 0)
    status = runtime.statuses[selected_index]
    spent = replace(status, remaining_count=status.remaining_count - 1)
    if spent.remaining_count == 0:
        after_statuses = (
            *runtime.statuses[:selected_index],
            *runtime.statuses[selected_index + 1 :],
        )
    else:
        after_statuses = (
            *runtime.statuses[:selected_index],
            spent,
            *runtime.statuses[selected_index + 1 :],
        )
    return PlayCountBuffUse(
        runtime,
        PlayCountBuffRuntime(after_statuses),
        selected_index,
        status.limit,
    )


def direct_effect_passes(
    effect_slots: Sequence[CardPlayEffectSlot],
    repeat_count: int,
) -> tuple[tuple[CardPlayEffectSlot, ...], ...]:
    """Expand N additional passes while preserving Master effect order."""

    if isinstance(repeat_count, bool) or not isinstance(repeat_count, int):
        raise CardSearchEffectPlayCountBuffInputError("invalid-repeat-count")
    if repeat_count < 0 or repeat_count >= INT32_MAX:
        raise CardSearchEffectPlayCountBuffInputError("invalid-repeat-count")
    slots = tuple(effect_slots)
    if not all(isinstance(slot, CardPlayEffectSlot) for slot in slots):
        raise CardSearchEffectPlayCountBuffInputError("invalid-effect-slots")
    return tuple(
        slots
        if pass_index == 0
        else tuple(slot for slot in slots if not slot.is_once_play_effect)
        for pass_index in range(repeat_count + 1)
    )


@dataclass(frozen=True, slots=True)
class CardPlayCountBuffHookInput:
    state: Plan3NativeState
    runtime: PlayCountBuffRuntime
    playing_guid: str
    accepted_play: bool = True
    is_playable: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.state, Plan3NativeState):
            raise CardSearchEffectPlayCountBuffInputError("invalid-native-state")
        if not isinstance(self.runtime, PlayCountBuffRuntime):
            raise CardSearchEffectPlayCountBuffInputError("invalid-status-runtime")
        if not isinstance(self.playing_guid, str) or not self.playing_guid:
            raise CardSearchEffectPlayCountBuffInputError("invalid-playing-guid")
        _strict_bool(self.accepted_play, "accepted_play")
        _strict_bool(self.is_playable, "is_playable")


@dataclass(frozen=True, slots=True)
class CardPlayCountBuffExecution:
    before_state: Plan3NativeState
    after_state: Plan3NativeState
    before_runtime: PlayCountBuffRuntime
    after_runtime: PlayCountBuffRuntime
    playing_guid: str
    card: Plan3NativeCard | None
    master: CardSearchBuffCardMaster | None
    accepted_play: bool
    is_playable: bool
    search_matched: bool
    selected_status_index: int | None
    repeat_count: int
    effect_passes: tuple[tuple[CardPlayEffectSlot, ...], ...]
    unresolved: tuple[str, ...] = ()
    order: tuple[str, ...] = NATIVE_CARD_TRANSACTION_ORDER

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def native_state_unchanged(self) -> bool:
        return self.before_state is self.after_state

    @property
    def status_consumed(self) -> bool:
        return self.selected_status_index is not None and self.executable

    @property
    def total_effect_passes(self) -> int:
        return len(self.effect_passes)

    @property
    def core_card_play_count_delta(self) -> int:
        return 1 if self.executable else 0

    @property
    def core_history_event_count(self) -> int:
        return 1 if self.executable else 0

    @property
    def core_cost_payment_count(self) -> int:
        # The payment precedes this hook and is not owned by this module.  The
        # exact affected card has Unknown/0 cost, so its normal command emits
        # no stamina-payment event even though the ordering remains pre-hook.
        if not self.executable or self.master is None:
            return 0
        return int(
            self.master.card.cost_type != COST_UNKNOWN
            and self.master.card.stamina_cost > 0
        )

    @property
    def core_move_count(self) -> int:
        return 1 if self.executable else 0


def _empty_execution(
    hook: CardPlayCountBuffHookInput,
    *,
    unresolved: tuple[str, ...],
) -> CardPlayCountBuffExecution:
    return CardPlayCountBuffExecution(
        before_state=hook.state,
        after_state=hook.state,
        before_runtime=hook.runtime,
        after_runtime=hook.runtime,
        playing_guid=hook.playing_guid,
        card=None,
        master=None,
        accepted_play=hook.accepted_play,
        is_playable=hook.is_playable,
        search_matched=False,
        selected_status_index=None,
        repeat_count=0,
        effect_passes=(),
        unresolved=unresolved,
    )


def execute_card_search_effect_play_count_buff(
    hook: CardPlayCountBuffHookInput,
    *,
    contract: CardSearchEffectPlayCountBuffContract | None = None,
    database: Path = DEFAULT_DATABASE,
) -> CardPlayCountBuffExecution:
    """Run the standalone pre-direct-effect hook without moving the card."""

    if not isinstance(hook, CardPlayCountBuffHookInput):
        raise CardSearchEffectPlayCountBuffInputError("invalid-hook-input")
    if not hook.accepted_play:
        return _empty_execution(hook, unresolved=("play-rejected",))
    if not hook.is_playable:
        return _empty_execution(hook, unresolved=("card-not-playable",))
    try:
        resolved = contract or load_card_search_effect_play_count_buff_contract(
            database
        )
    except CardSearchEffectPlayCountBuffResolutionError as error:
        return _empty_execution(hook, unresolved=(error.code,))
    match = match_playing_card_search(
        hook.state,
        hook.playing_guid,
        resolved,
        database=database,
    )
    if not match.executable:
        return CardPlayCountBuffExecution(
            hook.state,
            hook.state,
            hook.runtime,
            hook.runtime,
            hook.playing_guid,
            match.card,
            match.master,
            hook.accepted_play,
            hook.is_playable,
            False,
            None,
            0,
            (),
            match.unresolved,
        )
    assert match.card is not None and match.master is not None
    try:
        use = use_play_count_buff(
            hook.runtime,
            search_matches=match.matched,
            search_id=resolved.search.id,
        )
        if use.unresolved:
            return CardPlayCountBuffExecution(
                hook.state,
                hook.state,
                hook.runtime,
                hook.runtime,
                hook.playing_guid,
                match.card,
                match.master,
                hook.accepted_play,
                hook.is_playable,
                match.matched,
                None,
                0,
                (),
                use.unresolved,
            )
        passes = direct_effect_passes(match.master.effect_slots, use.repeat_count)
    except CardSearchEffectPlayCountBuffError as error:
        return CardPlayCountBuffExecution(
            hook.state,
            hook.state,
            hook.runtime,
            hook.runtime,
            hook.playing_guid,
            match.card,
            match.master,
            hook.accepted_play,
            hook.is_playable,
            match.matched,
            None,
            0,
            (),
            (error.code,),
        )
    return CardPlayCountBuffExecution(
        before_state=hook.state,
        after_state=hook.state,
        before_runtime=hook.runtime,
        after_runtime=use.after,
        playing_guid=hook.playing_guid,
        card=match.card,
        master=match.master,
        accepted_play=hook.accepted_play,
        is_playable=hook.is_playable,
        search_matched=match.matched,
        selected_status_index=use.selected_status_index,
        repeat_count=use.repeat_count,
        effect_passes=passes,
    )


@dataclass(frozen=True, slots=True)
class CardSearchEffectPlayCountBuffAdapter:
    """Thin typed boundary for a future core hook; no core registration."""

    contract: CardSearchEffectPlayCountBuffContract
    database: Path = DEFAULT_DATABASE

    @classmethod
    def from_master(
        cls, database: Path = DEFAULT_DATABASE
    ) -> "CardSearchEffectPlayCountBuffAdapter":
        return cls(load_card_search_effect_play_count_buff_contract(database), Path(database))

    def execute(self, hook: CardPlayCountBuffHookInput) -> CardPlayCountBuffExecution:
        return execute_card_search_effect_play_count_buff(
            hook, contract=self.contract, database=self.database
        )

    def install_status(
        self,
        runtime: PlayCountBuffRuntime,
        *,
        native_status_add_blocked: bool,
    ) -> PlayCountBuffStatusInstall:
        return install_card_search_effect_play_count_buff(
            runtime,
            self.contract,
            native_status_add_blocked=native_status_add_blocked,
        )


__all__ = [
    "AFFECTED_CARD_VERSION_COUNT",
    "AFFECTED_CARD_VERSIONS",
    "ANDROID_EXECUTE_CARD_COMMAND_IMPL",
    "ANDROID_NATIVE_ADDRESSES",
    "ANDROID_VERSION",
    "CARD_ID",
    "CARD_UPGRADES",
    "CORE_HOOK_REQUIREMENTS",
    "EFFECT_BY_ID",
    "EFFECT_GROUP_ID",
    "EFFECT_ID",
    "EFFECT_TYPE",
    "EFFECT_TYPE_VALUE",
    "EXACT_EFFECT_ROW",
    "FORCE_PLAY_EFFECT_ID",
    "NATIVE_CARD_TRANSACTION_ORDER",
    "ORDERED_AFFECTED_CARD_EFFECT_IDS",
    "CardPlayCountBuffExecution",
    "CardPlayCountBuffHookInput",
    "CardPlayEffectSlot",
    "CardSearchBuffCardMaster",
    "CardSearchBuffMatch",
    "CardSearchEffectPlayCountBuffAdapter",
    "CardSearchEffectPlayCountBuffContract",
    "CardSearchEffectPlayCountBuffEffectRow",
    "CardSearchEffectPlayCountBuffError",
    "CardSearchEffectPlayCountBuffInputError",
    "CardSearchEffectPlayCountBuffResolutionError",
    "CardSearchEffectPlayCountBuffUnresolvedInput",
    "PlayCountBuffRuntime",
    "PlayCountBuffStatus",
    "PlayCountBuffStatusInstall",
    "PlayCountBuffTurnStartTransition",
    "PlayCountBuffUse",
    "MasterCardShape",
    "contract_for_card_search_effect_play_count_buff",
    "advance_play_count_buff_turn_start",
    "direct_effect_passes",
    "execute_card_search_effect_play_count_buff",
    "get_play_count_buff",
    "install_card_search_effect_play_count_buff",
    "load_affected_card_versions",
    "load_card_search_buff_card_master",
    "load_card_search_effect_play_count_buff_contract",
    "load_master_card_search_effect_play_count_buff_rows",
    "match_playing_card_search",
    "matches_affected_card_master",
    "matches_affected_card_version",
    "resolve_card_search_effect_play_count_buff_contract",
    "try_resolve_card_search_effect_play_count_buff_contract",
    "use_play_count_buff",
    "use_matching_play_count_buff",
]
