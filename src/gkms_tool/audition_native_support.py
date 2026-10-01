"""Pure deterministic replay of native HandAdd support-upgrade rolls.

This module intentionally knows nothing about zone transitions or game input.
Its caller supplies cards in the exact native HandAdd order and the RNG state
after any preceding draw/shuffle consumers have completed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .audition_support_runtime import (
    SUPPORTED_LESSON_TYPES,
    SUPPORTED_SUPPORT_LESSON_TYPES,
    SupportUpgradeRuntimeInput,
    support_lesson_type_matches,
)
from .card_search import ProduceCardSearchRule, match_support_hand_add_search
from .exam_native_rng import UINT32_MASK, next_range


MAX_SUPPORT_UPGRADE = 3


class NativeHandAddSupportError(ValueError):
    """Fail-closed input or native-predicate error with a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = code if not detail else f"{code}:{detail}"
        super().__init__(message)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NativeHandAddSupportError("invalid-text", label)
    return value


def _upgrade(value: object, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= MAX_SUPPORT_UPGRADE
    ):
        raise NativeHandAddSupportError("invalid-upgrade", label)
    return value


def _uint32(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise NativeHandAddSupportError("invalid-random-state", repr(value))
    if not 0 <= value <= UINT32_MASK:
        raise NativeHandAddSupportError("invalid-random-state", str(value))
    return value


@dataclass(frozen=True, order=True, slots=True)
class NativeHandAddCard:
    """One already-drawn card in native HandAdd enumeration order."""

    guid: str
    card_id: str
    base_upgrade: int
    effective_upgrade: int

    def __post_init__(self) -> None:
        _text(self.guid, "guid")
        _text(self.card_id, "card_id")
        _upgrade(self.base_upgrade, "base_upgrade")
        _upgrade(self.effective_upgrade, "effective_upgrade")
        if self.effective_upgrade < self.base_upgrade:
            raise NativeHandAddSupportError(
                "invalid-upgrade", "effective_upgrade is below base_upgrade"
            )


@dataclass(frozen=True, order=True, slots=True)
class NativeHandAddCardResult:
    guid: str
    card_id: str
    base_upgrade: int
    initial_effective_upgrade: int
    added_support_ids: tuple[str, ...]
    final_effective_upgrade: int


@dataclass(frozen=True, order=True, slots=True)
class NativeHandAddSupportRoll:
    """One RNG-consuming support threshold test, for audit only."""

    card_order: int
    card_guid: str
    card_id: str
    support_id: str
    support_loadout_order: int
    card_search_id: str
    state_before: int
    roll: int
    state_after: int
    runtime_permil: int
    success: bool


@dataclass(frozen=True, slots=True)
class NativeHandAddSupportResult:
    cards: tuple[NativeHandAddCardResult, ...]
    used_support_ids: tuple[str, ...]
    initial_random_state: int
    final_random_state: int
    # Audit detail must not alter semantic equality or hashes.
    audit_trace: tuple[NativeHandAddSupportRoll, ...] = field(
        default=(), compare=False, hash=False
    )


def _ordered_supports(
    values: Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput],
) -> tuple[SupportUpgradeRuntimeInput, ...]:
    if isinstance(values, Mapping):
        raw = tuple(values.values())
        for key, support in values.items():
            if (
                not isinstance(support, SupportUpgradeRuntimeInput)
                or key != support.support_id
            ):
                raise NativeHandAddSupportError(
                    "support-runtime-key-mismatch", str(key)
                )
    else:
        try:
            raw = tuple(values)
        except TypeError as error:
            raise NativeHandAddSupportError("support-runtime-input-invalid") from error
    if any(not isinstance(value, SupportUpgradeRuntimeInput) for value in raw):
        raise NativeHandAddSupportError("support-runtime-input-invalid")
    for support in raw:
        if (
            not isinstance(support.support_id, str)
            or not support.support_id.strip()
            or support.lesson_type not in SUPPORTED_SUPPORT_LESSON_TYPES
            or not isinstance(support.runtime_permil, int)
            or isinstance(support.runtime_permil, bool)
            or not 0 <= support.runtime_permil <= 1000
            or not isinstance(support.loadout_order, int)
            or isinstance(support.loadout_order, bool)
            or support.loadout_order < 0
            or not isinstance(support.card_search_id, str)
            or not support.card_search_id.strip()
        ):
            raise NativeHandAddSupportError(
                "support-runtime-input-invalid", repr(support)
            )
    ids = tuple(value.support_id for value in raw)
    if len(set(ids)) != len(ids):
        raise NativeHandAddSupportError("support-runtime-id-duplicate")
    orders = tuple(value.loadout_order for value in raw)
    if len(set(orders)) != len(orders):
        raise NativeHandAddSupportError("support-runtime-order-duplicate")
    return tuple(sorted(raw, key=lambda value: value.loadout_order))


def _validated_searches(
    supports: tuple[SupportUpgradeRuntimeInput, ...],
    searches: Mapping[str, ProduceCardSearchRule],
) -> dict[str, ProduceCardSearchRule]:
    if not isinstance(searches, Mapping):
        raise NativeHandAddSupportError("support-card-search-input-invalid")
    result: dict[str, ProduceCardSearchRule] = {}
    for search_id, rule in searches.items():
        if not isinstance(search_id, str) or not isinstance(
            rule, ProduceCardSearchRule
        ):
            raise NativeHandAddSupportError(
                "support-card-search-input-invalid", str(search_id)
            )
        if search_id != rule.id:
            raise NativeHandAddSupportError(
                "support-card-search-key-mismatch", str(search_id)
            )
        if rule.id in result:
            raise NativeHandAddSupportError(
                "support-card-search-id-duplicate", rule.id
            )
        result[rule.id] = rule
    for support in supports:
        rule = result.get(support.card_search_id)
        if rule is None:
            raise NativeHandAddSupportError(
                "support-card-search-missing", support.card_search_id
            )
        # Predicate support is structural.  Probe it even when no drawn card
        # happens to reach this support, so unsupported input cannot hide.
        _matches, unsupported = _match_search(
            rule, "__native_hand_add_probe__", 0
        )
        if unsupported is not None:
            raise NativeHandAddSupportError(
                "support-card-search-unsupported", unsupported
            )
    return result


