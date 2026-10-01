"""Fail-closed native ProduceCardSearch loading and exact target matching."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .master_db import DEFAULT_DATABASE


_TARGET = "ProduceCardPositionType_Target"
_HAND = "ProduceCardPositionType_Hand"
_DECK = "ProduceCardPositionType_Deck"
_GRAVE = "ProduceCardPositionType_Grave"
_DECK_GRAVE = "ProduceCardPositionType_DeckGrave"
_HOLD = "ProduceCardPositionType_Hold"
_PLAYING = "ProduceCardPositionType_Playing"
_UNKNOWN_PLAN = "ProducePlanType_Unknown"
_UNKNOWN_STATUS = "ProduceCardSearchStatusType_Unknown"
_UNKNOWN_ORDER = "ProduceCardOrderType_Unknown"
_UNKNOWN_MIN_MAX = "ConditionMinMaxType_Unknown"
_UNKNOWN_EXAM_EFFECT = "ProduceExamEffectType_Unknown"
_UNKNOWN_COST = "ExamCostType_Unknown"


@dataclass(frozen=True, slots=True)
class ProduceCardSearchRule:
    id: str
    card_rarities: tuple[str, ...]
    produce_card_ids: tuple[str, ...]
    upgrade_counts: tuple[int, ...]
    plan_type: str
    card_categories: tuple[str, ...]
    card_status_type: str
    order_type: str
    card_position_type: str
    card_search_tag: str
    produce_card_random_pool_id: str
    limit_count: int
    stamina_min_max_type: str
    stamina_min: int
    stamina_max: int
    exam_effect_type: str
    effect_group_ids: tuple[str, ...]
    is_self: bool
    produce_card_pool_id: str
    cost_type: str
    is_customized: bool


def _strings(value: str, label: str) -> tuple[str, ...]:
    raw = json.loads(value)
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"{label} must be a JSON string array")
    return tuple(raw)


def _integers(value: str, label: str) -> tuple[int, ...]:
    raw = json.loads(value)
    if not isinstance(raw, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in raw):
        raise ValueError(f"{label} must be a JSON integer array")
    return tuple(raw)


def load_produce_card_search(
    search_id: str, database: Path = DEFAULT_DATABASE
) -> ProduceCardSearchRule:
    """Load one complete native card-search row; missing rows are not matches."""

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM produce_card_search WHERE id = ?", (search_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"ProduceCardSearch not found: {search_id}")
    return ProduceCardSearchRule(
        id=str(row["id"]),
        card_rarities=_strings(row["card_rarities_json"], "card_rarities"),
        produce_card_ids=_strings(row["produce_card_ids_json"], "produce_card_ids"),
        upgrade_counts=_integers(row["upgrade_counts_json"], "upgrade_counts"),
        plan_type=str(row["plan_type"]),
        card_categories=_strings(row["card_categories_json"], "card_categories"),
        card_status_type=str(row["card_status_type"]),
        order_type=str(row["order_type"]),
        card_position_type=str(row["card_position_type"]),
        card_search_tag=str(row["card_search_tag"]),
        produce_card_random_pool_id=str(row["produce_card_random_pool_id"]),
        limit_count=int(row["limit_count"]),
        stamina_min_max_type=str(row["stamina_min_max_type"]),
        stamina_min=int(row["stamina_min"]),
        stamina_max=int(row["stamina_max"]),
        exam_effect_type=str(row["exam_effect_type"]),
        effect_group_ids=_strings(row["effect_group_ids_json"], "effect_group_ids"),
        is_self=bool(row["is_self"]),
        produce_card_pool_id=str(row["produce_card_pool_id"]),
        cost_type=str(row["cost_type"]),
        is_customized=bool(row["is_customized"]),
    )


def exact_playing_card_search_mismatches(
    rule: ProduceCardSearchRule,
    *,
    expected_categories: tuple[str, ...],
    expected_effect_group_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Return every field outside one proved direct ``Playing`` predicate.

    Android's CardPlay listener receives the already selected card.  For the
    two currently proved searches, the collection position is exactly
    ``Playing`` and every field except category/effect-group membership is
    neutral.  Keeping this validator plan-neutral lets Plan2 and Plan3 share
    the same search grammar instead of maintaining look-alike predicates.
    """

    expected: tuple[tuple[str, object], ...] = (
        ("card_rarities", ()),
        ("produce_card_ids", ()),
        ("upgrade_counts", ()),
        ("plan_type", _UNKNOWN_PLAN),
        ("card_categories", tuple(expected_categories)),
        ("card_status_type", _UNKNOWN_STATUS),
        ("order_type", _UNKNOWN_ORDER),
        ("card_position_type", _PLAYING),
        ("card_search_tag", ""),
        ("produce_card_random_pool_id", ""),
        ("limit_count", 0),
        ("stamina_min_max_type", _UNKNOWN_MIN_MAX),
        ("stamina_min", 0),
        ("stamina_max", 0),
        ("exam_effect_type", _UNKNOWN_EXAM_EFFECT),
        ("effect_group_ids", tuple(expected_effect_group_ids)),
        ("is_self", False),
        ("produce_card_pool_id", ""),
        ("cost_type", _UNKNOWN_COST),
        ("is_customized", False),
    )
    return tuple(
        field_name
        for field_name, expected_value in expected
        if getattr(rule, field_name) != expected_value
    )


