"""Standalone Android v3.2.3 ``ExamForcePlayCardSearch`` planner.

The native effect executor does not synchronously play a selected card.  It
builds one ``UsePool`` command per selected GUID and appends one
``CardForcePlay`` difference after each command.  The ordinary command runner
later validates ``IsPlayable``, removes the card's then-current GUID position,
sets the card as Playing, executes the normal card batch, and settles it.

This module implements the complete effect-executor boundary for the two
shapes reachable from the five currently affected Plan 3 card versions:

* DeckGrave + Select + exactly one explicit GUID;
* Hold + All, where native ignores the nominal 0..0 pick count.

It deliberately leaves the later arbitrary-card transaction behind an
explicit core hook.  Unknown Master rows, richer searches, missing selection,
runtime-mutated target effect lists, and invalid GUID selections fail closed
without consuming RNG or mutating :class:`Plan3NativeState`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any

from .card_search import (
    ProduceCardSearchRule,
    load_produce_card_search,
    validate_plan3_card_move_search,
)
from .master_db import DEFAULT_DATABASE
from .plan3_native_state import (
    Plan3NativeCard,
    Plan3NativeCardMoveTarget,
    Plan3NativeState,
    Plan3NativeStateError,
)


EFFECT_TYPE = "ProduceExamEffectType_ExamForcePlayCardSearch"
EFFECT_TYPE_VALUE = 24
DIFFERENCE_TYPE = "CardForcePlay"
DIFFERENCE_TYPE_VALUE = 14

PICK_SELECT = "ProducePickRangeType_Select"
PICK_RANDOM = "ProducePickRangeType_Random"
PICK_ALL = "ProducePickRangeType_All"
PICK_COUNT_UNKNOWN = "ProducePickCountType_Unknown"
PICK_RANGE_UNKNOWN = "ProducePickRangeType_Unknown"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
MOVE_HAND = "ProduceCardMovePositionType_Hand"
POSITION_DECK_GRAVE = "ProduceCardPositionType_DeckGrave"
POSITION_HOLD = "ProduceCardPositionType_Hold"

SEARCH_DECK_GRAVE = "p_card_search-deck_grave"
SEARCH_HOLD = "p_card_search-hold"

DECK_GRAVE_SELECT_EFFECT_ID = (
    "e_effect-exam_force_play_card_search-p_card_search-deck_grave-select-1_1"
)
HOLD_ALL_EFFECT_ID = (
    "e_effect-exam_force_play_card_search-p_card_search-hold-all-0_0"
)

ANDROID_VERSION = "Android v3.2.3"
ANDROID_EXECUTOR_CTOR = "0x7E7A170"
ANDROID_EXECUTOR_EXECUTE = "0x7E7A480"
ANDROID_PICK_IMPL = "0x7E59CA4"
ANDROID_RECURSION_PREDICATE = "0x7E7AC54"
ANDROID_CREATE_USE_POOL = "0x7EC3458"
ANDROID_SET_FORCE_DIFFERENCE = "0x7E57420"


class ForcePlayCardSearchError(ValueError):
    """Base fail-closed error with a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class ForcePlayCardSearchResolutionError(ForcePlayCardSearchError):
    """A static Master or target-card shape is not proved."""


