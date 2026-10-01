"""Exact native contract for Plan 3's one current Trouble card, ``眠気``.

Android v3.2.3 treats Trouble as an ordinary card category.  ``IsPlayable``
checks ``IsRestrict`` and an optional play trigger, not the category or the
length of ``PlayProduceExamEffectList``.  ``ExecuteCardCommandImpl`` then
iterates that list and continues to play-count and movement settlement when
the list is empty.  This module encodes that narrow, evidence-backed result
without broadening the central Plan 3 card interpreter.

Only ``p_card-00-acc-0_002#0`` with no runtime upgrade/customization/grow
mutation is accepted.  Other Trouble shapes fail closed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
import json
from pathlib import Path
import sqlite3

from .audition_support_runtime import SupportUpgradeRuntimeInput
from .card_search import ProduceCardSearchRule, load_produce_card_search
from .logic_engine import load_master_card
from .master_db import DEFAULT_DATABASE
from .plan3_engine import MOVE_LOST, Plan3State, load_plan3_card
from .plan3_force_play_card_search import (
    HOLD_ALL_EFFECT_ID,
    ForcePlayPlanResult,
    contract_for_effect,
    plan_force_play_card_search,
)
from .plan3_native_state import (
    MOVE_HOLD,
    Plan3NativeCard,
    Plan3NativeCardMoveTarget,
    Plan3NativeDrawTransition,
    Plan3NativeState,
)


CARD_ID = "p_card-00-acc-0_002"
UPGRADE = 0
CARD_NAME = "眠気"
PLAN_TYPE = "ProducePlanType_Common"
CATEGORY = "ProduceCardCategory_Trouble"
COST_TYPE = "ExamCostType_Unknown"
MOVE_DESTINATION = MOVE_LOST
RARITY = "ProduceCardRarity_N"
MOVE_EFFECT_TRIGGER_UNKNOWN = "ProduceCardMoveEffectTriggerType_Unknown"

SEARCH_EXACT_DECK_GRAVE = "p_card_search-deck_grave-p_card-00-acc-0_002"
SEARCH_TROUBLE_DECK = "p_card_search-trouble-deck-2"
SEARCH_TROUBLE_DECK_ALL = "p_card_search-trouble-deck_all"
SEARCH_TROUBLE_DECK_GRAVE = "p_card_search-trouble-deck_grave"
SEARCH_TROUBLE_HAND = "p_card_search-trouble-hand"
SEARCH_TROUBLE_NOT_LOST = "p_card_search-trouble-not_lost"
SEARCH_TROUBLE_TARGET = "p_card_search-trouble-target"
PROVED_SEARCH_IDS = frozenset(
    {
        SEARCH_EXACT_DECK_GRAVE,
        SEARCH_TROUBLE_DECK,
        SEARCH_TROUBLE_DECK_ALL,
        SEARCH_TROUBLE_DECK_GRAVE,
        SEARCH_TROUBLE_HAND,
        SEARCH_TROUBLE_NOT_LOST,
        SEARCH_TROUBLE_TARGET,
        "p_card_search-hold",
    }
)

ANDROID_VERSION = "Android v3.2.3"
ANDROID_IS_PLAYABLE_RVA = "0x6807248"
ANDROID_IS_SEARCH_TARGET_RVA = "0x68092A4"
ANDROID_MOVE_CARD_RVA = "0x8097808"
ANDROID_DRAW_CARD_RVA = "0x809DE54"
ANDROID_MOVE_PLAY_CARD_RVA = "0x80A1668"

EMPTY_EFFECT_TRANSACTION_ORDER = (
    "is-playable-gate",
    "set-playing-card-same-guid-object",
    "run-zero-direct-effect-commands",
    "card-play-count-increment",
    "user-card-after-check",
    "move-play-card",
    "global-play-count-increment",
    "resolve-playing-card",
    "append-same-guid-card-to-lost",
)

EXTERNAL_CORE_HOOKS = (
    "normal card passive/status/card-status listener snapshot",
    "Playing transient-state ownership",
    "card/global play-count object synchronization and callbacks",
    "difference callback for forced UsePool play",
)


class SleepyTroubleError(ValueError):
    """Fail-closed error with a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True, slots=True)
