"""Plan2/Common boundary for the shared native AntiDebuff charge runtime.

The effect is plan-neutral in the proven native surface.  This module keeps a
Plan2-facing catalog and a small target-classification boundary, then delegates
the count transition and pre-add projection to ``plan3_anti_debuff``.  It does
not create a second charge algorithm.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Mapping, TypeAlias

from .plan3_anti_debuff import (
    ANTI_DEBUFF_CATALOG as COMMON_ANTI_DEBUFF_CATALOG,
    ANTI_DEBUFF_EFFECT_TYPE_VALUE,
    ANTI_DEBUFF_PERMANENT_TURN,
    AntiDebuffBlockResult,
    AntiDebuffCatalog,
    AntiDebuffDifference,
    AntiDebuffEffectRow,
    AntiDebuffExecution,
    AntiDebuffRuntime,
    CardEffectSlot,
    CommonCardVersion,
    EffectField,
    ExamStatusEffectTargetType,
    EXECUTION_EFFECT_FIELD_ORDER,
    ExecutionStatus,
    RAW_EFFECT_FIELD_ORDER,
    UnresolvedExecution,
    UnresolvedReason,
    execute_anti_debuff as _shared_execute_anti_debuff,
    try_block_status_addition as _shared_try_block_status_addition,
)


EFFECT_TYPE = "ProduceExamEffectType_ExamAntiDebuff"
PLAN_COMMON = "ProducePlanType_Common"
PLAN2 = "ProducePlanType_Plan2"
PLAN2_COMMON_PLAN_TYPES = frozenset((PLAN_COMMON, PLAN2))


# The catalog objects and runtime types remain the same immutable objects as
# the Common primitive.  Only the scope label is adapted for this standalone
# surface so a report can distinguish the caller boundary.
ANTI_DEBUFF_CATALOG: AntiDebuffCatalog = replace(
    COMMON_ANTI_DEBUFF_CATALOG,
    scope="Plan2/Common",
)
PLAN2_ANTI_DEBUFF_CATALOG = ANTI_DEBUFF_CATALOG
CATALOG = ANTI_DEBUFF_CATALOG
COMMON_CATALOG = COMMON_ANTI_DEBUFF_CATALOG

MASTER_EFFECT_ROWS = COMMON_ANTI_DEBUFF_CATALOG.master_effect_rows
EXECUTABLE_EFFECT_ROWS = MASTER_EFFECT_ROWS
COMMON_AFFECTED_CARD_VERSIONS = (
    COMMON_ANTI_DEBUFF_CATALOG.affected_card_versions
)
AFFECTED_CARD_VERSIONS = COMMON_AFFECTED_CARD_VERSIONS


Plan2AntiDebuffRuntime: TypeAlias = AntiDebuffRuntime
Plan2AntiDebuffEffectRow: TypeAlias = AntiDebuffEffectRow
Plan2AntiDebuffCatalog: TypeAlias = AntiDebuffCatalog
Plan2AntiDebuffExecution: TypeAlias = AntiDebuffExecution
Plan2AntiDebuffBlockResult: TypeAlias = AntiDebuffBlockResult


class Plan2AntiDebuffContractError(ValueError):
    """A caller boundary is outside the proven Plan2/Common contract."""


def validate_plan2_common_plan(plan_type: object) -> str:
    """Accept only the two plan labels allowed at this adapter boundary."""

    if not isinstance(plan_type, str) or plan_type not in PLAN2_COMMON_PLAN_TYPES:
        raise Plan2AntiDebuffContractError(
            f"unsupported plan type: {plan_type!r}"
        )
    return plan_type


# These are the relevant entries proven by the native target-type table.  The
# wrapper intentionally refuses to infer a target for any other effect value.
PLAN2_TARGET_CLASSIFICATION: Mapping[int, ExamStatusEffectTargetType] = (
    MappingProxyType(
        {
            ANTI_DEBUFF_EFFECT_TYPE_VALUE: ExamStatusEffectTargetType.BUFF,
            105: ExamStatusEffectTargetType.DEBUFF,
            106: ExamStatusEffectTargetType.DEBUFF,
        }
    )
)


def classify_plan2_effect_target(
    incoming_effect_type_value: object,
) -> ExamStatusEffectTargetType | None:
    """Return the proven native target class, or ``None`` when unknown."""

    if isinstance(incoming_effect_type_value, bool):
        return None
    if not isinstance(incoming_effect_type_value, int):
        return None
    return PLAN2_TARGET_CLASSIFICATION.get(incoming_effect_type_value)


@dataclass(frozen=True, slots=True)
class Plan2AntiDebuffUnresolvedBlock:
    """No-op result used when target classification cannot be proven."""

    state_before: object
    state_after: object
    incoming_effect_type_value: object
    incoming_target_type: object
    reason: str
    simulated: bool = False
    status: ExecutionStatus = ExecutionStatus.UNRESOLVED
    blocked: bool = False
    consumed_count: int = 0
    removed_status: bool = False
    difference: None = None
    trace: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is not ExecutionStatus.UNRESOLVED:
            raise ValueError("unresolved block must remain unresolved")
        if self.state_after is not self.state_before:
            raise ValueError("unresolved block must preserve state identity")
        if self.blocked or self.consumed_count or self.removed_status:
            raise ValueError("unresolved block must not consume a charge")
        if self.difference is not None:
            raise ValueError("unresolved block must not emit a difference")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("unresolved block reason is required")
        if not isinstance(self.simulated, bool):
            raise TypeError("simulated must be a boolean")
        object.__setattr__(self, "trace", tuple(self.trace))

    @property
    def executable(self) -> bool:
        return False

    @property
    def changed(self) -> bool:
        return False

    @property
    def state_unchanged(self) -> bool:
        return self.state_after is self.state_before

    @property
    def reasons(self) -> tuple[str, ...]:
        return (self.reason,)

    def to_json(self) -> dict[str, object]:
        def state_json(value: object) -> object:
            if isinstance(value, AntiDebuffRuntime):
                return value.to_json()
            return {"unresolved_state_type": type(value).__name__}

        return {
            "status": self.status.value,
            "incoming_effect_type_value": self.incoming_effect_type_value,
            "incoming_target_type": repr(self.incoming_target_type),
            "blocked": False,
            "consumed_count": 0,
            "removed_status": False,
            "simulated": self.simulated,
            "reason": self.reason,
            "state_before": state_json(self.state_before),
            "state_after": state_json(self.state_after),
            "difference": None,
            "trace": list(self.trace),
        }


def _unresolved_block(
    state: object,
    incoming_effect_type_value: object,
    incoming_target_type: object,
    reason: str,
    simulate: object,
) -> Plan2AntiDebuffUnresolvedBlock:
    return Plan2AntiDebuffUnresolvedBlock(
        state_before=state,
        state_after=state,
        incoming_effect_type_value=incoming_effect_type_value,
        incoming_target_type=incoming_target_type,
        reason=reason,
        simulated=simulate if isinstance(simulate, bool) else False,
        trace=("contract:unresolved", f"reason:{reason}"),
    )


def try_block_plan2_status_addition(
    state: object,
    *,
    incoming_effect_type_value: object,
    incoming_target_type: object,
    simulate: bool = False,
) -> AntiDebuffBlockResult | Plan2AntiDebuffUnresolvedBlock:
    """Delegate the native gate only after target classification is proven.

    Unknown effect values, unknown target values, mismatched classifications,
    and unsupported state shapes all return an unchanged unresolved result.
    """

    if not isinstance(state, AntiDebuffRuntime):
        return _unresolved_block(
            state,
            incoming_effect_type_value,
            incoming_target_type,
            "unsupported-state-shape",
            simulate,
        )
    if not isinstance(simulate, bool):
        return _unresolved_block(
            state,
            incoming_effect_type_value,
            incoming_target_type,
            "invalid-simulate-flag",
            simulate,
        )

    expected_target = classify_plan2_effect_target(incoming_effect_type_value)
    if expected_target is None:
        return _unresolved_block(
            state,
            incoming_effect_type_value,
            incoming_target_type,
            "unknown-incoming-effect",
            simulate,
        )
    if not isinstance(incoming_target_type, ExamStatusEffectTargetType):
        return _unresolved_block(
            state,
            incoming_effect_type_value,
            incoming_target_type,
            "unknown-target",
            simulate,
        )
    if incoming_target_type is not expected_target:
        return _unresolved_block(
            state,
            incoming_effect_type_value,
            incoming_target_type,
            "target-classification-mismatch",
            simulate,
        )

    return _shared_try_block_status_addition(
        state,
        incoming_effect_type_value=incoming_effect_type_value,
        incoming_target_type=incoming_target_type,
        simulate=simulate,
    )


# Public name matching the Common primitive's gate API.  It retains the
# adapter's fail-closed classification check; the exact shared callable is
# exposed separately for differential/identity checks.
try_block_status_addition = try_block_plan2_status_addition
shared_try_block_status_addition = _shared_try_block_status_addition


# Effect execution has no plan-specific inputs.  Keep this an actual alias so
# the Plan2 surface cannot drift from the immutable count transition.
execute_plan2_anti_debuff = _shared_execute_anti_debuff
execute_anti_debuff = execute_plan2_anti_debuff
shared_execute_anti_debuff = _shared_execute_anti_debuff


def try_execute_plan2_anti_debuff(
    effect: object,
    state: object,
    *,
    simulate: bool = False,
    plan_type: object | None = None,
) -> AntiDebuffExecution | UnresolvedExecution[object]:
    """Apply the optional plan guard and return a typed no-op on rejection."""

    if plan_type is not None:
        try:
            validate_plan2_common_plan(plan_type)
        except Plan2AntiDebuffContractError:
            row = effect if isinstance(effect, AntiDebuffEffectRow) else None
            return UnresolvedExecution(
                status=ExecutionStatus.UNRESOLVED,
                source_effect_id=(row.source_effect_id if row is not None else ""),
                effect_type=(row.effect_type if row is not None else ""),
                state_before=state,
                state_after=state,
                reasons=(UnresolvedReason.UNSUPPORTED_ROW_SHAPE,),
                effect=row,
            )
    return execute_plan2_anti_debuff(effect, state, simulate=simulate)


def catalog_json() -> dict[str, object]:
    """Return the Plan2/Common scoped catalog as an independent JSON value."""

    return json.loads(json.dumps(ANTI_DEBUFF_CATALOG.to_json()))


__all__ = [
    "AFFECTED_CARD_VERSIONS",
    "ANTI_DEBUFF_CATALOG",
    "ANTI_DEBUFF_EFFECT_TYPE_VALUE",
    "ANTI_DEBUFF_PERMANENT_TURN",
    "CATALOG",
    "COMMON_AFFECTED_CARD_VERSIONS",
    "COMMON_CATALOG",
    "EFFECT_TYPE",
    "EXECUTION_EFFECT_FIELD_ORDER",
    "EXECUTABLE_EFFECT_ROWS",
    "MASTER_EFFECT_ROWS",
    "PLAN2",
    "PLAN2_ANTI_DEBUFF_CATALOG",
    "PLAN2_COMMON_PLAN_TYPES",
    "PLAN2_TARGET_CLASSIFICATION",
    "PLAN_COMMON",
    "AntiDebuffBlockResult",
    "AntiDebuffCatalog",
    "AntiDebuffDifference",
    "AntiDebuffEffectRow",
    "AntiDebuffExecution",
    "AntiDebuffRuntime",
    "CardEffectSlot",
    "CommonCardVersion",
    "EffectField",
    "ExamStatusEffectTargetType",
    "ExecutionStatus",
    "Plan2AntiDebuffBlockResult",
    "Plan2AntiDebuffCatalog",
    "Plan2AntiDebuffContractError",
    "Plan2AntiDebuffEffectRow",
    "Plan2AntiDebuffExecution",
    "Plan2AntiDebuffRuntime",
    "Plan2AntiDebuffUnresolvedBlock",
    "RAW_EFFECT_FIELD_ORDER",
    "UnresolvedExecution",
    "UnresolvedReason",
    "catalog_json",
    "classify_plan2_effect_target",
    "execute_anti_debuff",
    "execute_plan2_anti_debuff",
    "shared_execute_anti_debuff",
    "shared_try_block_status_addition",
    "try_block_plan2_status_addition",
    "try_block_status_addition",
    "try_execute_plan2_anti_debuff",
    "validate_plan2_common_plan",
]
