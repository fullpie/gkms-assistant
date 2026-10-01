"""Exact Master Hold-count + Just Appeal CardPlayAfter trigger contract.

This is the pitem_03-3-078 listener found in a native FullPower replay. It
combines two different predicates: any actual Hold card, and the just-played
Target card's identity. It does not substitute Hand/Playing for either pool.
The existing CardPlayAfter queue and AddGrow executor own its consequences.
"""
from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache

from .card_search import load_produce_card_search
from .master_db import DEFAULT_DATABASE


TRIGGER_ID = "e_trigger-exam_card_play_after-card_search_count_up-1-p_card_search-hold-p_card_search-target-1-p_card-03-act-1_038-0_1"
HOLD_SEARCH_ID = "p_card_search-hold"
TARGET_SEARCH_ID = "p_card_search-target-1-p_card-03-act-1_038"
TARGET_CARD_ID = "p_card-03-act-1_038"
PHASE = "ProduceExamPhaseType_ExamCardPlayAfter"


def _search_shape(search, *, identity, position, cards=(), limit=0):
    expected = {"id": identity, "card_rarities": (), "produce_card_ids": cards, "upgrade_counts": (),
        "plan_type": "ProducePlanType_Unknown", "card_categories": (),
        "card_status_type": "ProduceCardSearchStatusType_Unknown", "order_type": "ProduceCardOrderType_Unknown",
        "card_position_type": position, "card_search_tag": "", "produce_card_random_pool_id": "",
        "limit_count": limit, "stamina_min_max_type": "ConditionMinMaxType_Unknown", "stamina_min": 0,
        "stamina_max": 0, "exam_effect_type": "ProduceExamEffectType_Unknown", "effect_group_ids": (),
        "is_self": False, "produce_card_pool_id": "", "cost_type": "ExamCostType_Unknown", "is_customized": False}
    return asdict(search) == expected


@lru_cache(maxsize=2)
def _searches_match(database, modified_ns, version_fingerprint=None):
    return (_search_shape(load_produce_card_search(HOLD_SEARCH_ID, database), identity=HOLD_SEARCH_ID,
                          position="ProduceCardPositionType_Hold")
            and _search_shape(load_produce_card_search(TARGET_SEARCH_ID, database), identity=TARGET_SEARCH_ID,
                              position="ProduceCardPositionType_Target", cards=(TARGET_CARD_ID,), limit=1))


def matches_trigger_contract(trigger, *, database=DEFAULT_DATABASE):
    if getattr(trigger, "id", None) != TRIGGER_ID:
        return False
    expected = {"id": TRIGGER_ID, "phase_types": (PHASE,), "phase_values": (), "field_check_types": (),
        "field_types": ("ProduceExamFieldStatusType_CardSearchCountUp",), "field_values": (1,),
        "field_card_search_ids": (HOLD_SEARCH_ID,), "produce_card_search_id": TARGET_SEARCH_ID,
        "upper_search_count": 0, "lower_search_count": 1,
        "card_move_position_type": "ProduceCardMovePositionType_Unknown", "effect_types": (),
        "lesson_type": "ProduceStepLessonType_Unknown"}
    if any(getattr(trigger, key, None) != value for key, value in expected.items()):
        return False
    from .version_bound_master import resolve_bound_default_database,bound_master_fingerprint
    database=resolve_bound_default_database(database,default_database=DEFAULT_DATABASE)
    try:
        return _searches_match(database, database.stat().st_mtime_ns,bound_master_fingerprint(database))
    except (KeyError, OSError, TypeError, ValueError):
        return False


def fires(trigger, state, *, event_phase, event_card):
    """Return None for unsupported shape; no histories or guessed counts."""
    if not matches_trigger_contract(trigger):
        return None
    if event_phase != PHASE or event_card is None:
        return False
    if getattr(event_card, "id", None) != TARGET_CARD_ID:
        return False
    held = getattr(state, "hold_pile", None)
    if not isinstance(held, tuple):
        return None
    return len(held) >= 1
