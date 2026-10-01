"""Plan2/Common boundary for the shared Hand -> Grave -> draw primitive.

The exact Master row is plan-neutral: the recovered executor constructor has
no plan field and the Plan3 module already owns the typed GUID, zone, limit,
support-callback, and RNG transition.  This module only applies the
Plan2/Common boundary check and aliases that implementation.  It deliberately
does not introduce a second state model or copy the native algorithm.

Rows without plan metadata are accepted because the Master effect row is a
shared payload row.  A caller that supplies a plan tag must supply Common or
Plan2; every other tag fails closed before delegation.
"""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import TypeAlias

from .plan3_hand_grave_draw import (
    DRAW_COUNT_FORMULA,
    GRAVE_MOVE_ENUM_VALUE,
    HAND_POSITION_ENUM_VALUE,
    HandGraveDrawCardReference,
    HandGraveDrawContract,
    HandGraveDrawContractError,
    HandGraveDrawExecutable,
    HandGraveDrawResolution,
    HandGraveDrawTransition,
    HandGraveDrawUnresolved,
    HandGraveDrawUnresolvedError,
    MASTER_EFFECT_GROUP_ID,
    MASTER_EFFECT_ID,
    MASTER_EFFECT_TYPE,
    MASTER_ENUM_NAME,
    MASTER_ENUM_VALUE,
    NATIVE_ADD_TOTAL_EFFECT_DRAW_COUNT_VA,
    NATIVE_CONSTRUCTOR_TOKEN,
    NATIVE_CONSTRUCTOR_VA,
    NATIVE_CREATE_CARD_POSITION_LIST_VA,
    NATIVE_DRAW_CARD_VA,
    NATIVE_EXECUTE_RANGE,
    NATIVE_EXECUTE_TOKEN,
    NATIVE_EXECUTE_VA,
    NATIVE_EXECUTOR_TYPE,
    NATIVE_MOVE_CARD_VA,
    NATIVE_OPERATION_ORDER,
    NATIVE_SELECTED_INDEX_PREDICATE_VA,
    NATIVE_SELECT_CARD_VA,
    NATIVE_SELECT_WITH_INDEX_VA,
    NATIVE_TYPE_GLOBAL_VA,
    NATIVE_TYPE_INDEX,
    NATIVE_USAGE_CELL_VA,
    REQUIRED_NATIVE_FORMULA,
    REQUIRED_NATIVE_INPUTS,
    SIMULATION_BEHAVIOR,
    UNRESOLVED_REASON,
    execute_hand_grave_draw,
    load_hand_grave_draw,
    parse_hand_grave_draw_master_row,
    resolve_hand_grave_draw,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


PLAN_COMMON = "ProducePlanType_Common"
PLAN2 = "ProducePlanType_Plan2"
PLAN2_COMMON_PLAN_TYPES = frozenset((PLAN_COMMON, PLAN2))

# The Plan3 module has a different plan-specific card reference.  Keep the
# Plan2/Common catalog explicit at this boundary; the shared effect row and
# executor remain the same.
AFFECTED_CARD_IDS = (
    "p_card-00-men-3_005",
    "p_card-02-sup-3_195",
)
AFFECTED_CARD_VERSIONS = tuple(
    (card_id, upgrade)
    for card_id in AFFECTED_CARD_IDS
    for upgrade in range(4)
)

# Typed aliases make the Plan2-facing contract explicit without creating a
# parallel state/result implementation.
Plan2HandGraveDrawState: TypeAlias = Plan3NativeState
Plan2HandGraveDrawCard: TypeAlias = Plan3NativeCard
Plan2HandGraveDrawContract: TypeAlias = HandGraveDrawContract
Plan2HandGraveDrawExecutable: TypeAlias = HandGraveDrawExecutable
Plan2HandGraveDrawResolution: TypeAlias = HandGraveDrawResolution
Plan2HandGraveDrawTransition: TypeAlias = HandGraveDrawTransition
Plan2HandGraveDrawUnresolved: TypeAlias = HandGraveDrawUnresolved


class Plan2HandGraveDrawResolutionError(HandGraveDrawContractError):
    """A Plan2/Common boundary tag is outside the shared contract."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}")


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
        raise Plan2HandGraveDrawResolutionError("unsupported-plan", str(observed))


def resolve_plan2_hand_grave_draw(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    plan_type: str | None = None,
) -> Plan2HandGraveDrawResolution:
    """Resolve a Plan2/Common row through the shared exact-shape resolver."""

    _require_plan2_common(effect_row, plan_type)
    return resolve_hand_grave_draw(effect_row)


def try_resolve_plan2_hand_grave_draw(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    plan_type: str | None = None,
) -> Plan2HandGraveDrawResolution:
    """Return a typed unresolved result for an unsupported plan tag."""

    try:
        return resolve_plan2_hand_grave_draw(effect_row, plan_type=plan_type)
    except Plan2HandGraveDrawResolutionError as error:
        raw_id = _row_value(effect_row, "id")
        effect_id = raw_id if isinstance(raw_id, str) and raw_id else "<invalid-effect-id>"
        return HandGraveDrawUnresolved(
            effect_id=effect_id,
            blocker_code=error.code,
            detail=error.detail or error.code,
        )


def parse_plan2_hand_grave_draw_master_row(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    plan_type: str | None = None,
) -> Plan2HandGraveDrawContract:
    """Parse the shared contract after the Plan2/Common boundary check."""

    _require_plan2_common(effect_row, plan_type)
    return parse_hand_grave_draw_master_row(effect_row)


# These are aliases, rather than copied wrappers, so loading and execution
# cannot drift from the existing GUID/RNG/zone primitive.
load_plan2_hand_grave_draw = load_hand_grave_draw
load_master_plan2_hand_grave_draw = load_hand_grave_draw
execute_plan2_hand_grave_draw = execute_hand_grave_draw


__all__ = [
    "AFFECTED_CARD_IDS",
    "AFFECTED_CARD_VERSIONS",
    "DRAW_COUNT_FORMULA",
    "GRAVE_MOVE_ENUM_VALUE",
    "HAND_POSITION_ENUM_VALUE",
    "HandGraveDrawCardReference",
    "HandGraveDrawContract",
    "HandGraveDrawContractError",
    "HandGraveDrawExecutable",
    "HandGraveDrawResolution",
    "HandGraveDrawTransition",
    "HandGraveDrawUnresolved",
    "HandGraveDrawUnresolvedError",
    "MASTER_EFFECT_GROUP_ID",
    "MASTER_EFFECT_ID",
    "MASTER_EFFECT_TYPE",
    "MASTER_ENUM_NAME",
    "MASTER_ENUM_VALUE",
    "NATIVE_ADD_TOTAL_EFFECT_DRAW_COUNT_VA",
    "NATIVE_CONSTRUCTOR_TOKEN",
    "NATIVE_CONSTRUCTOR_VA",
    "NATIVE_CREATE_CARD_POSITION_LIST_VA",
    "NATIVE_DRAW_CARD_VA",
    "NATIVE_EXECUTE_RANGE",
    "NATIVE_EXECUTE_TOKEN",
    "NATIVE_EXECUTE_VA",
    "NATIVE_EXECUTOR_TYPE",
    "NATIVE_MOVE_CARD_VA",
    "NATIVE_OPERATION_ORDER",
    "NATIVE_SELECTED_INDEX_PREDICATE_VA",
    "NATIVE_SELECT_CARD_VA",
    "NATIVE_SELECT_WITH_INDEX_VA",
    "NATIVE_TYPE_GLOBAL_VA",
    "NATIVE_TYPE_INDEX",
    "NATIVE_USAGE_CELL_VA",
    "PLAN2",
    "PLAN2_COMMON_PLAN_TYPES",
    "PLAN_COMMON",
    "Plan2HandGraveDrawCard",
    "Plan2HandGraveDrawContract",
    "Plan2HandGraveDrawExecutable",
    "Plan2HandGraveDrawResolution",
    "Plan2HandGraveDrawResolutionError",
    "Plan2HandGraveDrawState",
    "Plan2HandGraveDrawTransition",
    "Plan2HandGraveDrawUnresolved",
    "Plan3NativeCard",
    "Plan3NativeState",
    "REQUIRED_NATIVE_FORMULA",
    "REQUIRED_NATIVE_INPUTS",
    "SIMULATION_BEHAVIOR",
    "UNRESOLVED_REASON",
    "execute_plan2_hand_grave_draw",
    "load_master_plan2_hand_grave_draw",
    "load_plan2_hand_grave_draw",
    "parse_plan2_hand_grave_draw_master_row",
    "resolve_plan2_hand_grave_draw",
    "try_resolve_plan2_hand_grave_draw",
]