class SleepyTroubleContract:
    card_id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    stamina_cost: int
    force_stamina_cost: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    play_effect_ids: tuple[str, ...]
    move_position_type: str
    move_effect_trigger_type: str
    move_effect_ids: tuple[str, ...]
    move_trigger_ids: tuple[str, ...]
    produce_card_status_enchant_id: str
    rarity: str
    evaluation: int
    is_end_turn_lost: bool
    is_initial: bool
    is_restrict: bool
    effect_group_ids: tuple[str, ...]
    customize_ids: tuple[str, ...]
    max_customize_count: int

    @property
    def direct_effect_count(self) -> int:
        return len(self.play_effect_ids)

    @property
    def playable_by_static_native_gate(self) -> bool:
        # Exact Master has no trigger, so there is no runtime trigger state to
        # inspect.  Native IsPlayable otherwise only checks IsRestrict.
        return not self.is_restrict and not self.play_trigger_id


_EXPECTED_RAW: Mapping[str, object] = {
    "id": CARD_ID,
    "upgradeCount": UPGRADE,
    "name": CARD_NAME,
    "planType": PLAN_TYPE,
    "category": CATEGORY,
    "stamina": 0,
    "forceStamina": 0,
    "costType": COST_TYPE,
    "costValue": 0,
    "playProduceExamTriggerId": "",
    "playEffects": [],
    "playMovePositionType": MOVE_DESTINATION,
    "moveEffectTriggerType": MOVE_EFFECT_TRIGGER_UNKNOWN,
    "moveProduceExamEffectIds": [],
    "moveProduceExamTriggerIds": [],
    "produceCardStatusEnchantId": "",
    "rarity": RARITY,
    "evaluation": 5,
    "isEndTurnLost": False,
    "isInitialDeckProduceCard": False,
    "isRestrict": False,
    "effectGroupIds": [],
    "produceCardCustomizeIds": [],
    "maxCustomizeCount": 0,
}