def match_exact_playing_card_search(
    rule: ProduceCardSearchRule,
    *,
    card_category: str,
    card_effect_group_ids: tuple[str, ...] = (),
    expected_categories: tuple[str, ...],
    expected_effect_group_ids: tuple[str, ...],
) -> tuple[bool, str | None]:
    """Match one already-bound playing card against the exact search row.

    A non-``None`` reason denotes an unsupported row/input shape.  ``False``
    with no reason is an ordinary proved non-match.
    """

    mismatches = exact_playing_card_search_mismatches(
        rule,
        expected_categories=expected_categories,
        expected_effect_group_ids=expected_effect_group_ids,
    )
    if mismatches:
        return False, f"playing-card-search-shape:{rule.id}:{mismatches[0]}"
    if not isinstance(card_category, str) or not card_category:
        return False, f"playing-card-search-input-category:{rule.id}"
    groups = tuple(card_effect_group_ids)
    if any(not isinstance(group_id, str) or not group_id for group_id in groups):
        return False, f"playing-card-search-input-effect-group:{rule.id}"
    if expected_categories and card_category not in expected_categories:
        return False, None
    if expected_effect_group_ids and not all(
        group_id in groups for group_id in expected_effect_group_ids
    ):
        return False, None
    return True, None


def match_exact_target_card_search(
    rule: ProduceCardSearchRule, card_id: str, upgrade_count: int
) -> tuple[bool, str | None]:
    """Match only Target/IsSearchTargetByKeys-equivalent native searches.

    A false result with a reason means the row's shape is unsupported, rather
    than that the supplied card simply did not match.
    """

    neutral = (
        not rule.card_rarities
        and rule.plan_type == _UNKNOWN_PLAN
        and not rule.card_categories
        and rule.card_status_type == _UNKNOWN_STATUS
        and rule.order_type == _UNKNOWN_ORDER
        and not rule.card_search_tag
        and not rule.produce_card_random_pool_id
        and rule.stamina_min_max_type == _UNKNOWN_MIN_MAX
        and rule.stamina_min == 0
        and rule.stamina_max == 0
        and rule.exam_effect_type == _UNKNOWN_EXAM_EFFECT
        and not rule.effect_group_ids
        and not rule.is_self
        and not rule.produce_card_pool_id
        and rule.cost_type == _UNKNOWN_COST
        and not rule.is_customized
    )
    if not neutral:
        return False, f"card-search-non-neutral:{rule.id}"
    if rule.card_position_type != _TARGET:
        return False, f"card-search-position:{rule.id}"
    # LimitCount controls how many cards a collection search returns.  Native
    # direct-card predicates receive the already played card and only test the
    # ID/upgrade key sets, so both zero (unbounded) and one are exact here.
    if rule.limit_count not in (0, 1):
        return False, f"card-search-limit:{rule.id}"
    if not rule.produce_card_ids or len(set(rule.produce_card_ids)) != len(rule.produce_card_ids):
        return False, f"card-search-ids:{rule.id}"
    if (
        len(set(rule.upgrade_counts)) != len(rule.upgrade_counts)
        or any(value < 0 for value in rule.upgrade_counts)
    ):
        return False, f"card-search-upgrades:{rule.id}"
    if not isinstance(upgrade_count, int) or isinstance(upgrade_count, bool) or upgrade_count < 0:
        return False, f"card-search-input-upgrade:{rule.id}"
    if card_id not in rule.produce_card_ids:
        return False, None
    # Native IsSearchTargetByKeys treats the ID and upgrade filters as two
    # independent membership sets.  upgradeCounts are not positionally paired
    # with produceCardIds.
    if rule.upgrade_counts and upgrade_count not in rule.upgrade_counts:
        return False, None
    return True, None


