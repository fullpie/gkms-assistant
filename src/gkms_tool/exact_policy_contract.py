"""Strict identity binding for frozen native Exam main-action candidates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final


SUPPORTED_KINDS: Final = frozenset({"use-hand", "use-drink", "turn-end"})


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _kind(value: Mapping[str, Any]) -> str:
    kind = value.get("kind", value.get("action_type"))
    aliases = {
        "play": "use-hand",
        "card": "use-hand",
        "use_hand": "use-hand",
        "drink": "use-drink",
        "use_drink": "use-drink",
        "end_turn": "turn-end",
        "turn_end": "turn-end",
    }
    return aliases.get(str(kind), str(kind))


def _hand_identity(state_before: Mapping[str, Any]) -> dict[str, tuple[int, str]]:
    hand = state_before.get("handList")
    if not isinstance(hand, list):
        raise ValueError("exact policy state has no native handList")
    result: dict[str, tuple[int, str]] = {}
    for slot, raw in enumerate(hand):
        entry = _mapping(raw)
        guid = entry.get("_guid", entry.get("guid"))
        card_data = _mapping(entry.get("_cardData", entry.get("cardData")))
        card_id = card_data.get("_id", card_data.get("id", entry.get("card_id")))
        if not isinstance(guid, str) or not guid:
            raise ValueError("exact policy hand card has no GUID")
        if not isinstance(card_id, str) or not card_id:
            raise ValueError("exact policy hand card has no card ID")
        if guid in result:
            raise ValueError("exact policy hand contains duplicate GUID")
        result[guid] = (slot, card_id)
    return result


def _drink_identity(state_before: Mapping[str, Any]) -> tuple[str, ...]:
    drinks = state_before.get("drinkList")
    if not isinstance(drinks, list):
        raise ValueError("exact policy state has no native drinkList")
    result: list[str] = []
    for raw in drinks:
        entry = _mapping(raw)
        drink_id = entry.get("_id", entry.get("id"))
        if not isinstance(drink_id, str) or not drink_id:
            raise ValueError("exact policy drink has no ID")
        result.append(drink_id)
    return tuple(result)


def _validated_candidate_identities(
    state_before: Mapping[str, Any],
    legal_candidates: Sequence[object],
) -> tuple[tuple[str, object, object], ...]:
    if not isinstance(state_before, Mapping):
        raise TypeError("exact policy state must be an object")
    if not legal_candidates:
        raise ValueError("exact policy legal candidates are empty")
    hand = _hand_identity(state_before)
    drinks = _drink_identity(state_before)
    normalized: list[tuple[str, object, object]] = []
    turn_end_count = 0
    for position, raw in enumerate(legal_candidates):
        candidate = _mapping(raw)
        if not candidate or candidate.get("legal") is not True:
            raise ValueError("exact policy candidate is not authoritatively legal")
        kind = _kind(candidate)
        if kind not in SUPPORTED_KINDS:
            raise ValueError(f"unsupported exact policy action family: {kind}")
        slot = candidate.get("slot_index")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            raise ValueError("exact policy candidate slot is invalid")
        if kind == "use-hand":
            guid = candidate.get("card_guid", candidate.get("guid"))
            if not isinstance(guid, str) or guid not in hand:
                raise ValueError("exact policy card candidate is outside native hand")
            hand_slot, hand_card_id = hand[guid]
            if slot != hand_slot:
                raise ValueError("exact policy card slot does not match native hand")
            card_id = candidate.get("card_id")
            if card_id != hand_card_id:
                raise ValueError("exact policy candidate card ID disagrees with native hand")
            identity = (kind, guid, slot)
        elif kind == "use-drink":
            drink_id = candidate.get("drink_id")
            if slot >= len(drinks) or drink_id != drinks[slot]:
                raise ValueError("exact policy drink slot/ID disagrees with native state")
            identity = (kind, drink_id, slot)
        else:
            turn_end_count += 1
            identity = (kind, "END_TURN", slot)
        normalized.append(identity)
    if turn_end_count != 1:
        raise ValueError("exact policy legal set must contain one turn-end action")
    if len(set(normalized)) != len(normalized):
        raise ValueError("exact policy legal set contains duplicate identities")
    return tuple(normalized)


def validate_exact_legal_candidates(
    state_before: Mapping[str, Any],
    legal_candidates: Sequence[object],
) -> None:
    """Validate the complete runtime candidate surface without choosing one."""

    _validated_candidate_identities(state_before, legal_candidates)


def strict_exact_candidate_index(
    state_before: Mapping[str, Any],
    action: Mapping[str, Any],
    legal_candidates: Sequence[object],
) -> int:
    """Return the uniquely bound candidate; never fall back from GUID to card ID."""

    if not isinstance(action, Mapping):
        raise TypeError("exact policy action must be an object")
    normalized = _validated_candidate_identities(state_before, legal_candidates)

    action_kind = _kind(action)
    if action_kind not in SUPPORTED_KINDS or action.get("legal") is not True:
        raise ValueError("exact policy chosen action is not authoritatively legal")
    if action_kind == "use-hand":
        guid = action.get("card_guid", action.get("guid"))
        slot = action.get("slot_index")
        action_identity = (action_kind, guid, slot)
    elif action_kind == "use-drink":
        action_identity = (
            action_kind,
            action.get("drink_id"),
            action.get("slot_index"),
        )
    else:
        action_identity = (
            action_kind,
            "END_TURN",
            action.get("slot_index"),
        )
    matches = [
        index for index, identity in enumerate(normalized) if identity == action_identity
    ]
    if len(matches) != 1:
        raise ValueError("exact policy chosen action has no unique strict candidate")
    return matches[0]


__all__ = [
    "SUPPORTED_KINDS",
    "strict_exact_candidate_index",
    "validate_exact_legal_candidates",
]
