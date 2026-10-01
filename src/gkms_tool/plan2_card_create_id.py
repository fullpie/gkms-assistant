"""Plan2/Common contract for the shared ``ExamCardCreateId`` primitive.

The Android executor has no plan-specific input: its constructor stores only
the effect payload, and the Plan3 module already owns the typed GUID/RNG/
placement implementation.  This module is therefore deliberately an adapter
surface, not a second implementation.  The state and result types are aliases
of the existing Plan3-native types; ``Plan2State`` is intentionally not used.

Rows without plan metadata are accepted because Master effect rows are shared
payload rows.  When a card/adapter supplies a plan tag, only Common and Plan2
are accepted.  Everything else fails closed before delegation.
"""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import TypeAlias

from .plan3_card_create_id import (
    CARD_CREATE_ID_EFFECT_TYPE,
    CardCreateBranch,
    CardCreateContract,
    CardCreateDestination,
    CardCreateError,
    CardCreateGuidAllocator,
    CardCreateInputError,
    CardCreateMutation,
    CardCreateMutationTrace,
    CardCreatePlacementBranch,
    CardCreateResolutionError,
    CardCreateResolvedBranch,
    CardCreateResult,
    CardCreateRngBranch,
    CardCreateUnresolvedBranch,
    CardCreateUnresolvedInput,
    ExplicitGuidAllocator,
    apply_card_create_id,
    execute_card_create_id,
    load_master_card_create_id_rows,
    resolve_card_create_contract,
    try_resolve_card_create_contract,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


PLAN_COMMON = "ProducePlanType_Common"
PLAN2 = "ProducePlanType_Plan2"
PLAN2_COMMON_PLAN_TYPES = frozenset((PLAN_COMMON, PLAN2))

# Typed aliases keep the Plan2-facing API explicit without introducing a
# parallel state model or copying the mutation algorithm.
Plan2CardCreateState: TypeAlias = Plan3NativeState
Plan2CardCreateCard: TypeAlias = Plan3NativeCard
Plan2CardCreateContract: TypeAlias = CardCreateContract
Plan2CardCreateResult: TypeAlias = CardCreateResult


def _row_value(
    row: Mapping[str, object] | sqlite3.Row, *names: str
) -> object:
    if isinstance(row, Mapping):
        for name in names:
            if name in row:
                return row[name]
        return None
    keys = getattr(row, "keys", None)
    if callable(keys):
        available = set(keys())
        for name in names:
            if name in available:
                return row[name]
    return None


def _require_plan2_common(
    row: Mapping[str, object] | sqlite3.Row,
    explicit_plan_type: str | None,
) -> None:
    observed = explicit_plan_type
    if observed is None:
        candidate = _row_value(
            row,
            "plan_type",
            "planType",
            "producePlanType",
            "card_plan_type",
        )
        if candidate is not None:
            observed = candidate if isinstance(candidate, str) else str(candidate)
    if observed is not None and observed not in PLAN2_COMMON_PLAN_TYPES:
        raise CardCreateResolutionError("unsupported-plan", str(observed))


def resolve_plan2_card_create_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    plan_type: str | None = None,
) -> Plan2CardCreateContract:
    """Resolve a Plan2/Common row through the Plan3 contract resolver.

    The optional plan tag is a boundary check only.  All structural parsing,
    ID/field agreement checks, and shape rejection remain in the shared
    resolver.
    """

    _require_plan2_common(effect_row, plan_type)
    return resolve_card_create_contract(effect_row)


def try_resolve_plan2_card_create_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    plan_type: str | None = None,
) -> Plan2CardCreateContract | CardCreateUnresolvedInput:
    """Fail closed as a typed unresolved input for Plan2/Common callers."""

    try:
        return resolve_plan2_card_create_contract(effect_row, plan_type=plan_type)
    except CardCreateResolutionError as error:
        return CardCreateUnresolvedInput(error.code, error.detail or error.code)


# These are aliases, rather than wrappers, so the Plan2 surface cannot drift
# from the already-tested GUID/RNG/placement primitive.
load_master_plan2_card_create_id_rows = load_master_card_create_id_rows
apply_plan2_card_create_id = apply_card_create_id
execute_plan2_card_create_id = execute_card_create_id


__all__ = [
    "CARD_CREATE_ID_EFFECT_TYPE",
    "PLAN_COMMON",
    "PLAN2",
    "PLAN2_COMMON_PLAN_TYPES",
    "Plan2CardCreateCard",
    "Plan2CardCreateContract",
    "Plan2CardCreateState",
    "Plan2CardCreateResult",
    "CardCreateBranch",
    "CardCreateContract",
    "CardCreateDestination",
    "CardCreateError",
    "CardCreateGuidAllocator",
    "CardCreateInputError",
    "CardCreateMutation",
    "CardCreateMutationTrace",
    "CardCreatePlacementBranch",
    "CardCreateResolutionError",
    "CardCreateResolvedBranch",
    "CardCreateResult",
    "CardCreateRngBranch",
    "CardCreateUnresolvedBranch",
    "CardCreateUnresolvedInput",
    "ExplicitGuidAllocator",
    "Plan3NativeCard",
    "Plan3NativeState",
    "load_master_plan2_card_create_id_rows",
    "resolve_plan2_card_create_contract",
    "try_resolve_plan2_card_create_contract",
    "apply_plan2_card_create_id",
    "execute_plan2_card_create_id",
]