def match_exact_target_card_context_search(
    rule: ProduceCardSearchRule,
    *,
    card_id: str,
    upgrade_count: int,
    card_category: str,
    card_effect_group_ids: tuple[str, ...] = (),
) -> tuple[bool, str | None]:
    """Match a target-card search with the native card context available.

    Some native CardPlayAfter listeners search the just-played target by
    category (or effect group) instead of enumerating card IDs.  The older
    :func:`match_exact_target_card_search` intentionally accepts only
    ID/upgrade-key rows, so using it for these listeners would turn a valid
    category-only row into ``card-search-non-neutral``.  This companion keeps
    the same fail-closed shape checks while evaluating the additional fields
    against the already-bound card context; it never infers identity from an
    item or replay position.
    """

    if rule.card_position_type != _TARGET:
        return False, f"card-search-position:{rule.id}"
    if (
        rule.card_rarities
        or rule.plan_type != _UNKNOWN_PLAN
        or rule.card_status_type != _UNKNOWN_STATUS
        or rule.order_type != _UNKNOWN_ORDER
        or rule.card_search_tag
        or rule.produce_card_random_pool_id
        or rule.stamina_min_max_type != _UNKNOWN_MIN_MAX
        or rule.stamina_min != 0
        or rule.stamina_max != 0
        or rule.exam_effect_type != _UNKNOWN_EXAM_EFFECT
        or rule.is_self
        or rule.produce_card_pool_id
        or rule.cost_type != _UNKNOWN_COST
        or rule.is_customized
    ):
        return False, f"card-search-non-neutral:{rule.id}"
    if rule.limit_count not in (0, 1):
        return False, f"card-search-limit:{rule.id}"
    for name, values in (
        ("card IDs", rule.produce_card_ids),
        ("upgrade counts", rule.upgrade_counts),
        ("card categories", rule.card_categories),
        ("effect groups", rule.effect_group_ids),
    ):
        if any(not value for value in values) or len(values) != len(set(values)):
            return False, f"card-search-{name.replace(' ', '-')}:{rule.id}"
    if not (
        rule.produce_card_ids
        or rule.upgrade_counts
        or rule.card_categories
        or rule.effect_group_ids
    ):
        return False, f"card-search-target-filters-empty:{rule.id}"
    if (
        not isinstance(card_id, str)
        or not card_id
        or not isinstance(upgrade_count, int)
        or isinstance(upgrade_count, bool)
        or upgrade_count < 0
    ):
        return False, f"card-search-input-card:{rule.id}"
    if rule.produce_card_ids and card_id not in rule.produce_card_ids:
        return False, None
    if rule.upgrade_counts and upgrade_count not in rule.upgrade_counts:
        return False, None
    if rule.card_categories:
        if not isinstance(card_category, str) or not card_category:
            return False, f"card-search-input-category:{rule.id}"
        if card_category not in rule.card_categories:
            return False, None
    if rule.effect_group_ids:
        groups = tuple(card_effect_group_ids)
        if any(not isinstance(group, str) or not group for group in groups):
            return False, f"card-search-input-effect-group:{rule.id}"
        if not all(group in groups for group in rule.effect_group_ids):
            return False, None
    return True, None


