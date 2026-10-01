"""Exact StartPlay Deck/Grave idol-unique return through native zone owners."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import lru_cache
import json
from pathlib import Path
import sqlite3

from .card_search import load_produce_card_search, validate_plan3_card_move_search
from .master_db import DEFAULT_DATABASE


EFFECT_ID = "e_effect-exam_card_move-p_card_search-deck_grave-idol-unique-1-hand-all-0_0"
SEARCH_ID = "p_card_search-deck_grave-idol-unique-1"
DESTINATION = "ProduceCardMovePositionType_Hand"


def matches_effect(effect):
    if (getattr(effect, "id", None) != EFFECT_ID or effect.effect_type != "ProduceExamEffectType_ExamCardMove"
            or any((effect.value1, effect.value2, effect.effect_count, effect.effect_turn, effect.status_enchant_id,
                    effect.status_enchant, effect.chain_effect_id, effect.chain_effect_ids, effect.chain_effect,
                    effect.trigger, effect.once)) or effect.card_move_rule is None):
        return False
    expected = {"target_card_id": "", "target_upgrade": 0, "target_effect_type": "ProduceExamEffectType_Unknown",
        "search_id": SEARCH_ID, "destination": DESTINATION, "pick_range_type": "ProducePickRangeType_All",
        "pick_reference_search_id": "", "pick_count_type": "ProducePickCountType_Unknown", "pick_count_min": 0,
        "pick_count_max": 0, "second_search_id": "", "second_pick_range_type": "ProducePickRangeType_Unknown",
        "second_pick_reference_search_id": "", "second_pick_count_type": "ProducePickCountType_Unknown",
        "second_pick_count_min": 0, "second_pick_count_max": 0, "chain_effect_ids": (),
        "card_status_enchant_id": "", "card_grow_effect_ids": (), "effect_group_ids": ()}
    return asdict(effect.card_move_rule) == expected


@lru_cache(maxsize=4096)
def _search_tag(card_id, upgrade, database, modified_ns):
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT raw_json FROM card WHERE id=? AND upgrade_count=?", (card_id, upgrade)).fetchone()
    if row is None:
        raise ValueError(f"idol-return card is absent from Master:{card_id}@{upgrade}")
    return json.loads(row[0])["searchTag"]


@dataclass(frozen=True)
class IdolReturnTrace:
    effect_id: str
    target_guids: tuple[str, ...]
    source_zones: tuple[str, ...]
    hand_before: tuple[str, ...]
    hand_after: tuple[str, ...]
    random_before: int
    random_after: int


def return_idol_to_hand(native, effect, *, hand_limit, hold_limit, is_full_power,
                        lesson_type, support_upgrades=(), support_card_searches=None, database=DEFAULT_DATABASE):
    """Take the first actual tag match in Deck then Grave; never infer by ID prefix."""
    from .plan3_native_state import Plan3NativeCardMoveTarget, Plan3NativeStateError
    if not matches_effect(effect):
        raise Plan3NativeStateError("runtime-startplay-idol-return-effect-unresolved", getattr(effect, "id", ""))
    database = Path(database)
    search = load_produce_card_search(SEARCH_ID, database)
    if (search.card_position_type != "ProduceCardPositionType_DeckGrave" or search.card_search_tag != "idol-unique"
            or search.limit_count != 1 or search.produce_card_ids or search.upgrade_counts
            or validate_plan3_card_move_search(replace(search, card_search_tag="")) is not None):
        raise Plan3NativeStateError("runtime-startplay-idol-return-search-unresolved", SEARCH_ID)
    modified = database.stat().st_mtime_ns
    candidates = tuple(Plan3NativeCardMoveTarget(card.guid, card, source_zone, index)
        for source_zone, cards in (("draw", native.deck), ("discard", native.grave))
        for index, card in enumerate(cards)
        if _search_tag(card.card_id, card.effective_upgrade, database, modified) == search.card_search_tag)
    selected = candidates[:search.limit_count]
    after = native.move_card_targets(selected, DESTINATION, hand_limit=hand_limit, hold_limit=hold_limit,
        is_full_power=is_full_power, lesson_type=lesson_type, support_upgrades=support_upgrades,
        support_card_searches={} if support_card_searches is None else support_card_searches)
    return after, IdolReturnTrace(EFFECT_ID, tuple(card.guid for card in selected), tuple(card.source_zone for card in selected),
        tuple(card.guid for card in native.hand), tuple(card.guid for card in after.hand), native.random_state, after.random_state)