class ForcePlayCardSearchInputError(ForcePlayCardSearchError):
    """A runtime execution input is malformed."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ForcePlayCardSearchInputError("invalid-text", label)
    return value


def _text_tuple(values: Sequence[object], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ForcePlayCardSearchInputError("invalid-text-sequence", label)
    result = tuple(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise ForcePlayCardSearchInputError("invalid-text-sequence", label)
    return result


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ForcePlayCardSearchInputError("invalid-nonnegative-int", label)
    return value


@dataclass(frozen=True, slots=True)
class ForcePlayEffectRow:
    """Every execution-relevant column of one ForcePlay Master row."""

    effect_id: str
    source_slot: int
    search_id: str
    pick_range_type: str
    pick_count_min: int
    pick_count_max: int
    move_position_type: str = MOVE_UNKNOWN
    pick_count_reference_search_id: str = ""
    pick_count_type: str = PICK_COUNT_UNKNOWN
    search_id2: str = ""
    pick_range_type2: str = PICK_RANGE_UNKNOWN
    pick_count_reference_search_id2: str = ""
    pick_count_type2: str = PICK_COUNT_UNKNOWN
    pick_count_min2: int = 0
    pick_count_max2: int = 0
    effect_value1: int = 0
    effect_value2: int = 0
    effect_count: int = 0
    effect_turn: int = 0
    chain_effect_id: str = ""
    chain_effect_ids: tuple[str, ...] = ()
    status_enchant_id: str = ""
    card_status_enchant_id: str = ""
    card_grow_effect_ids: tuple[str, ...] = ()
    effect_group_ids: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        _nonnegative_int(self.source_slot, "source_slot")
        _text(self.search_id, "search_id")
        for name in (
            "pick_count_min",
            "pick_count_max",
            "pick_count_min2",
            "pick_count_max2",
            "effect_value1",
            "effect_value2",
            "effect_count",
            "effect_turn",
        ):
            _nonnegative_int(getattr(self, name), name)
        for name in (
            "chain_effect_ids",
            "card_grow_effect_ids",
            "effect_group_ids",
        ):
            object.__setattr__(self, name, _text_tuple(getattr(self, name), name))
        object.__setattr__(self, "unresolved_reasons", _effect_shape_errors(self))

    @property
    def standalone_executable(self) -> bool:
        return not self.unresolved_reasons

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _effect_shape_errors(row: ForcePlayEffectRow) -> tuple[str, ...]:
    common_invalid = (
        row.pick_count_reference_search_id
        or row.pick_count_type != PICK_COUNT_UNKNOWN
        or row.search_id2
        or row.pick_range_type2 != PICK_RANGE_UNKNOWN
        or row.pick_count_reference_search_id2
        or row.pick_count_type2 != PICK_COUNT_UNKNOWN
        or row.pick_count_min2 != 0
        or row.pick_count_max2 != 0
        or row.effect_value1 != 0
        or row.effect_value2 != 0
        or row.effect_count != 0
        or row.effect_turn != 0
        or row.chain_effect_id
        or row.chain_effect_ids
        or row.status_enchant_id
        or row.card_status_enchant_id
        or row.card_grow_effect_ids
        or row.effect_group_ids
    )
    if common_invalid:
        return ("non-neutral-effect-payload",)
    if (
        row.search_id == SEARCH_DECK_GRAVE
        and row.pick_range_type == PICK_SELECT
        and row.pick_count_min == row.pick_count_max == 1
        and row.move_position_type == MOVE_UNKNOWN
    ):
        return ()
    if (
        row.search_id == SEARCH_HOLD
        and row.pick_range_type == PICK_ALL
        and row.pick_count_min == row.pick_count_max == 0
        and row.move_position_type == MOVE_UNKNOWN
    ):
        return ()
    if row.pick_range_type == PICK_RANDOM:
        return ("random-shape-outside-affected-slice",)
    return ("search-pick-shape-outside-affected-slice",)


# Source order from the local ProduceExamEffect snapshot.
ALL_EFFECT_ROWS: tuple[ForcePlayEffectRow, ...] = (
    ForcePlayEffectRow(
        "e_effect-exam_force_play_card_search-p_card_search-active_skill-lost-random-1_1",
        0,
        "p_card_search-active_skill-lost",
        PICK_RANDOM,
        1,
        1,
    ),
    ForcePlayEffectRow(
        DECK_GRAVE_SELECT_EFFECT_ID,
        1,
        SEARCH_DECK_GRAVE,
        PICK_SELECT,
        1,
        1,
    ),
    ForcePlayEffectRow(
        HOLD_ALL_EFFECT_ID,
        2,
        SEARCH_HOLD,
        PICK_ALL,
        0,
        0,
    ),
    ForcePlayEffectRow(
        "e_effect-exam_force_play_card_search-p_card_search-mental_skill-sr-random-hand-1-all-0_0",
        3,
        "p_card_search-mental_skill-sr-random-hand-1",
        PICK_ALL,
        0,
        0,
    ),
    ForcePlayEffectRow(
        "e_effect-exam_force_play_card_search-p_card_search-r-random-hand-2-all-0_0",
        4,
        "p_card_search-r-random-hand-2",
        PICK_ALL,
        0,
        0,
    ),
    ForcePlayEffectRow(
        "e_effect-exam_force_play_card_search-p_card_search-random-random_pool-p_random_pool-rush_idol_unique_set-ssr-upgrade_1-1-hand-random-1_1",
        5,
        "p_card_search-random-random_pool-p_random_pool-rush_idol_unique_set-ssr-upgrade_1-1",
        PICK_RANDOM,
        1,
        1,
        move_position_type=MOVE_HAND,
    ),
    ForcePlayEffectRow(
        "e_effect-exam_force_play_card_search-p_card_search-random-random_pool-p_random_pool-rush-ssr-upgrade_1-1-hand-random-1_1",
        6,
        "p_card_search-random-random_pool-p_random_pool-rush-ssr-upgrade_1-1",
        PICK_RANDOM,
        1,
        1,
        move_position_type=MOVE_HAND,
    ),
    ForcePlayEffectRow(
        "e_effect-exam_force_play_card_search-p_card_search-target_is_self-exam_status_enchant_encore-all-1_1",
        7,
        "p_card_search-target_is_self-exam_status_enchant_encore",
        PICK_ALL,
        1,
        1,
    ),
)
EFFECT_BY_ID: Mapping[str, ForcePlayEffectRow] = MappingProxyType(
    {row.effect_id: row for row in ALL_EFFECT_ROWS}
)
EXECUTABLE_EFFECT_ROWS = tuple(
    row for row in ALL_EFFECT_ROWS if row.standalone_executable
)


@dataclass(frozen=True, slots=True)
class AffectedCardVersion:
    card_id: str
    upgrade: int
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    ordered_effect_ids: tuple[str, ...]
    force_effect_slot: int

    def __post_init__(self) -> None:
        _text(self.card_id, "card_id")
        _nonnegative_int(self.upgrade, "upgrade")
        _nonnegative_int(self.stamina, "stamina")
        _nonnegative_int(self.cost_value, "cost_value")
        _nonnegative_int(self.force_effect_slot, "force_effect_slot")
        object.__setattr__(
            self,
            "ordered_effect_ids",
            _text_tuple(self.ordered_effect_ids, "ordered_effect_ids"),
        )
        if (
            self.force_effect_slot >= len(self.ordered_effect_ids)
            or self.ordered_effect_ids[self.force_effect_slot] not in EFFECT_BY_ID
        ):
            raise ForcePlayCardSearchInputError("invalid-force-effect-slot")

    @property
    def force_effect_id(self) -> str:
        return self.ordered_effect_ids[self.force_effect_slot]


_NEO_EFFECTS = (
    "e_effect-exam_playable_value_add-01",
    "e_effect-exam_card_search_effect_play_count_buff-0001-01-inf-p_card_search-n-r-sr-ssr-playing-all-0_0",
    DECK_GRAVE_SELECT_EFFECT_ID,
)
_HOLD_EFFECTS = (
    "e_effect-exam_add_grow_effect-p_card_search-hold-all-0_0-g_effect-lesson_add-5",
    HOLD_ALL_EFFECT_ID,
    "e_effect-exam_playable_value_add-01",
    "e_effect-exam_effect_timer-0001-01-e_effect-exam_preservation-0001",
)
AFFECTED_CARD_VERSIONS: tuple[AffectedCardVersion, ...] = tuple(
    AffectedCardVersion(
        "p_card-03-ido-3_193",
        upgrade,
        "ProducePlanType_Plan3",
        "ProduceCardCategory_MentalSkill",
        0,
        "ExamCostType_Unknown",
        0,
        "",
        "ProduceCardMovePositionType_Lost",
        _NEO_EFFECTS,
        2,
    )
    for upgrade in range(4)
) + (
    AffectedCardVersion(
        "p_card-03-men-100_015",
        0,
        "ProducePlanType_Plan3",
        "ProduceCardCategory_MentalSkill",
        3,
        "ExamCostType_Unknown",
        0,
        "e_trigger-none-stance_change_count_up-4",
        "ProduceCardMovePositionType_Grave",
        _HOLD_EFFECTS,
        1,
    ),
)


@dataclass(frozen=True, slots=True)
class ForcePlayEffectContract:
    row: ForcePlayEffectRow
    search: ProduceCardSearchRule

    def __post_init__(self) -> None:
        if not isinstance(self.row, ForcePlayEffectRow):
            raise ForcePlayCardSearchInputError("invalid-effect-row")
        if not isinstance(self.search, ProduceCardSearchRule):
            raise ForcePlayCardSearchInputError("invalid-search-rule")
        if self.search.id != self.row.search_id:
            raise ForcePlayCardSearchInputError("effect-search-id-mismatch")

    @property
    def standalone_executable(self) -> bool:
        return self.row.standalone_executable

    @property
    def unresolved_reasons(self) -> tuple[str, ...]:
        reasons = list(self.row.unresolved_reasons)
        search_reason = validate_plan3_card_move_search(self.search)
        if self.row.standalone_executable and search_reason is not None:
            reasons.append(search_reason)
        if self.row.search_id == SEARCH_DECK_GRAVE:
            if self.search.card_position_type != POSITION_DECK_GRAVE:
                reasons.append("deck-grave-position-mismatch")
        elif self.row.search_id == SEARCH_HOLD:
            if self.search.card_position_type != POSITION_HOLD:
                reasons.append("hold-position-mismatch")
        if self.row.standalone_executable and self.search.limit_count != 0:
            reasons.append("affected-search-limit")
        return tuple(dict.fromkeys(reasons))

    @property
    def executable(self) -> bool:
        return not self.unresolved_reasons


def contract_for_effect(
    effect_id: str, database: Path = DEFAULT_DATABASE
) -> ForcePlayEffectContract:
    """Load the shared card-search primitive for one exact catalog row."""

    row = EFFECT_BY_ID.get(effect_id)
    if row is None:
        raise ForcePlayCardSearchResolutionError("unknown-effect-id", effect_id)
    try:
        search = load_produce_card_search(row.search_id, Path(database))
    except (KeyError, ValueError, sqlite3.Error) as error:
        raise ForcePlayCardSearchResolutionError(
            "search-load-failed", row.search_id
        ) from error
    return ForcePlayEffectContract(row, search)


def load_master_force_play_rows(
    database: Path = DEFAULT_DATABASE,
) -> tuple[dict[str, object], ...]:
    with sqlite3.connect(Path(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM effect WHERE effect_type = ? ORDER BY rowid",
            (EFFECT_TYPE,),
        ).fetchall()
    return tuple(dict(row) for row in rows)


_MASTER_RAW_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "effectType": "effect_type",
        "effectValue1": "effect_value1",
        "effectValue2": "effect_value2",
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

_NEUTRAL_MASTER_RAW_FIELDS: Mapping[str, object] = MappingProxyType(
    {
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": "ProduceExamEffectType_Unknown",
    }
)


def resolve_force_play_card_search_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
) -> ForcePlayEffectContract:
    """Resolve only an exact current Master row; structural drift is fatal."""

    try:
        values = dict(effect_row)
    except (TypeError, ValueError) as error:
        raise ForcePlayCardSearchResolutionError("invalid-master-row") from error
    effect_id = values.get("id")
    if not isinstance(effect_id, str) or not effect_id:
        raise ForcePlayCardSearchResolutionError("missing-effect-id")
    expected = EFFECT_BY_ID.get(effect_id)
    if expected is None:
        raise ForcePlayCardSearchResolutionError("unknown-effect-id", effect_id)
    raw = values.get("raw_json")
    if not isinstance(raw, str):
        raise ForcePlayCardSearchResolutionError("missing-master-raw-json")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ForcePlayCardSearchResolutionError("invalid-master-raw-json") from error
    if not isinstance(payload, Mapping):
        raise ForcePlayCardSearchResolutionError("invalid-master-raw-json")
    if payload.get("id") != effect_id:
        raise ForcePlayCardSearchResolutionError("master-id-mismatch")
    for raw_name, expected_value in _NEUTRAL_MASTER_RAW_FIELDS.items():
        if payload.get(raw_name) != expected_value:
            raise ForcePlayCardSearchResolutionError(
                "master-structural-mismatch", raw_name
            )
    for raw_name, field_name in _MASTER_RAW_FIELDS.items():
        expected_value: object = EFFECT_TYPE if raw_name == "effectType" else getattr(
            expected, field_name
        )
        if isinstance(expected_value, tuple):
            expected_value = list(expected_value)
        if payload.get(raw_name) != expected_value:
            raise ForcePlayCardSearchResolutionError(
                "master-structural-mismatch", raw_name
            )
    return contract_for_effect(effect_id, database)


@dataclass(frozen=True, slots=True)
class ForcePlayUnresolvedInput:
    field: str
    reason: str


def try_resolve_force_play_card_search_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
) -> ForcePlayEffectContract | ForcePlayUnresolvedInput:
    try:
        return resolve_force_play_card_search_contract(effect_row, database=database)
    except ForcePlayCardSearchResolutionError as error:
        return ForcePlayUnresolvedInput(error.code, error.detail or error.code)


@dataclass(frozen=True, slots=True)
class ForcePlayTargetCardMaster:
    card_id: str
    upgrade: int
    play_effect_ids: tuple[str, ...]
    play_effect_types: tuple[str, ...]
    base_move_position_type: str

    @property
    def has_recursive_force_play(self) -> bool:
        return EFFECT_TYPE in self.play_effect_types


def load_force_play_target_card_master(
    card: Plan3NativeCard,
    database: Path = DEFAULT_DATABASE,
) -> ForcePlayTargetCardMaster:
    """Load the current effect list used by native's enum-24 recursion guard."""

    if not isinstance(card, Plan3NativeCard):
        raise ForcePlayCardSearchInputError("invalid-target-card")
    # Runtime customization can rewrite the effective play-effect list.  A
    # raw ID tuple is therefore insufficient, but Plan2's canonical ordered
    # contract has already resolved every EffectAdd child.  Accept only that
    # fully bound identity; Plan3/legacy callers without it remain fail-closed.
    customization = card.plan2_runtime_customization
    if (
        card.runtime_customize_ids
        and customization.customize_ids != card.runtime_customize_ids
    ):
        raise ForcePlayCardSearchResolutionError(
            "runtime-target-effect-list-unavailable", card.guid
        )
    with sqlite3.connect(Path(database)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT play_effects_json, move_position_type FROM card "
            "WHERE id = ? AND upgrade_count = ?",
            (card.card_id, card.effective_upgrade),
        ).fetchone()
        if row is None:
            raise ForcePlayCardSearchResolutionError(
                "target-card-master-missing",
                f"{card.card_id}+{card.effective_upgrade}",
            )
        try:
            raw_effects = json.loads(row["play_effects_json"])
        except json.JSONDecodeError as error:
            raise ForcePlayCardSearchResolutionError(
                "target-card-effects-invalid", card.card_id
            ) from error
        if not isinstance(raw_effects, list):
            raise ForcePlayCardSearchResolutionError(
                "target-card-effects-invalid", card.card_id
            )
        effect_ids: list[str] = []
        for item in raw_effects:
            if not isinstance(item, Mapping):
                raise ForcePlayCardSearchResolutionError(
                    "target-card-effects-invalid", card.card_id
                )
            effect_id = item.get("produceExamEffectId")
            if not isinstance(effect_id, str) or not effect_id:
                raise ForcePlayCardSearchResolutionError(
                    "target-card-effects-invalid", card.card_id
                )
            effect_ids.append(effect_id)
        effect_types: list[str] = []
        for effect_id in effect_ids:
            effect = connection.execute(
                "SELECT effect_type FROM effect WHERE id = ?", (effect_id,)
            ).fetchone()
            if effect is None:
                raise ForcePlayCardSearchResolutionError(
                    "target-effect-master-missing", effect_id
                )
            effect_types.append(str(effect["effect_type"]))
        for entry in customization.effects:
            added = entry.added_effect
            if added is None:
                continue
            effect_ids.append(added.id)
            effect_types.append(added.effect_type)
    return ForcePlayTargetCardMaster(
        card.card_id,
        card.effective_upgrade,
        tuple(effect_ids),
        tuple(effect_types),
        str(row["move_position_type"]),
    )


