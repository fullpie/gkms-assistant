"""Exact executors for twelve remaining triggered Plan2 effect slots.

The predicate owners and effect runtimes already exist independently.  This
leaf cross-binds their exact Master occurrences and dispatches only when the
immutable pre-direct-effect predicate snapshot fires.  Native formulas,
status stacking, status UIDs, and Review lifecycle behavior remain delegated
to the stamina and review-dynamic owners.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, TypeAlias

from .logic_engine import load_master_effect
from .master_db import DEFAULT_DATABASE
from .plan2_native_catalog_effect_triggers import (
    EffectTriggerOrigin,
    Plan2NativeEffectTriggerEvaluation,
    Plan2NativeEffectTriggerHandoff,
    Plan2NativeEffectTriggerSnapshot,
    evaluate_plan2_native_effect_trigger,
    load_plan2_native_catalog_effect_triggers,
)
from .plan2_native_catalog_effect_triggers_remaining import (
    Plan2RemainingEffectTriggerEvaluation,
    Plan2RemainingEffectTriggerHandoff,
    Plan2RemainingEffectTriggerSnapshot,
    evaluate_plan2_remaining_effect_trigger,
    load_plan2_native_catalog_effect_triggers_remaining,
)
from .plan2_native_catalog_review_dynamic import (
    OP_INSTALL_REVIEW_ADDITIVE,
    OP_REVIEW_VALUE_MULTIPLE,
    Plan2NativeReviewDynamicEventInput,
    Plan2NativeReviewDynamicEventTransition,
    Plan2NativeReviewDynamicInstallInput,
    Plan2NativeReviewDynamicInstallTransition,
    Plan2NativeReviewDynamicProgram,
    Plan2NativeReviewDynamicRuntime,
    compile_plan2_native_catalog_review_dynamic,
    execute_plan2_native_review_dynamic_event,
    install_plan2_native_review_dynamic,
)
from .plan2_native_catalog_stamina import (
    CARD_PLAY_STAMINA_TRIGGER_OWNER,
    EFFECT_STAMINA_CONSUMPTION_DOWN_FIX,
    Plan2NativeStaminaEffectHandoff,
    Plan2StaminaModifierExecution,
    Plan2StaminaModifierRuntime,
    StaminaConsumptionDownFixEffect,
    apply_plan2_stamina_modifier,
    compile_plan2_native_catalog_stamina,
)


PLAN2_NATIVE_TRIGGERED_EFFECT_EXECUTORS_SCHEMA_VERSION: Final = 1
TRIGGERED_EFFECT_EXECUTORS_ADAPTER_ID: Final = (
    "plan2.native.catalog.triggered_effect_executors"
)
EXPECTED_TARGET_OCCURRENCE_COUNT: Final = 12
EXPECTED_DIRECT_OCCURRENCE_COUNT: Final = 12
EXPECTED_CO_BLOCKED_OCCURRENCE_COUNT: Final = 0
EXPECTED_TARGET_VERSION_COUNT: Final = 12
EXPECTED_DIRECT_VERSION_COUNT: Final = 12
EXPECTED_CO_BLOCKED_VERSION_COUNT: Final = 0
EXPECTED_UNIQUE_EFFECT_COUNT: Final = 3

STAMINA_CARD_ID: Final = "p_card-00-men-1_007"
REVIEW_VALUE_CARD_ID: Final = "p_card-02-ido-3_165"
REVIEW_ADDITIVE_CARD_ID: Final = "p_card-02-men-3_004"
STAMINA_EFFECT_ID: Final = "e_effect-exam_stamina_consumption_down_fix-0001-inf"
REVIEW_VALUE_EFFECT_ID: Final = "e_effect-exam_review_value_multiple-0100"
REVIEW_ADDITIVE_EFFECT_ID: Final = "e_effect-exam_review_additive-0500-05"
STAMINA_TRIGGER_ID: Final = "e_trigger-exam_card_play-stamina_up_multiple-500"
REVIEW_VALUE_TRIGGER_ID: Final = "e_trigger-none-not-card_play_aggressive_up-9"
REVIEW_ADDITIVE_TRIGGER_ID: Final = "e_trigger-none-review_up-10"


class TriggeredEffectExecutorFamily(str, Enum):
    STAMINA_DOWN_FIX = "stamina-consumption-down-fix"
    REVIEW_VALUE_MULTIPLE = "review-value-multiple"
    REVIEW_ADDITIVE = "review-additive"


EXPECTED_OCCURRENCES: Final = tuple(
    (
        STAMINA_CARD_ID,
        upgrade,
        1,
        STAMINA_EFFECT_ID,
        STAMINA_TRIGGER_ID,
        TriggeredEffectExecutorFamily.STAMINA_DOWN_FIX,
    )
    for upgrade in range(4)
) + tuple(
    (
        REVIEW_VALUE_CARD_ID,
        upgrade,
        1,
        REVIEW_VALUE_EFFECT_ID,
        REVIEW_VALUE_TRIGGER_ID,
        TriggeredEffectExecutorFamily.REVIEW_VALUE_MULTIPLE,
    )
    for upgrade in range(4)
) + tuple(
    (
        REVIEW_ADDITIVE_CARD_ID,
        upgrade,
        2 if upgrade == 0 else 3,
        REVIEW_ADDITIVE_EFFECT_ID,
        REVIEW_ADDITIVE_TRIGGER_ID,
        TriggeredEffectExecutorFamily.REVIEW_ADDITIVE,
    )
    for upgrade in range(4)
)
EXPECTED_TARGET_REFS: Final = tuple(
    sorted({(card_id, upgrade) for card_id, upgrade, *_rest in EXPECTED_OCCURRENCES})
)


class Plan2NativeTriggeredEffectExecutorError(ValueError):
    """Master linkage or runtime input is outside this exact leaf."""


Runtime: TypeAlias = Plan2StaminaModifierRuntime | Plan2NativeReviewDynamicRuntime
DelegateTransition: TypeAlias = (
    Plan2StaminaModifierExecution
    | Plan2NativeReviewDynamicEventTransition
    | Plan2NativeReviewDynamicInstallTransition
)
PredicateEvaluation: TypeAlias = (
    Plan2NativeEffectTriggerEvaluation | Plan2RemainingEffectTriggerEvaluation
)


@dataclass(frozen=True, slots=True)
class Plan2NativeTriggeredEffectExecutorHandoff:
    family: TriggeredEffectExecutorFamily
    base_trigger: Plan2NativeEffectTriggerHandoff
    remaining_trigger: Plan2RemainingEffectTriggerHandoff | None
    stamina_handoff: Plan2NativeStaminaEffectHandoff | None
    stamina_contract: StaminaConsumptionDownFixEffect | None
    review_program: Plan2NativeReviewDynamicProgram | None
    target_effect_executable: bool = True
    companion_effect_executable_here: bool = False
    whole_card_executable_here: bool = False
    predicate_snapshot: str = "immutable-pre-direct-effect-build"

    @property
    def card_id(self) -> str:
        return self.base_trigger.card_id

    @property
    def upgrade(self) -> int:
        return self.base_trigger.upgrade

    @property
    def slot_index(self) -> int:
        return self.base_trigger.slot_index

    @property
    def ref(self) -> tuple[str, int]:
        return self.base_trigger.ref

    @property
    def occurrence_key(self) -> tuple[str, int, int]:
        return self.base_trigger.occurrence_key

    @property
    def effect_id(self) -> str:
        return self.base_trigger.effect_id

    @property
    def trigger_id(self) -> str:
        return self.base_trigger.trigger.trigger_id

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return self.base_trigger.ordered_effect_ids

    @property
    def effects_before(self) -> tuple[str, ...]:
        return self.base_trigger.effects_before

    @property
    def companion_effect_ids(self) -> tuple[str, ...]:
        return (*self.base_trigger.effects_before, *self.base_trigger.effects_after)


@dataclass(frozen=True, slots=True)
class Plan2NativeTriggeredEffectExecutorAccounting:
    target_occurrence_count: int
    direct_occurrence_count: int
    co_blocked_occurrence_count: int
    target_version_count: int
    direct_version_count: int
    co_blocked_version_count: int
    unique_effect_count: int
    target_refs: tuple[tuple[str, int], ...]
    direct_refs: tuple[tuple[str, int], ...]
    co_blocked_refs: tuple[tuple[str, int], ...]
    family_occurrences: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class Plan2NativeTriggeredEffectExecutorCatalog:
    schema_version: int
    adapter_id: str
    handoffs: tuple[Plan2NativeTriggeredEffectExecutorHandoff, ...]
    accounting: Plan2NativeTriggeredEffectExecutorAccounting

    def occurrence(
        self, card_id: str, upgrade: int, slot_index: int
    ) -> Plan2NativeTriggeredEffectExecutorHandoff:
        matches = tuple(
            row
            for row in self.handoffs
            if row.occurrence_key == (card_id, upgrade, slot_index)
        )
        if len(matches) != 1:
            raise KeyError((card_id, upgrade, slot_index))
        return matches[0]

    def for_version(
        self, card_id: str, upgrade: int
    ) -> tuple[Plan2NativeTriggeredEffectExecutorHandoff, ...]:
        return tuple(row for row in self.handoffs if row.ref == (card_id, upgrade))


def _expected_for(
    handoff: Plan2NativeTriggeredEffectExecutorHandoff,
) -> tuple[str, int, int, str, str, TriggeredEffectExecutorFamily]:
    return (
        handoff.card_id,
        handoff.upgrade,
        handoff.slot_index,
        handoff.effect_id,
        handoff.trigger_id,
        handoff.family,
    )


def _handoff_is_exact(handoff: Plan2NativeTriggeredEffectExecutorHandoff) -> bool:
    if _expected_for(handoff) not in EXPECTED_OCCURRENCES:
        return False
    base = handoff.base_trigger
    if (
        base.ordered_effect_ids[base.slot_index] != base.effect_id
        or not handoff.target_effect_executable
        or handoff.companion_effect_executable_here
        or handoff.whole_card_executable_here
    ):
        return False
    if handoff.family is TriggeredEffectExecutorFamily.STAMINA_DOWN_FIX:
        return (
            base.target_predicate_executable
            and handoff.remaining_trigger is None
            and handoff.review_program is None
            and handoff.stamina_handoff is not None
            and handoff.stamina_handoff.ref == base.ref
            and handoff.stamina_handoff.slot_index == base.slot_index
            and handoff.stamina_handoff.effect_id == base.effect_id
            and handoff.stamina_handoff.owner == CARD_PLAY_STAMINA_TRIGGER_OWNER
            and handoff.stamina_handoff.trigger_id == base.trigger.trigger_id
            and isinstance(handoff.stamina_contract, StaminaConsumptionDownFixEffect)
            and handoff.stamina_contract.effect_id == base.effect_id
            and handoff.stamina_contract.effect_type
            == EFFECT_STAMINA_CONSUMPTION_DOWN_FIX
            and handoff.stamina_contract.value1 == 1
            and handoff.stamina_contract.value2 == 0
            and handoff.stamina_contract.effect_count == 0
            and handoff.stamina_contract.effect_turn == -1
        )
    if handoff.stamina_handoff is not None or handoff.stamina_contract is not None:
        return False
    program = handoff.review_program
    if (
        program is None
        or program.ref != base.ref
        or program.slot_index != base.slot_index
        or program.ordered_effect_ids != base.ordered_effect_ids
        or program.effect_id != base.effect_id
        or program.value1 != base.effect_value1
        or program.value2 != base.effect_value2
        or program.effect_count != base.effect_count
        or program.effect_turn != base.effect_turn
        or program.companion_blockers
    ):
        return False
    if handoff.family is TriggeredEffectExecutorFamily.REVIEW_VALUE_MULTIPLE:
        return (
            base.target_predicate_executable
            and handoff.remaining_trigger is None
            and program.operation_kind == OP_REVIEW_VALUE_MULTIPLE
            and program.value1 == 100
            and program.effect_turn == 0
        )
    remaining = handoff.remaining_trigger
    return (
        handoff.family is TriggeredEffectExecutorFamily.REVIEW_ADDITIVE
        and not base.target_predicate_executable
        and remaining is not None
        and remaining.base == base
        and remaining.target_predicate_executable
        and remaining.threshold == 10
        and program.operation_kind == OP_INSTALL_REVIEW_ADDITIVE
        and program.value1 == 500
        and program.effect_turn == 5
    )


def compile_plan2_native_catalog_triggered_effect_executors(
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeTriggeredEffectExecutorCatalog:
    """Cross-bind exactly the twelve predicate/runtime handoffs."""

    database_path = Path(database).resolve()
    base_catalog = load_plan2_native_catalog_effect_triggers(database_path)
    remaining_catalog = load_plan2_native_catalog_effect_triggers_remaining(
        database_path
    )
    stamina_catalog = compile_plan2_native_catalog_stamina(database=database_path)
    review_compilation = compile_plan2_native_catalog_review_dynamic(
        database=database_path
    )
    review_value_template = next(
        row
        for row in review_compilation.catalog.programs
        if row.effect_id == REVIEW_VALUE_EFFECT_ID
    )
    exact_additive = load_master_effect(REVIEW_ADDITIVE_EFFECT_ID, database_path)

    handoffs: list[Plan2NativeTriggeredEffectExecutorHandoff] = []
    for card_id, upgrade, slot, effect_id, trigger_id, family in EXPECTED_OCCURRENCES:
        base = base_catalog.occurrence(card_id, upgrade, slot)
        if base.effect_id != effect_id or base.trigger.trigger_id != trigger_id:
            raise Plan2NativeTriggeredEffectExecutorError(
                "effect-trigger-linkage-changed"
            )
        remaining: Plan2RemainingEffectTriggerHandoff | None = None
        stamina_handoff: Plan2NativeStaminaEffectHandoff | None = None
        stamina_contract: StaminaConsumptionDownFixEffect | None = None
        review_program: Plan2NativeReviewDynamicProgram | None = None
        if family is TriggeredEffectExecutorFamily.STAMINA_DOWN_FIX:
            matches = stamina_catalog.handoffs_for((card_id, upgrade))
            if len(matches) != 1:
                raise Plan2NativeTriggeredEffectExecutorError(
                    "stamina-owner-cardinality-changed"
                )
            stamina_handoff = matches[0]
            contract = stamina_catalog.effect_by_id.get(effect_id)
            if not isinstance(contract, StaminaConsumptionDownFixEffect):
                raise Plan2NativeTriggeredEffectExecutorError(
                    "stamina-contract-changed"
                )
            stamina_contract = contract
        elif family is TriggeredEffectExecutorFamily.REVIEW_VALUE_MULTIPLE:
            review_program = Plan2NativeReviewDynamicProgram(
                card_id,
                upgrade,
                slot,
                base.ordered_effect_ids,
                effect_id,
                base.effect_type,
                review_value_template.operation_kind,
                base.effect_value1,
                base.effect_value2,
                base.effect_count,
                base.effect_turn,
            )
        else:
            remaining = remaining_catalog.occurrence(card_id, upgrade, slot)
            if (
                exact_additive.id,
                exact_additive.effect_type,
                exact_additive.value1,
                exact_additive.value2,
                exact_additive.effect_count,
                exact_additive.effect_turn,
                exact_additive.status_enchant_id,
                exact_additive.chain_effect_id,
            ) != (
                effect_id,
                base.effect_type,
                base.effect_value1,
                base.effect_value2,
                base.effect_count,
                base.effect_turn,
                "",
                "",
            ):
                raise Plan2NativeTriggeredEffectExecutorError(
                    "review-additive-master-shape-changed"
                )
            review_program = Plan2NativeReviewDynamicProgram(
                card_id,
                upgrade,
                slot,
                base.ordered_effect_ids,
                effect_id,
                base.effect_type,
                OP_INSTALL_REVIEW_ADDITIVE,
                base.effect_value1,
                base.effect_value2,
                base.effect_count,
                base.effect_turn,
            )
        handoff = Plan2NativeTriggeredEffectExecutorHandoff(
            family,
            base,
            remaining,
            stamina_handoff,
            stamina_contract,
            review_program,
        )
        if not _handoff_is_exact(handoff):
            raise Plan2NativeTriggeredEffectExecutorError(
                "cross-owner-contract-changed"
            )
        handoffs.append(handoff)

    if tuple(_expected_for(row) for row in handoffs) != EXPECTED_OCCURRENCES:
        raise Plan2NativeTriggeredEffectExecutorError(
            "target-occurrence-accounting-changed"
        )
    family_counts = tuple(
        (family.value, sum(row.family is family for row in handoffs))
        for family in TriggeredEffectExecutorFamily
    )
    accounting = Plan2NativeTriggeredEffectExecutorAccounting(
        target_occurrence_count=len(handoffs),
        direct_occurrence_count=sum(row.target_effect_executable for row in handoffs),
        co_blocked_occurrence_count=sum(
            not row.target_effect_executable for row in handoffs
        ),
        target_version_count=len({row.ref for row in handoffs}),
        direct_version_count=len({row.ref for row in handoffs if row.target_effect_executable}),
        co_blocked_version_count=len(
            {row.ref for row in handoffs if not row.target_effect_executable}
        ),
        unique_effect_count=len({row.effect_id for row in handoffs}),
        target_refs=EXPECTED_TARGET_REFS,
        direct_refs=EXPECTED_TARGET_REFS,
        co_blocked_refs=(),
        family_occurrences=family_counts,
    )
    if accounting != Plan2NativeTriggeredEffectExecutorAccounting(
        EXPECTED_TARGET_OCCURRENCE_COUNT,
        EXPECTED_DIRECT_OCCURRENCE_COUNT,
        EXPECTED_CO_BLOCKED_OCCURRENCE_COUNT,
        EXPECTED_TARGET_VERSION_COUNT,
        EXPECTED_DIRECT_VERSION_COUNT,
        EXPECTED_CO_BLOCKED_VERSION_COUNT,
        EXPECTED_UNIQUE_EFFECT_COUNT,
        EXPECTED_TARGET_REFS,
        EXPECTED_TARGET_REFS,
        (),
        tuple((family.value, 4) for family in TriggeredEffectExecutorFamily),
    ):
        raise Plan2NativeTriggeredEffectExecutorError(
            "target-direct-co-accounting-changed"
        )
    return Plan2NativeTriggeredEffectExecutorCatalog(
        PLAN2_NATIVE_TRIGGERED_EFFECT_EXECUTORS_SCHEMA_VERSION,
        TRIGGERED_EFFECT_EXECUTORS_ADAPTER_ID,
        tuple(handoffs),
        accounting,
    )


load_plan2_native_catalog_triggered_effect_executors = (
    compile_plan2_native_catalog_triggered_effect_executors
)


@dataclass(frozen=True, slots=True)
class Plan2NativeTriggeredEffectExecutionInput:
    source_guid: object
    phase: object
    boundary: object
    origin: object = EffectTriggerOrigin.NORMAL
    aggressive_snapshot: object | None = None
    current_stamina_snapshot: object | None = None
    max_stamina_snapshot: object | None = None
    review_snapshot: object | None = None
    later_same_card_review: object | None = None
    payment_committed_before_build: object = False
    direct_effects_executed_before_build: object = 0
    accepted: object = True
    context_present: object = True
    status_addition_blocked: object = False


@dataclass(frozen=True, slots=True)
class Plan2NativeTriggeredEffectExecution:
    handoff: Plan2NativeTriggeredEffectExecutorHandoff | None
    before: object
    after: object
    execution_input: Plan2NativeTriggeredEffectExecutionInput
    predicate: PredicateEvaluation | None
    delegate: DelegateTransition | None
    supported: bool
    trigger_fires: bool | None
    slot_skipped: bool
    executed: bool
    applied: bool
    unresolved: tuple[str, ...]
    trace: tuple[str, ...]

    @property
    def fail_closed(self) -> bool:
        return not self.supported

    @property
    def state_unchanged(self) -> bool:
        return self.before is self.after


def _unsupported(
    handoff: Plan2NativeTriggeredEffectExecutorHandoff | None,
    runtime: object,
    execution_input: Plan2NativeTriggeredEffectExecutionInput,
    reasons: tuple[str, ...],
    predicate: PredicateEvaluation | None = None,
) -> Plan2NativeTriggeredEffectExecution:
    return Plan2NativeTriggeredEffectExecution(
        handoff,
        runtime,
        runtime,
        execution_input,
        predicate,
        None,
        False,
        None if predicate is None else predicate.fires,
        False,
        False,
        False,
        tuple(dict.fromkeys(reasons)),
        (),
    )


def execute_plan2_native_triggered_effect(
    runtime: Runtime | object,
    source: Plan2NativeTriggeredEffectExecutorHandoff | object,
    execution_input: Plan2NativeTriggeredEffectExecutionInput,
) -> Plan2NativeTriggeredEffectExecution:
    """Evaluate one exact predicate, then delegate only a true target slot."""

    if not isinstance(execution_input, Plan2NativeTriggeredEffectExecutionInput):
        raise TypeError(
            "execution_input must be Plan2NativeTriggeredEffectExecutionInput"
        )
    if not isinstance(source, Plan2NativeTriggeredEffectExecutorHandoff):
        return _unsupported(
            None,
            runtime,
            execution_input,
            ("unknown-occurrence-failed-closed",),
        )
    handoff = source
    if not _handoff_is_exact(handoff):
        return _unsupported(
            handoff, runtime, execution_input, ("handoff-contract-changed",)
        )

    if handoff.remaining_trigger is None:
        predicate: PredicateEvaluation = evaluate_plan2_native_effect_trigger(
            handoff.base_trigger,
            Plan2NativeEffectTriggerSnapshot(
                phase=execution_input.phase,
                boundary=execution_input.boundary,
                origin=execution_input.origin,
                aggressive=execution_input.aggressive_snapshot,
                current_stamina=execution_input.current_stamina_snapshot,
                max_stamina=execution_input.max_stamina_snapshot,
                review=execution_input.review_snapshot,
                payment_committed_before_build=(
                    execution_input.payment_committed_before_build
                ),
                direct_effects_executed_before_build=(
                    execution_input.direct_effects_executed_before_build
                ),
            ),
        )
    else:
        predicate = evaluate_plan2_remaining_effect_trigger(
            handoff.remaining_trigger,
            Plan2RemainingEffectTriggerSnapshot(
                phase=execution_input.phase,
                boundary=execution_input.boundary,
                origin=execution_input.origin,
                review=execution_input.review_snapshot,
                payment_committed_before_build=(
                    execution_input.payment_committed_before_build
                ),
                direct_effects_executed_before_build=(
                    execution_input.direct_effects_executed_before_build
                ),
                later_same_card_field_value=execution_input.later_same_card_review,
            ),
        )
    if not predicate.supported or type(predicate.fires) is not bool:
        return _unsupported(
            handoff,
            runtime,
            execution_input,
            predicate.reasons or ("predicate-failed-closed",),
            predicate,
        )
    if predicate.fires is False:
        return Plan2NativeTriggeredEffectExecution(
            handoff,
            runtime,
            runtime,
            execution_input,
            predicate,
            None,
            True,
            False,
            True,
            False,
            False,
            (),
            (f"slot:{handoff.slot_index}:trigger-false:skip",),
        )

    if not isinstance(execution_input.source_guid, str) or not execution_input.source_guid:
        return _unsupported(
            handoff, runtime, execution_input, ("source-guid-unknown",), predicate
        )
    for name in ("accepted", "context_present", "status_addition_blocked"):
        value = getattr(execution_input, name)
        if value is not None and type(value) is not bool:
            return _unsupported(
                handoff,
                runtime,
                execution_input,
                (f"{name}-snapshot-shape",),
                predicate,
            )
        if value is None:
            return _unsupported(
                handoff,
                runtime,
                execution_input,
                (f"{name}-unknown",),
                predicate,
            )
    if execution_input.accepted is not True:
        return _unsupported(
            handoff, runtime, execution_input, ("card-not-accepted",), predicate
        )
    if execution_input.context_present is not True:
        return _unsupported(
            handoff, runtime, execution_input, ("context-missing",), predicate
        )
    block_add = execution_input.status_addition_blocked is True

    delegate: DelegateTransition
    applied: bool
    if handoff.family is TriggeredEffectExecutorFamily.STAMINA_DOWN_FIX:
        if not isinstance(runtime, Plan2StaminaModifierRuntime):
            return _unsupported(
                handoff,
                runtime,
                execution_input,
                ("stamina-runtime-shape",),
                predicate,
            )
        assert handoff.stamina_contract is not None
        delegate = apply_plan2_stamina_modifier(
            runtime, handoff.stamina_contract, block_add_status=block_add
        )
        applied = delegate.installed
    else:
        if not isinstance(runtime, Plan2NativeReviewDynamicRuntime):
            return _unsupported(
                handoff,
                runtime,
                execution_input,
                ("review-runtime-shape",),
                predicate,
            )
        assert handoff.review_program is not None
        origin = EffectTriggerOrigin(execution_input.origin).value
        if handoff.family is TriggeredEffectExecutorFamily.REVIEW_VALUE_MULTIPLE:
            delegate = execute_plan2_native_review_dynamic_event(
                runtime,
                handoff.review_program,
                Plan2NativeReviewDynamicEventInput(
                    execution_input.source_guid,
                    origin,
                    handoff.review_program.prior_effect_ids,
                    True,
                    True,
                    block_add,
                ),
            )
            applied = delegate.handoff is not None
        else:
            delegate = install_plan2_native_review_dynamic(
                runtime,
                handoff.review_program,
                Plan2NativeReviewDynamicInstallInput(
                    execution_input.source_guid,
                    origin,
                    handoff.review_program.prior_effect_ids,
                    True,
                    True,
                    block_add,
                ),
            )
            applied = delegate.installed
    if not delegate.executable:
        return _unsupported(
            handoff,
            runtime,
            execution_input,
            delegate.unresolved or ("delegate-failed-closed",),
            predicate,
        )
    return Plan2NativeTriggeredEffectExecution(
        handoff,
        runtime,
        delegate.after,
        execution_input,
        predicate,
        delegate,
        True,
        True,
        False,
        True,
        applied,
        (),
        (
            "predicate-build:immutable-pre-direct-effect-snapshot",
            f"slot:{handoff.slot_index}:trigger-true:delegate",
            *(delegate.trace if hasattr(delegate, "trace") else ()),
        ),
    )


compile_triggered_effect_executors = (
    compile_plan2_native_catalog_triggered_effect_executors
)
execute_triggered_effect = execute_plan2_native_triggered_effect
