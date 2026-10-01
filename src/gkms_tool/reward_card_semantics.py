"""Pure candidate-pool semantics shared by Produce reward consumers.

Reward-card exclusion is keyed by the canonical ``card_id`` only.  The
candidate object is never copied or rewritten, so order, upgrade, weight, and
caller-owned metadata remain unchanged for candidates that survive.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TypeVar


CandidateT = TypeVar("CandidateT")


def normalize_excluded_reward_card_ids(
    excluded_reward_card_ids: Iterable[str],
) -> frozenset[str]:
    """Return deterministic card-id membership for a run's exclusion list.

    ``RunShadowState.excluded_reward_card_ids`` is already unique, but this
    boundary also accepts replay/test input with repeated IDs.  A string is
    rejected as a collection because treating it as characters would silently
    create the wrong exclusion set.
    """

    if isinstance(excluded_reward_card_ids, (str, bytes)):
        raise TypeError("excluded_reward_card_ids must be an iterable of card IDs")
    normalized: set[str] = set()
    for card_id in excluded_reward_card_ids:
        if not isinstance(card_id, str) or not card_id:
            raise ValueError("excluded_reward_card_ids must contain card IDs")
        normalized.add(card_id)
    return frozenset(normalized)


def _default_card_id(candidate: CandidateT) -> str:
    if isinstance(candidate, Mapping):
        card_id = candidate.get("card_id")
    else:
        card_id = getattr(candidate, "card_id", None)
    if not isinstance(card_id, str) or not card_id:
        raise ValueError("candidate must expose a non-empty card_id")
    return card_id


def filter_excluded_reward_card_candidates(
    candidates: Iterable[CandidateT],
    excluded_reward_card_ids: Iterable[str] = (),
    *,
    card_id_getter: Callable[[CandidateT], str] | None = None,
) -> tuple[CandidateT, ...]:
    """Filter candidates by ``card_id`` while preserving surviving objects.

    This function deliberately does not deduplicate candidates.  Repeated
    candidate rows remain repeated, and every upgrade/weight/metadata field is
    retained because the original candidate object is returned unchanged.
    """

    excluded = normalize_excluded_reward_card_ids(excluded_reward_card_ids)
    get_card_id = _default_card_id if card_id_getter is None else card_id_getter
    result: list[CandidateT] = []
    for candidate in candidates:
        card_id = get_card_id(candidate)
        if not isinstance(card_id, str) or not card_id:
            raise ValueError("candidate card_id getter must return a non-empty string")
        if card_id not in excluded:
            result.append(candidate)
    return tuple(result)


__all__ = [
    "filter_excluded_reward_card_candidates",
    "normalize_excluded_reward_card_ids",
]