@dataclass(frozen=True, slots=True)
class ForcePlayExecutionInput:
    selected_guids: tuple[str, ...] = ()
    enchant_effect_uid: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selected_guids",
            _text_tuple(self.selected_guids, "selected_guids"),
        )
        _nonnegative_int(self.enchant_effect_uid, "enchant_effect_uid")


FORCED_USE_POOL_TRANSACTION_ORDER: tuple[str, ...] = (
    "remove-current-use-pool-command",
    "bypass-cost-validation-because-is-consume-cost-false",
    "is-playable-gate",
    "invalid-calls-on-play-card-invalid-and-builds-no-card-batch",
    "resolve-current-source-position-by-command-card-guid",
    "remove-source-card",
    "check-insert-existing-difference-commands",
    "build-card-batch-with-is-use-playable-count-false",
    "separate-activity-start",
    "set-playing-card-same-guid-object",
    "increment-card-play-phase-counts-and-build-normal-card-listeners",
    "build-ordered-direct-card-effects",
    "user-card-after-check",
    "move-play-card",
    "card-play-count-callsite-and-global-play-count-increment",
    "resolve-playing-card",
    "move-to-live-card-destination",
    "grave-move-listeners-when-applicable",
    "difference-callback",
    "separate-activity-end-before-appended-post-play-listeners",
)