def match_support_hand_add_search(
    rule: ProduceCardSearchRule, card_id: str, upgrade_count: int
) -> tuple[bool, str | None]:
    """Match the exact HandAdd predicate shape used by support upgrades.

    Native support rows normally use a Hand-position search with otherwise
    neutral filters.  Some fixtures/rows use the already-proven Target shape;
    route those through :func:`match_exact_target_card_search` rather than
    broadening its semantics.
    """

    if rule.card_position_type != _HAND:
        return match_exact_target_card_search(rule, card_id, upgrade_count)
    neutral = (
        not rule.card_rarities
        and rule.plan_type == _UNKNOWN_PLAN
        and not rule.card_categories
        and rule.card_status_type == _UNKNOWN_STATUS
        and rule.order_type == _UNKNOWN_ORDER
        and not rule.card_search_tag
        and not rule.produce_card_random_pool_id
        and rule.stamina_min_max_type == _UNKNOWN_MIN_MAX
        and rule.stamina_min == 0
        and rule.stamina_max == 0
        and rule.exam_effect_type == _UNKNOWN_EXAM_EFFECT
        and not rule.effect_group_ids
        and not rule.is_self
        and not rule.produce_card_pool_id
        and rule.cost_type == _UNKNOWN_COST
        and not rule.is_customized
        and rule.limit_count in (0, 1)
    )
    if not neutral:
        return False, f"card-search-non-neutral:{rule.id}"
    if len(set(rule.produce_card_ids)) != len(rule.produce_card_ids):
        return False, f"card-search-ids:{rule.id}"
    if (
        len(set(rule.upgrade_counts)) != len(rule.upgrade_counts)
        or any(value < 0 for value in rule.upgrade_counts)
    ):
        return False, f"card-search-upgrades:{rule.id}"
    if (
        not isinstance(upgrade_count, int)
        or isinstance(upgrade_count, bool)
        or upgrade_count < 0
    ):
        return False, f"card-search-input-upgrade:{rule.id}"
    if rule.produce_card_ids and card_id not in rule.produce_card_ids:
        return False, None
    if rule.upgrade_counts and upgrade_count not in rule.upgrade_counts:
        return False, None
    return True, None


PLAN3_CARD_MOVE_SEARCH_POSITIONS = frozenset(
    {_HAND, _DECK, _GRAVE, _DECK_GRAVE, _HOLD, _PLAYING}
)


def match_plan3_card_move_search(
    rule: ProduceCardSearchRule, card_id: str, upgrade_count: int
) -> tuple[bool, str | None]:
    """Match the exact collection-search subset used by Plan 3 CardMove.

    Android builds collection results in their native zone order and then
    applies the ID and upgrade key sets as independent filters.  The Plan 3
    kernel intentionally accepts only fields it can evaluate from the
    GUID-card state; richer rarity/category/status/order searches remain
    fail-closed instead of silently broadening their target set.
    """

    reason = validate_plan3_card_move_search(rule)
    if reason is not None:
        return False, reason
    if (
        not isinstance(upgrade_count, int)
        or isinstance(upgrade_count, bool)
        or upgrade_count < 0
    ):
        return False, f"card-move-search-input-upgrade:{rule.id}"
    if rule.produce_card_ids and card_id not in rule.produce_card_ids:
        return False, None
    if rule.upgrade_counts and upgrade_count not in rule.upgrade_counts:
        return False, None
    return True, None


def validate_plan3_card_move_search(
    rule: ProduceCardSearchRule,
) -> str | None:
    neutral = (
        not rule.card_rarities
        and rule.plan_type == _UNKNOWN_PLAN
        and not rule.card_categories
        and rule.card_status_type == _UNKNOWN_STATUS
        and rule.order_type == _UNKNOWN_ORDER
        and not rule.card_search_tag
        and not rule.produce_card_random_pool_id
        and rule.stamina_min_max_type == _UNKNOWN_MIN_MAX
        and rule.stamina_min == 0
        and rule.stamina_max == 0
        and rule.exam_effect_type == _UNKNOWN_EXAM_EFFECT
        and not rule.effect_group_ids
        and not rule.is_self
        and not rule.produce_card_pool_id
        and rule.cost_type == _UNKNOWN_COST
        and not rule.is_customized
    )
    if not neutral:
        return f"card-move-search-non-neutral:{rule.id}"
    if rule.card_position_type not in PLAN3_CARD_MOVE_SEARCH_POSITIONS:
        return f"card-move-search-position:{rule.id}"
    if rule.limit_count < 0:
        return f"card-move-search-limit:{rule.id}"
    if len(set(rule.produce_card_ids)) != len(rule.produce_card_ids):
        return f"card-move-search-ids:{rule.id}"
    if (
        len(set(rule.upgrade_counts)) != len(rule.upgrade_counts)
        or any(value < 0 for value in rule.upgrade_counts)
    ):
        return f"card-move-search-upgrades:{rule.id}"
    return None


__all__ = [
    "PLAN3_CARD_MOVE_SEARCH_POSITIONS",
    "ProduceCardSearchRule",
    "exact_playing_card_search_mismatches",
    "load_produce_card_search",
    "match_exact_target_card_context_search",
    "match_exact_target_card_search",
    "match_exact_playing_card_search",
    "match_plan3_card_move_search",
    "match_support_hand_add_search",
    "validate_plan3_card_move_search",
]
