"""Fail-closed HandAdd support adapter for immutable Plan2 horizon states.

The native HandAdd predicate, support ordering, probability rolls, and used-ID
ordering remain owned by :mod:`audition_native_support`.  This adapter only
projects that exact result onto ordered Plan2 horizon cards while preserving
every zone order and all non-support horizon fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

from .audition_native_ordered_zones import (
    NativeOrderedZoneError,
    NativeOrderedZoneState,
)
from .audition_native_support import (
    NativeHandAddCard,
    NativeHandAddCardResult,
    NativeHandAddSupportError,
    NativeHandAddSupportResult,
    evaluate_native_hand_add_support,
)
from .card_search import ProduceCardSearchRule
from .exam_hand_add_support_runtime import (
    ExamHandAddSupportCatalog,
    ExamHandAddSupportInput,
)
if TYPE_CHECKING:
    from .plan2_native_horizon import Plan2NativeHorizonState


PLAN2_NATIVE_HAND_ADD_SUPPORT_ADAPTER_SCHEMA_VERSION: Final = 1
PLAN2_NATIVE_HAND_ADD_SUPPORT_ADAPTER_ID: Final = (
    "plan2.native.horizon.hand_add_support"
)


# Public Plan 2 names remain stable compatibility aliases.  The value shape is
# native Exam/HandAdd data and is shared by all plans, so its owner is the
# plan-neutral runtime projection module.
Plan2NativeHandAddSupportCatalog = ExamHandAddSupportCatalog
Plan2NativeHandAddSupportInput = ExamHandAddSupportInput


@dataclass(frozen=True, slots=True)
class Plan2NativeHandAddSupportBlocker:
    """Stable reason why the adapter could not execute atomically."""

    code: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Plan2NativeHandAddSupportTransition:
    """Atomic horizon projection plus caller-owned current-turn used IDs."""

    schema_version: int
    adapter_id: str
    before: Plan2NativeHorizonState
    after: Plan2NativeHorizonState
    request: Plan2NativeHandAddSupportInput
    used_support_ids_before: tuple[str, ...]
    used_support_ids_after: tuple[str, ...]
    upgraded_guids: tuple[str, ...]
    native_result: NativeHandAddSupportResult | None = field(
        default=None,
        compare=False,
        hash=False,
        repr=False,
    )
    blocker: Plan2NativeHandAddSupportBlocker | None = None

    @property
    def executable(self) -> bool:
        return self.blocker is None


def _blocked(
    state: Plan2NativeHorizonState,
    request: Plan2NativeHandAddSupportInput,
    code: str,
    detail: str = "",
) -> Plan2NativeHandAddSupportTransition:
    return Plan2NativeHandAddSupportTransition(
        schema_version=PLAN2_NATIVE_HAND_ADD_SUPPORT_ADAPTER_SCHEMA_VERSION,
        adapter_id=PLAN2_NATIVE_HAND_ADD_SUPPORT_ADAPTER_ID,
        before=state,
        after=state,
        request=request,
        used_support_ids_before=request.used_support_ids,
        used_support_ids_after=request.used_support_ids,
        upgraded_guids=(),
        blocker=Plan2NativeHandAddSupportBlocker(code=code, detail=detail),
    )


def _search_mapping(
    rules: tuple[ProduceCardSearchRule, ...],
) -> dict[str, ProduceCardSearchRule]:
    result: dict[str, ProduceCardSearchRule] = {}
    for rule in rules:
        if not isinstance(rule, ProduceCardSearchRule):
            raise NativeHandAddSupportError(
                "support-card-search-input-invalid"
            )
        if rule.id in result:
            raise NativeHandAddSupportError(
                "support-card-search-id-duplicate",
                rule.id,
            )
        result[rule.id] = rule
    return result


def apply_plan2_native_hand_add_support_to_zones(
    zones: NativeOrderedZoneState,
    request: Plan2NativeHandAddSupportInput,
    catalog: Plan2NativeHandAddSupportCatalog,
) -> tuple[
    NativeOrderedZoneState,
    NativeHandAddSupportResult,
    tuple[str, ...],
]:
    """Apply one native HandAdd event directly to ordered zones.

    HandAdd may run while a played card is still in ``pending_played``.  The
    zone primitive is therefore the common authority; the horizon wrapper
    below is only for callers already at a settled node.
    """

    if not isinstance(zones, NativeOrderedZoneState):
        raise TypeError("zones must be NativeOrderedZoneState")
    if not isinstance(request, Plan2NativeHandAddSupportInput):
        raise TypeError("request must be Plan2NativeHandAddSupportInput")
    if not isinstance(catalog, Plan2NativeHandAddSupportCatalog):
        raise TypeError("catalog must be Plan2NativeHandAddSupportCatalog")

    guids = request.drawn_guids
    if any(not isinstance(guid, str) or not guid.strip() for guid in guids):
        raise NativeHandAddSupportError("drawn-guid-invalid")
    if len(set(guids)) != len(guids):
        raise NativeHandAddSupportError("drawn-guid-duplicate")

    hand_by_guid = {card.guid: card for card in zones.hand}
    for guid in guids:
        if guid not in hand_by_guid:
            raise NativeHandAddSupportError("drawn-guid-not-in-hand", guid)

    installed_support_ids = tuple(
        dict.fromkeys(
            support_id
            for card in zones.hand
            for support_id in card.support_upgrade_ids
        )
    )
    missing_used = tuple(
        support_id
        for support_id in installed_support_ids
        if support_id not in request.used_support_ids
    )
    if missing_used:
        raise NativeHandAddSupportError(
            "installed-support-id-not-used",
            missing_used[0],
        )

    drawn_cards = tuple(
        NativeHandAddCard(
            guid=guid,
            card_id=hand_by_guid[guid].card_id,
            base_upgrade=hand_by_guid[guid].base_upgrade,
            effective_upgrade=hand_by_guid[guid].effective_upgrade,
        )
        for guid in guids
    )
    eligible_cards = drawn_cards
    master_refs = catalog.master_card_refs
    if master_refs is not None:
        known = frozenset(master_refs)
        unknown = next(
            (
                card
                for card in drawn_cards
                if (card.card_id, card.effective_upgrade) not in known
            ),
            None,
        )
        if unknown is not None:
            raise NativeHandAddSupportError(
                "hand-add-card-master-missing",
                f"{unknown.card_id}@{unknown.effective_upgrade}",
            )
        eligible_cards = tuple(
            card
            for card in drawn_cards
            if (card.card_id, card.effective_upgrade + 1) in known
        )
    searches = _search_mapping(catalog.support_card_searches)
    native = evaluate_native_hand_add_support(
        eligible_cards,
        lesson_type=request.lesson_type,
        random_state=zones.random_state,
        support_upgrades=catalog.support_upgrades,
        support_card_searches=searches,
        used_support_ids=request.used_support_ids,
    )
    if len(eligible_cards) != len(drawn_cards):
        result_by_guid = {result.guid: result for result in native.cards}
        order_by_guid = {card.guid: order for order, card in enumerate(drawn_cards)}
        native = NativeHandAddSupportResult(
            cards=tuple(
                result_by_guid.get(
                    card.guid,
                    NativeHandAddCardResult(
                        guid=card.guid,
                        card_id=card.card_id,
                        base_upgrade=card.base_upgrade,
                        initial_effective_upgrade=card.effective_upgrade,
                        added_support_ids=(),
                        final_effective_upgrade=card.effective_upgrade,
                    ),
                )
                for card in drawn_cards
            ),
            used_support_ids=native.used_support_ids,
            initial_random_state=native.initial_random_state,
            final_random_state=native.final_random_state,
            audit_trace=tuple(
                replace(
                    event,
                    card_order=order_by_guid[event.card_guid],
                )
                for event in native.audit_trace
            ),
        )

    updated_by_guid = {}
    upgraded_guids: list[str] = []
    for card_result in native.cards:
        current = hand_by_guid[card_result.guid]
        if (
            card_result.card_id != current.card_id
            or card_result.base_upgrade != current.base_upgrade
            or card_result.initial_effective_upgrade != current.effective_upgrade
        ):
            raise NativeHandAddSupportError(
                "native-card-result-drift",
                card_result.guid,
            )
        updated = current.install_support_upgrades(card_result.added_support_ids)
        if updated.effective_upgrade != card_result.final_effective_upgrade:
            raise NativeHandAddSupportError(
                "native-card-upgrade-drift",
                card_result.guid,
            )
        if updated != current:
            updated_by_guid[card_result.guid] = updated
            upgraded_guids.append(card_result.guid)

    universe = tuple(
        updated_by_guid.get(card.guid, card) for card in zones.card_universe
    )
    hand = tuple(updated_by_guid.get(card.guid, card) for card in zones.hand)
    after = replace(
        zones,
        random_state=native.final_random_state,
        card_universe=universe,
        hand=hand,
    )
    return after, native, tuple(upgraded_guids)


def execute_plan2_native_hand_add_support(
    state: Plan2NativeHorizonState,
    request: Plan2NativeHandAddSupportInput,
    catalog: Plan2NativeHandAddSupportCatalog,
) -> Plan2NativeHandAddSupportTransition:
    """Apply exact native HandAdd support outcomes without changing zone order.

    ``request.drawn_guids`` supplies native HandAdd enumeration order; Hand's
    stored order is deliberately not used for evaluation.  On every blocker,
    the original horizon and used-ID tuple are returned unchanged.
    """

    # Keep the adapter importable while the horizon owns its optional support
    # runtime field.  The runtime import is deliberately local to avoid a
    # module cycle; annotations remain available through postponed evaluation.
    from .plan2_native_horizon import Plan2NativeHorizonState

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    if not isinstance(request, Plan2NativeHandAddSupportInput):
        raise TypeError("request must be Plan2NativeHandAddSupportInput")
    if not isinstance(catalog, Plan2NativeHandAddSupportCatalog):
        raise TypeError("catalog must be Plan2NativeHandAddSupportCatalog")

    try:
        zones, native, upgraded_guids = (
            apply_plan2_native_hand_add_support_to_zones(
                state.zones,
                request,
                catalog,
            )
        )
    except (NativeHandAddSupportError, NativeOrderedZoneError) as error:
        code = getattr(error, "code", "ordered-zone-update-failed")
        detail = getattr(error, "detail", str(error))
        return _blocked(state, request, str(code), str(detail))
    after = state if zones == state.zones else replace(state, zones=zones)

    return Plan2NativeHandAddSupportTransition(
        schema_version=PLAN2_NATIVE_HAND_ADD_SUPPORT_ADAPTER_SCHEMA_VERSION,
        adapter_id=PLAN2_NATIVE_HAND_ADD_SUPPORT_ADAPTER_ID,
        before=state,
        after=after,
        request=request,
        used_support_ids_before=request.used_support_ids,
        used_support_ids_after=native.used_support_ids,
        upgraded_guids=upgraded_guids,
        native_result=native,
    )