CORE_HOOK_REQUIREMENTS: tuple[str, ...] = (
    "live IsPlayable evaluation",
    "normal card passive/status/card-status listener snapshot",
    "ordered direct-effect execution with recursive difference insertion",
    "Playing transient-state ownership",
    "runtime PlayMovePositionType and FullPower Hold rejection",
    "card/global play-count object synchronization and callbacks",
)


@dataclass(frozen=True, slots=True)
class ForcePlayQueuedCommand:
    ordinal: int
    guid: str
    card: Plan3NativeCard
    original_source_zone: str
    original_source_position_type: str
    original_source_index: int
    base_move_position_type: str
    enchant_effect_uid: int
    command_type: int = 4
    is_consume_cost: bool = False
    is_manual: bool = False
    is_use_playable_count: bool = False
    resolves_source_by_guid_at_execution: bool = True
    core_hook_requirements: tuple[str, ...] = CORE_HOOK_REQUIREMENTS
    transaction_order: tuple[str, ...] = FORCED_USE_POOL_TRANSACTION_ORDER

    def __post_init__(self) -> None:
        _nonnegative_int(self.ordinal, "ordinal")
        _text(self.guid, "guid")
        if self.guid != self.card.guid:
            raise ForcePlayCardSearchInputError("command-guid-card-mismatch")
        _nonnegative_int(self.original_source_index, "original_source_index")
        _nonnegative_int(self.enchant_effect_uid, "enchant_effect_uid")

    @property
    def cost_delta(self) -> int:
        return 0

    @property
    def plays_remaining_delta(self) -> int:
        return 0

    @property
    def global_play_count_delta_after_move(self) -> int:
        return 1

    @property
    def updates_play_count_status_listeners(self) -> bool:
        return False

    @property
    def card_play_count_native_callsite_count(self) -> int:
        # Native has a build-side call and a resolved-card MovePlayCard call.
        # Their object-copy synchronization is deliberately left to core.
        return 2