def _match_search(
    rule: ProduceCardSearchRule, card_id: str, effective_upgrade: int
) -> tuple[bool, str | None]:
    try:
        return match_support_hand_add_search(rule, card_id, effective_upgrade)
    except (TypeError, ValueError) as error:
        raise NativeHandAddSupportError(
            "support-card-search-unsupported",
            f"{rule.id}:{type(error).__name__}:{error}",
        ) from error


def evaluate_native_hand_add_support(
    drawn_cards: Iterable[NativeHandAddCard],
    *,
    lesson_type: str,
    random_state: int,
    support_upgrades: Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput],
    support_card_searches: Mapping[str, ProduceCardSearchRule],
    used_support_ids: Iterable[str] = (),
) -> NativeHandAddSupportResult:
    """Replay deterministic Android v3.2.3 HandAdd support processing.

    Search matching and the initial IsUpgradable check use the card's supplied
    effective upgrade.  Successful support IDs are marked used immediately.
    All successes for one card are collected before upgrade application, so a
    later success can be consumed even when an earlier one already reached +3.
    """

    if lesson_type not in SUPPORTED_LESSON_TYPES:
        raise NativeHandAddSupportError("unsupported-lesson", str(lesson_type))
    initial_state = _uint32(random_state)
    try:
        cards = tuple(drawn_cards)
    except TypeError as error:
        raise NativeHandAddSupportError("drawn-card-input-invalid") from error
    if any(not isinstance(card, NativeHandAddCard) for card in cards):
        raise NativeHandAddSupportError("drawn-card-input-invalid")
    guids = tuple(card.guid for card in cards)
    if len(set(guids)) != len(guids):
        raise NativeHandAddSupportError("drawn-card-guid-duplicate")

    supports = _ordered_supports(support_upgrades)
    searches = _validated_searches(supports, support_card_searches)
    support_by_id = {support.support_id: support for support in supports}
    try:
        initial_used = tuple(used_support_ids)
    except TypeError as error:
        raise NativeHandAddSupportError("support-used-input-invalid") from error
    if any(not isinstance(value, str) or not value.strip() for value in initial_used):
        raise NativeHandAddSupportError("support-used-input-invalid")
    if len(set(initial_used)) != len(initial_used):
        raise NativeHandAddSupportError("support-used-id-duplicate")
    unknown_used = set(initial_used) - set(support_by_id)
    if unknown_used:
        raise NativeHandAddSupportError(
            "support-used-id-unknown", sorted(unknown_used)[0]
        )

    used = set(initial_used)
    cursor = initial_state
    results: list[NativeHandAddCardResult] = []
    trace: list[NativeHandAddSupportRoll] = []
    for card_order, card in enumerate(cards):
        successful: list[str] = []
        if card.effective_upgrade < MAX_SUPPORT_UPGRADE:
            for support in supports:
                if (
                    not support_lesson_type_matches(
                        support.lesson_type, lesson_type
                    )
                    or support.support_id in used
                ):
                    continue
                search = searches[support.card_search_id]
                matches, unsupported = _match_search(
                    search, card.card_id, card.effective_upgrade
                )
                if unsupported is not None:
                    raise NativeHandAddSupportError(
                        "support-card-search-unsupported", unsupported
                    )
                if not matches:
                    continue
                before = cursor
                roll, cursor = next_range(cursor, 0, 1000)
                success = roll < support.runtime_permil
                trace.append(
                    NativeHandAddSupportRoll(
                        card_order=card_order,
                        card_guid=card.guid,
                        card_id=card.card_id,
                        support_id=support.support_id,
                        support_loadout_order=support.loadout_order,
                        card_search_id=support.card_search_id,
                        state_before=before,
                        roll=roll,
                        state_after=cursor,
                        runtime_permil=support.runtime_permil,
                        success=success,
                    )
                )
                if success:
                    used.add(support.support_id)
                    successful.append(support.support_id)

        effective = card.effective_upgrade
        added: list[str] = []
        for support_id in successful:
            if effective < MAX_SUPPORT_UPGRADE:
                effective += 1
                added.append(support_id)
        results.append(
            NativeHandAddCardResult(
                guid=card.guid,
                card_id=card.card_id,
                base_upgrade=card.base_upgrade,
                initial_effective_upgrade=card.effective_upgrade,
                added_support_ids=tuple(added),
                final_effective_upgrade=effective,
            )
        )

    ordered_used = tuple(
        support.support_id for support in supports if support.support_id in used
    )
    return NativeHandAddSupportResult(
        cards=tuple(results),
        used_support_ids=ordered_used,
        initial_random_state=initial_state,
        final_random_state=cursor,
        audit_trace=tuple(trace),
    )


__all__ = [
    "MAX_SUPPORT_UPGRADE",
    "NativeHandAddCard",
    "NativeHandAddCardResult",
    "NativeHandAddSupportError",
    "NativeHandAddSupportResult",
    "NativeHandAddSupportRoll",
    "evaluate_native_hand_add_support",
]