def load_sleepy_trouble_contract(
    card_id: str = CARD_ID,
    upgrade: int = UPGRADE,
    database: Path = DEFAULT_DATABASE,
) -> SleepyTroubleContract:
    """Load and validate the one proved Master row and both card loaders."""

    if card_id != CARD_ID or upgrade != UPGRADE:
        raise SleepyTroubleError(
            "unknown-trouble-shape", f"{card_id}+{upgrade}"
        )
    try:
        with sqlite3.connect(Path(database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM card WHERE id = ? AND upgrade_count = ?",
                (card_id, upgrade),
            ).fetchone()
        if row is None:
            raise SleepyTroubleError("master-card-missing", card_id)
        raw = json.loads(row["raw_json"])
    except SleepyTroubleError:
        raise
    except (json.JSONDecodeError, sqlite3.Error, TypeError) as error:
        raise SleepyTroubleError("master-card-load-failed", card_id) from error
    if not isinstance(raw, dict):
        raise SleepyTroubleError("invalid-master-raw-json", card_id)
    for field_name, expected in _EXPECTED_RAW.items():
        if raw.get(field_name) != expected:
            raise SleepyTroubleError("master-shape-mismatch", field_name)
    try:
        raw_effects = json.loads(row["play_effects_json"])
        master = load_master_card(card_id, upgrade, Path(database))
        plan3 = load_plan3_card(card_id, upgrade, Path(database))
    except (json.JSONDecodeError, KeyError, ValueError, sqlite3.Error) as error:
        raise SleepyTroubleError("typed-master-load-failed", card_id) from error
    if raw_effects != [] or master.effects or plan3.effects:
        raise SleepyTroubleError("nonempty-play-effects", card_id)
    typed_values = (
        master.id == plan3.id == card_id,
        master.upgrade == plan3.upgrade == upgrade,
        master.name == plan3.name == CARD_NAME,
        master.plan_type == plan3.plan_type == PLAN_TYPE,
        master.category == plan3.category == CATEGORY,
        master.stamina_cost == plan3.stamina_cost == 0,
        master.force_stamina_cost == plan3.force_stamina_cost == 0,
        master.cost_type == plan3.cost_type == COST_TYPE,
        master.cost_value == plan3.cost_value == 0,
        master.play_trigger_id == "" and plan3.play_trigger is None,
        master.move_position_type == plan3.move_position_type == MOVE_DESTINATION,
        plan3.effect_group_ids == (),
    )
    if not all(typed_values):
        raise SleepyTroubleError("typed-master-shape-mismatch", card_id)
    return SleepyTroubleContract(
        card_id=card_id,
        upgrade=upgrade,
        name=master.name,
        plan_type=master.plan_type,
        category=master.category,
        stamina_cost=master.stamina_cost,
        force_stamina_cost=master.force_stamina_cost,
        cost_type=master.cost_type,
        cost_value=master.cost_value,
        play_trigger_id=master.play_trigger_id,
        play_effect_ids=(),
        move_position_type=master.move_position_type,
        move_effect_trigger_type=master.move_effect_trigger_type,
        move_effect_ids=master.move_effect_ids,
        move_trigger_ids=master.move_trigger_ids,
        produce_card_status_enchant_id=master.produce_card_status_enchant_id,
        rarity=master.rarity,
        evaluation=master.evaluation,
        is_end_turn_lost=master.is_end_turn_lost,
        is_initial=bool(raw["isInitialDeckProduceCard"]),
        is_restrict=bool(raw["isRestrict"]),
        effect_group_ids=tuple(raw["effectGroupIds"]),
        customize_ids=tuple(raw["produceCardCustomizeIds"]),
        max_customize_count=int(raw["maxCustomizeCount"]),
    )


def assert_exact_sleepy_instance(card: Plan3NativeCard) -> None:
    """Reject every runtime shape not covered by the exact empty-list proof."""

    if not isinstance(card, Plan3NativeCard):
        raise TypeError("card must be Plan3NativeCard")
    if card.card_id != CARD_ID:
        raise SleepyTroubleError("unknown-trouble-shape", card.card_id)
    if not (
        card.base_upgrade == card.temporary_upgrade == card.effective_upgrade == 0
        and not card.support_upgrade_ids
        and not card.runtime_grow_effect_ids
        and not card.runtime_customize_ids
        and not card.runtime_customization.effects
        and card.runtime_grow_status is None
        and card.runtime_lesson_add == 0
        and card.runtime_full_power_point_add == 0
        and card.runtime_full_power_point_add_aggregate.value == 0
        and card.runtime_full_power_point_cost_add == 0
        and card.runtime_customize_lesson_add == 0
        and card.runtime_lesson_count_add == 0
        and card.runtime_block_add == 0
        and card.runtime_cost_reduce == 0
        and card.runtime_cost_add == 0
        and card.runtime_cost_penetrate_reduce == 0
        and card.runtime_cost_penetrate_add == 0
    ):
        raise SleepyTroubleError("runtime-trouble-shape-unproved", card.guid)


@dataclass(frozen=True, slots=True)
class SleepyTroubleCapabilities:
    can_hold: bool = True
    can_draw: bool = True
    can_search: bool = True
    can_move: bool = True
    can_select: bool = True
    can_force_play: bool = True
    empty_effects_are_legal: bool = True
    positive_effect_type_search_matches: bool = False
    ordinary_limits_apply: bool = True
    full_power_blocks_hold_add: bool = True


CAPABILITIES = SleepyTroubleCapabilities()


@dataclass(frozen=True, slots=True)
class SleepySearchDecision:
    search_id: str
    matches: bool
    positive_effect_type_filter: bool
    reason: str = ""


def match_sleepy_search_rule(
    search: ProduceCardSearchRule,
    *,
    database: Path = DEFAULT_DATABASE,
) -> SleepySearchDecision:
    """Evaluate proved native filters for the exact empty-effect card.

    A positive effect-type filter is a normal non-match, not an unresolved
    empty effect list.  Rich filters whose semantics are outside this exact
    card snapshot fail closed.
    """

    if not isinstance(search, ProduceCardSearchRule):
        raise TypeError("search must be ProduceCardSearchRule")
    contract = load_sleepy_trouble_contract(database=database)
    unsupported = (
        search.card_status_type != "ProduceCardSearchStatusType_Unknown"
        or search.order_type != "ProduceCardOrderType_Unknown"
        or bool(search.card_search_tag)
        or bool(search.produce_card_random_pool_id)
        or search.stamina_min_max_type != "ConditionMinMaxType_Unknown"
        or search.stamina_min != 0
        or search.stamina_max != 0
        or search.is_self
        or bool(search.produce_card_pool_id)
    )
    if unsupported or search.limit_count < 0:
        raise SleepyTroubleError("unsupported-search-shape", search.id)
    matches = True
    if search.card_rarities and contract.rarity not in search.card_rarities:
        matches = False
    if search.produce_card_ids and CARD_ID not in search.produce_card_ids:
        matches = False
    if search.upgrade_counts and UPGRADE not in search.upgrade_counts:
        matches = False
    if search.plan_type not in ("ProducePlanType_Unknown", PLAN_TYPE):
        matches = False
    if search.card_categories and CATEGORY not in search.card_categories:
        matches = False
    if search.effect_group_ids:
        matches = False
    if search.cost_type not in ("ExamCostType_Unknown", COST_TYPE):
        matches = False
    if search.is_customized:
        matches = False
    positive_effect_filter = (
        search.exam_effect_type != "ProduceExamEffectType_Unknown"
    )
    if positive_effect_filter:
        matches = False
    return SleepySearchDecision(
        search.id,
        matches,
        positive_effect_filter,
        "empty-effects-do-not-contain-requested-effect-type"
        if positive_effect_filter
        else "",
    )


def sleepy_search_candidates(
    state: Plan3NativeState,
    search: ProduceCardSearchRule,
    *,
    playing_guid: str,
    database: Path = DEFAULT_DATABASE,
) -> tuple[Plan3NativeCardMoveTarget, ...]:
    """Build ordered candidates for proved ordinary native collection zones."""

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    decision = match_sleepy_search_rule(search, database=database)
    if not decision.matches:
        return ()
    playing = tuple(
        (index, card)
        for index, card in enumerate(state.hand)
        if card.guid == playing_guid
    )
    if len(playing) != 1:
        raise SleepyTroubleError("played-guid-not-in-hand", playing_guid)
    playing_index, playing_card = playing[0]
    groups = {
        "ProduceCardPositionType_Hand": (
            (
                "hand",
                tuple(
                    (index, card)
                    for index, card in enumerate(state.hand)
                    if card.guid != playing_guid
                ),
            ),
        ),
        "ProduceCardPositionType_Deck": (("draw", tuple(enumerate(state.deck))),),
        "ProduceCardPositionType_DeckAll": (
            ("draw", tuple(enumerate(state.deck))),
        ),
        "ProduceCardPositionType_Grave": (
            ("discard", tuple(enumerate(state.grave))),
        ),
        "ProduceCardPositionType_DeckGrave": (
            ("draw", tuple(enumerate(state.deck))),
            ("discard", tuple(enumerate(state.grave))),
        ),
        "ProduceCardPositionType_Hold": (("hold", tuple(enumerate(state.hold))),),
        "ProduceCardPositionType_NotLost": (
            (
                "hand",
                tuple(
                    (index, card)
                    for index, card in enumerate(state.hand)
                    if card.guid != playing_guid
                ),
            ),
            ("draw", tuple(enumerate(state.deck))),
            ("discard", tuple(enumerate(state.grave))),
            ("hold", tuple(enumerate(state.hold))),
        ),
        "ProduceCardPositionType_Target": (
            ("playing", ((playing_index, playing_card),)),
        ),
        "ProduceCardPositionType_Playing": (
            ("playing", ((playing_index, playing_card),)),
        ),
    }.get(search.card_position_type)
    if groups is None:
        raise SleepyTroubleError(
            "unsupported-search-collection-position", search.card_position_type
        )
    result: list[Plan3NativeCardMoveTarget] = []
    for source_zone, entries in groups:
        for source_index, card in entries:
            if card.card_id != CARD_ID:
                continue
            assert_exact_sleepy_instance(card)
            result.append(
                Plan3NativeCardMoveTarget(
                    card.guid, card, source_zone, source_index
                )
            )
    if search.limit_count > 0:
        result = result[: search.limit_count]
    return tuple(result)


def load_sleepy_search(
    search_id: str, database: Path = DEFAULT_DATABASE
) -> ProduceCardSearchRule:
    if search_id not in PROVED_SEARCH_IDS:
        raise SleepyTroubleError("unproved-search-id", search_id)
    try:
        return load_produce_card_search(search_id, Path(database))
    except (KeyError, ValueError, sqlite3.Error) as error:
        raise SleepyTroubleError("search-load-failed", search_id) from error


def select_sleepy_candidate(
    candidates: Iterable[Plan3NativeCardMoveTarget], guid: str
) -> Plan3NativeCardMoveTarget:
    """Resolve native Select membership by exact GUID, without fallback."""

    ordered = tuple(candidates)
    matches = tuple(value for value in ordered if value.guid == guid)
    if len(matches) != 1:
        raise SleepyTroubleError("selected-guid-not-candidate", guid)
    assert_exact_sleepy_instance(matches[0].card)
    return matches[0]


def move_selected_sleepy(
    state: Plan3NativeState,
    target: Plan3NativeCardMoveTarget,
    move_destination: str,
    *,
    hold_limit: int,
    hand_limit: int,
    is_full_power: bool,
    lesson_type: str,
    support_upgrades: Mapping[str, SupportUpgradeRuntimeInput] | None = None,
    support_card_searches: Mapping[str, ProduceCardSearchRule] | None = None,
) -> Plan3NativeState:
    """Move one selected Trouble GUID through the ordinary CardMove route."""

    assert_exact_sleepy_instance(target.card)
    return state.move_card_targets(
        (target,),
        move_destination,
        hold_limit=hold_limit,
        hand_limit=hand_limit,
        is_full_power=is_full_power,
        lesson_type=lesson_type,
        support_upgrades=support_upgrades or {},
        support_card_searches=support_card_searches or {},
    )


def move_sleepy_hand_to_hold(
    state: Plan3NativeState,
    guid: str,
    *,
    hold_limit: int,
    is_full_power: bool = False,
) -> Plan3NativeState:
    card = state.card_by_guid(guid)
    assert_exact_sleepy_instance(card)
    return state.move_hand_guids_to_hold(
        (guid,), hold_limit=hold_limit, is_full_power=is_full_power
    )


def draw_sleepy_to_hand(
    state: Plan3NativeState,
    *,
    expected_guid: str,
    hand_limit: int,
    lesson_type: str = "ProduceStepLessonType_LessonDance",
) -> Plan3NativeDrawTransition:
    """Draw once by the ordinary Deck/Grave path and require this exact GUID."""

    transition = state.draw_to_hand(
        1,
        hand_limit=hand_limit,
        lesson_type=lesson_type,
        support_upgrades={},
        support_card_searches={},
    )
    if transition.drawn_guids != (expected_guid,):
        raise SleepyTroubleError(
            "expected-sleepy-not-drawn", repr(transition.drawn_guids)
        )
    assert_exact_sleepy_instance(transition.after.card_by_guid(expected_guid))
    return transition


def plan_sleepy_force_play_from_hold(
    state: Plan3NativeState,
    *,
    playing_guid: str,
    sleepy_guid: str,
    database: Path = DEFAULT_DATABASE,
) -> ForcePlayPlanResult:
    """Prove Hold-All queues this empty-effect card as an ordinary UsePool."""

    card = state.card_by_guid(sleepy_guid)
    assert_exact_sleepy_instance(card)
    if not any(value.guid == sleepy_guid for value in state.hold):
        raise SleepyTroubleError("sleepy-guid-not-in-hold", sleepy_guid)
    plan = plan_force_play_card_search(
        state,
        contract_for_effect(HOLD_ALL_EFFECT_ID, Path(database)),
        playing_guid=playing_guid,
        database=Path(database),
    )
    if not plan.resolved:
        raise SleepyTroubleError("force-play-plan-unresolved")
    commands = tuple(command for command in plan.commands if command.guid == sleepy_guid)
    if len(commands) != 1:
        raise SleepyTroubleError("sleepy-force-play-command-missing", sleepy_guid)
    command = commands[0]
    if (
        command.is_consume_cost
        or command.is_use_playable_count
        or command.base_move_position_type != MOVE_DESTINATION
    ):
        raise SleepyTroubleError("sleepy-force-play-command-mismatch", sleepy_guid)
    return plan


class EmptyEffectPlayMode(str, Enum):
    MANUAL_HAND = "manual-hand"
    FORCED_HOLD = "forced-hold"


@dataclass(frozen=True, slots=True)
class EmptyEffectPlayResult:
    mode: EmptyEffectPlayMode
    guid: str
    before_native: Plan3NativeState
    after_native: Plan3NativeState
    before_scalar: Plan3State
    after_scalar: Plan3State
    direct_effect_commands: tuple[()] = ()
    stamina_delta: int = 0
    plays_remaining_delta: int = 0
    card_play_count_delta: int = 1
    global_play_count_delta: int = 1
    not_lost_trouble_count_delta: int = -1
    destination: str = MOVE_DESTINATION
    is_consume_cost: bool = True
    is_use_playable_count: bool = True
    base_transaction_complete: bool = True
    external_core_hooks: tuple[str, ...] = EXTERNAL_CORE_HOOKS
    transaction_order: tuple[str, ...] = EMPTY_EFFECT_TRANSACTION_ORDER


def _project_scalar(
    state: Plan3State,
    native: Plan3NativeState,
    *,
    plays_remaining: int,
) -> Plan3State:
    projection = native.projection()
    return replace(
        state,
        plays_remaining=plays_remaining,
        hand=projection.hand,
        draw_pile=projection.deck,
        discard_pile=projection.grave,
        lost_pile=projection.lost,
        hold_pile=projection.hold,
    )


def execute_empty_effect_play(
    native: Plan3NativeState,
    scalar: Plan3State,
    *,
    guid: str,
    mode: EmptyEffectPlayMode,
    playing_guid: str = "",
    database: Path = DEFAULT_DATABASE,
) -> EmptyEffectPlayResult:
    """Settle the exact zero-effect base transaction to Lost.

    The returned state covers the direct card batch, cost/playable use, card
    instance count, and destination.  External listeners remain explicit core
    hooks; their absence does not make the empty direct-effect list unknown.
    """

    if not isinstance(native, Plan3NativeState):
        raise TypeError("native must be Plan3NativeState")
    if not isinstance(scalar, Plan3State):
        raise TypeError("scalar must be Plan3State")
    if not isinstance(mode, EmptyEffectPlayMode):
        raise SleepyTroubleError("unsupported-play-mode", repr(mode))
    native.assert_plan3_projection(scalar)
    card = native.card_by_guid(guid)
    assert_exact_sleepy_instance(card)
    contract = load_sleepy_trouble_contract(database=database)
    if not contract.playable_by_static_native_gate:
        raise SleepyTroubleError("sleepy-not-playable", guid)

    if mode is EmptyEffectPlayMode.MANUAL_HAND:
        if not any(value.guid == guid for value in native.hand):
            raise SleepyTroubleError("manual-guid-not-in-hand", guid)
        if scalar.plays_remaining < 1:
            raise SleepyTroubleError("no-playable-count", guid)
        after_native = native.play_hand_by_guid(guid, MOVE_DESTINATION)
        plays_after = scalar.plays_remaining - 1
        is_consume_cost = True
        is_use_playable_count = True
        plays_delta = -1
    else:
        plan = plan_sleepy_force_play_from_hold(
            native,
            playing_guid=playing_guid,
            sleepy_guid=guid,
            database=database,
        )
        if tuple(command.guid for command in plan.commands) != (guid,):
            raise SleepyTroubleError("force-play-batch-not-exact-sleepy", guid)
        hold_matches = tuple(
            (index, value)
            for index, value in enumerate(native.hold)
            if value.guid == guid
        )
        if len(hold_matches) != 1:
            raise SleepyTroubleError("sleepy-guid-not-in-hold", guid)
        index, value = hold_matches[0]
        settled = value.increment_play_count().reset_support_upgrade()
        after_native = replace(
            native,
            hold=(*native.hold[:index], *native.hold[index + 1 :]),
            lost=(*native.lost, settled),
        )
        plays_after = scalar.plays_remaining
        is_consume_cost = False
        is_use_playable_count = False
        plays_delta = 0

    after_scalar = _project_scalar(
        scalar, after_native, plays_remaining=plays_after
    )
    if after_scalar.stamina != scalar.stamina:
        raise AssertionError("zero-cost Trouble play changed stamina")
    after_card = after_native.card_by_guid(guid)
    if after_card.play_count != card.play_count + 1:
        raise AssertionError("Trouble play count did not increment once")
    if not any(value.guid == guid for value in after_native.lost):
        raise AssertionError("Trouble card did not settle to Lost")
    return EmptyEffectPlayResult(
        mode=mode,
        guid=guid,
        before_native=native,
        after_native=after_native,
        before_scalar=scalar,
        after_scalar=after_scalar,
        plays_remaining_delta=plays_delta,
        is_consume_cost=is_consume_cost,
        is_use_playable_count=is_use_playable_count,
    )


__all__ = [
    "ANDROID_DRAW_CARD_RVA",
    "ANDROID_IS_PLAYABLE_RVA",
    "ANDROID_IS_SEARCH_TARGET_RVA",
    "ANDROID_MOVE_CARD_RVA",
    "ANDROID_MOVE_PLAY_CARD_RVA",
    "ANDROID_VERSION",
    "CAPABILITIES",
    "CARD_ID",
    "CARD_NAME",
    "CATEGORY",
    "EMPTY_EFFECT_TRANSACTION_ORDER",
    "EXTERNAL_CORE_HOOKS",
    "EmptyEffectPlayMode",
    "EmptyEffectPlayResult",
    "MOVE_DESTINATION",
    "PROVED_SEARCH_IDS",
    "SEARCH_EXACT_DECK_GRAVE",
    "SEARCH_TROUBLE_DECK_GRAVE",
    "SleepySearchDecision",
    "SleepyTroubleCapabilities",
    "SleepyTroubleContract",
    "SleepyTroubleError",
    "assert_exact_sleepy_instance",
    "draw_sleepy_to_hand",
    "execute_empty_effect_play",
    "load_sleepy_search",
    "load_sleepy_trouble_contract",
    "match_sleepy_search_rule",
    "move_selected_sleepy",
    "move_sleepy_hand_to_hold",
    "plan_sleepy_force_play_from_hold",
    "select_sleepy_candidate",
    "sleepy_search_candidates",
]