@dataclass(frozen=True, slots=True)
class ForcePlayDifference:
    ordinal: int
    guid: str
    difference_type: str = DIFFERENCE_TYPE
    difference_type_value: int = DIFFERENCE_TYPE_VALUE
    appended_after_matching_command: bool = True


@dataclass(frozen=True, slots=True)
class ForcePlayResolvedBranch:
    candidate_guids: tuple[str, ...]
    eligible_guids: tuple[str, ...]
    recursive_excluded_guids: tuple[str, ...]
    selected_guids: tuple[str, ...]
    selection_mode: str


@dataclass(frozen=True, slots=True)
class ForcePlayUnresolvedBranch:
    reason: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ForcePlayTrace:
    effect_id: str
    random_state_before: int
    random_state_after: int
    rng_consumed: bool
    operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ForcePlayPlanResult:
    before: Plan3NativeState
    after: Plan3NativeState
    contract: ForcePlayEffectContract
    execution_input: ForcePlayExecutionInput
    branch: ForcePlayResolvedBranch | ForcePlayUnresolvedBranch
    commands: tuple[ForcePlayQueuedCommand, ...]
    differences: tuple[ForcePlayDifference, ...]
    trace: ForcePlayTrace

    @property
    def resolved(self) -> bool:
        return isinstance(self.branch, ForcePlayResolvedBranch)

    @property
    def unresolved(self) -> bool:
        return not self.resolved

    @property
    def core_hook_required(self) -> bool:
        return bool(self.commands)

    def __post_init__(self) -> None:
        if not isinstance(self.before, Plan3NativeState) or not isinstance(
            self.after, Plan3NativeState
        ):
            raise ForcePlayCardSearchInputError("invalid-native-state")
        # AffectEffect only queues future commands and differences.  Zone
        # mutation belongs to the later UsePool runner.
        if self.after is not self.before:
            raise ForcePlayCardSearchInputError("effect-planner-mutated-state")
        if self.unresolved and (self.commands or self.differences):
            raise ForcePlayCardSearchInputError("unresolved-result-has-output")
        if len(self.commands) != len(self.differences):
            raise ForcePlayCardSearchInputError("command-difference-count-mismatch")
        for command, difference in zip(self.commands, self.differences, strict=True):
            if command.ordinal != difference.ordinal or command.guid != difference.guid:
                raise ForcePlayCardSearchInputError("command-difference-order-mismatch")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _unresolved_result(
    state: Plan3NativeState,
    contract: ForcePlayEffectContract,
    supplied: ForcePlayExecutionInput,
    reason: str,
    detail: str = "",
) -> ForcePlayPlanResult:
    return ForcePlayPlanResult(
        state,
        state,
        contract,
        supplied,
        ForcePlayUnresolvedBranch(reason, detail),
        (),
        (),
        ForcePlayTrace(
            contract.row.effect_id,
            state.random_state,
            state.random_state,
            False,
            ("fail-closed-before-queue",),
        ),
    )


_SOURCE_POSITION_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "draw": "ProduceCardPositionType_Deck",
        "discard": "ProduceCardPositionType_Grave",
        "hold": "ProduceCardPositionType_Hold",
    }
)


def plan_force_play_card_search(
    state: Plan3NativeState,
    contract: ForcePlayEffectContract,
    *,
    playing_guid: str,
    execution_input: ForcePlayExecutionInput | None = None,
    database: Path = DEFAULT_DATABASE,
) -> ForcePlayPlanResult:
    """Build exact future UsePool commands; never execute arbitrary cards."""

    if not isinstance(state, Plan3NativeState):
        raise ForcePlayCardSearchInputError("state-must-be-plan3-native-state")
    if not isinstance(contract, ForcePlayEffectContract):
        raise ForcePlayCardSearchInputError("invalid-contract")
    supplied = execution_input or ForcePlayExecutionInput()
    if not isinstance(supplied, ForcePlayExecutionInput):
        raise ForcePlayCardSearchInputError("invalid-execution-input")
    _text(playing_guid, "playing_guid")
    if not contract.executable:
        return _unresolved_result(
            state,
            contract,
            supplied,
            "force-play-contract-unresolved",
            contract.unresolved_reasons[0],
        )
    try:
        candidates = state.card_move_candidates(
            contract.search, playing_guid=playing_guid
        )
    except Plan3NativeStateError as error:
        return _unresolved_result(state, contract, supplied, error.code, error.detail)

    masters: dict[str, ForcePlayTargetCardMaster] = {}
    try:
        for candidate in candidates:
            masters[candidate.guid] = load_force_play_target_card_master(
                candidate.card, database
            )
    except ForcePlayCardSearchResolutionError as error:
        return _unresolved_result(
            state, contract, supplied, error.code, error.detail
        )
    eligible = tuple(
        candidate
        for candidate in candidates
        if not masters[candidate.guid].has_recursive_force_play
    )
    excluded = tuple(
        candidate.guid
        for candidate in candidates
        if masters[candidate.guid].has_recursive_force_play
    )

    selected: tuple[Plan3NativeCardMoveTarget, ...]
    selection_mode: str
    if contract.row.pick_range_type == PICK_SELECT:
        selection_mode = "explicit-target-index-membership"
        if not eligible and not supplied.selected_guids:
            selected = ()
        elif len(supplied.selected_guids) != 1:
            return _unresolved_result(
                state,
                contract,
                supplied,
                "select-count-mismatch",
                f"expected=1:actual={len(supplied.selected_guids)}",
            )
        elif len(set(supplied.selected_guids)) != 1:
            return _unresolved_result(
                state, contract, supplied, "duplicate-selected-guid"
            )
        else:
            selected_set = set(supplied.selected_guids)
            selected = tuple(
                candidate for candidate in eligible if candidate.guid in selected_set
            )
            if len(selected) != 1:
                return _unresolved_result(
                    state,
                    contract,
                    supplied,
                    "selected-guid-not-eligible",
                    supplied.selected_guids[0],
                )
    elif contract.row.pick_range_type == PICK_ALL:
        selection_mode = "all-ignores-pick-count"
        if supplied.selected_guids:
            return _unresolved_result(
                state, contract, supplied, "all-shape-rejects-explicit-selection"
            )
        selected = eligible
    else:
        return _unresolved_result(
            state,
            contract,
            supplied,
            "unsupported-pick-range",
            contract.row.pick_range_type,
        )

    commands: list[ForcePlayQueuedCommand] = []
    differences: list[ForcePlayDifference] = []
    operations: list[str] = [
        "build-candidates-in-native-zone-order",
        "filter-card-when-any-current-play-effect-has-type-24",
        selection_mode,
    ]
    for ordinal, target in enumerate(selected):
        position_type = _SOURCE_POSITION_TYPES.get(target.source_zone)
        if position_type is None:
            return _unresolved_result(
                state,
                contract,
                supplied,
                "unsupported-force-play-source-zone",
                target.source_zone,
            )
        commands.append(
            ForcePlayQueuedCommand(
                ordinal,
                target.guid,
                target.card,
                target.source_zone,
                position_type,
                target.source_index,
                masters[target.guid].base_move_position_type,
                supplied.enchant_effect_uid,
            )
        )
        operations.append(f"queue-use-pool:{target.guid}")
        differences.append(ForcePlayDifference(ordinal, target.guid))
        operations.append(f"append-card-force-play-difference:{target.guid}")
    operations.extend(
        (
            "play-effect-wrapper-check-inserts-difference-results",
            "play-effect-wrapper-runs-post-effect-callback-and-difference-callback",
        )
    )
    branch = ForcePlayResolvedBranch(
        tuple(candidate.guid for candidate in candidates),
        tuple(candidate.guid for candidate in eligible),
        excluded,
        tuple(target.guid for target in selected),
        selection_mode,
    )
    return ForcePlayPlanResult(
        state,
        state,
        contract,
        supplied,
        branch,
        tuple(commands),
        tuple(differences),
        ForcePlayTrace(
            contract.row.effect_id,
            state.random_state,
            state.random_state,
            False,
            tuple(operations),
        ),
    )


apply_force_play_card_search = plan_force_play_card_search
execute_force_play_card_search = plan_force_play_card_search


def catalog_to_dict() -> dict[str, object]:
    return {
        "effect_type": EFFECT_TYPE,
        "effect_type_value": EFFECT_TYPE_VALUE,
        "all_effect_rows": [row.to_dict() for row in ALL_EFFECT_ROWS],
        "executable_effect_ids": [row.effect_id for row in EXECUTABLE_EFFECT_ROWS],
        "affected_card_versions": [asdict(card) for card in AFFECTED_CARD_VERSIONS],
        "native": {
            "version": ANDROID_VERSION,
            "constructor": ANDROID_EXECUTOR_CTOR,
            "execute": ANDROID_EXECUTOR_EXECUTE,
            "pick_impl": ANDROID_PICK_IMPL,
            "recursion_predicate": ANDROID_RECURSION_PREDICATE,
            "create_use_pool": ANDROID_CREATE_USE_POOL,
            "set_force_difference": ANDROID_SET_FORCE_DIFFERENCE,
        },
        "core_hook_requirements": list(CORE_HOOK_REQUIREMENTS),
    }


__all__ = [
    "AFFECTED_CARD_VERSIONS",
    "ALL_EFFECT_ROWS",
    "CORE_HOOK_REQUIREMENTS",
    "DECK_GRAVE_SELECT_EFFECT_ID",
    "DIFFERENCE_TYPE_VALUE",
    "EFFECT_BY_ID",
    "EFFECT_TYPE",
    "EFFECT_TYPE_VALUE",
    "EXECUTABLE_EFFECT_ROWS",
    "FORCED_USE_POOL_TRANSACTION_ORDER",
    "ForcePlayCardSearchError",
    "ForcePlayCardSearchInputError",
    "ForcePlayCardSearchResolutionError",
    "ForcePlayDifference",
    "ForcePlayEffectContract",
    "ForcePlayEffectRow",
    "ForcePlayExecutionInput",
    "ForcePlayPlanResult",
    "ForcePlayQueuedCommand",
    "ForcePlayResolvedBranch",
    "ForcePlayTargetCardMaster",
    "ForcePlayTrace",
    "ForcePlayUnresolvedBranch",
    "ForcePlayUnresolvedInput",
    "HOLD_ALL_EFFECT_ID",
    "apply_force_play_card_search",
    "catalog_to_dict",
    "contract_for_effect",
    "execute_force_play_card_search",
    "load_force_play_target_card_master",
    "load_master_force_play_rows",
    "plan_force_play_card_search",
    "resolve_force_play_card_search_contract",
    "try_resolve_force_play_card_search_contract",
]
