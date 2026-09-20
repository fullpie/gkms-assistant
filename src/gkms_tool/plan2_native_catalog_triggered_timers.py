"""Exact owner for the eight triggered Plan2 Timer-chain occurrences.

The central compiler integrates this leaf, while the leaf retains its original
eight-occurrence blocker accounting as immutable provenance.  It binds them to
the exact Review predicates and to the existing relative-StartTurn Timer
runtime.  Predicate decisions use one immutable pre-direct-effect snapshot;
therefore the card's slot-zero Review gain cannot retroactively admit a Timer.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .master_db import DEFAULT_DATABASE
from .plan2_native_catalog_effect_chains import (
    CHILD_LESSON_REVIEW,
    EFFECT_TIMER,
    EXECUTOR_LESSON_REVIEW,
    Plan2NativeEffectChainChildExecution,
    Plan2NativeEffectChainChildInput,
    Plan2NativeEffectChainChildCommand,
    Plan2NativeEffectChainExecutionState,
    Plan2NativeEffectChainInstallInput,
    Plan2NativeEffectChainInstallTransition,
    Plan2NativeEffectChainProgram,
    Plan2NativeEffectChainRuntime,
    Plan2NativeEffectChainStartTurnInput,
    Plan2NativeEffectChainStartTurnTransition,
    compile_plan2_native_catalog_effect_chains,
    execute_plan2_native_effect_chain_child,
    install_plan2_native_effect_chain,
    start_plan2_native_effect_chain_turn,
)
from .plan2_native_catalog_effect_triggers import (
    EffectTriggerOrigin,
    PHASE_NONE,
    Plan2NativeEffectTriggerHandoff,
    load_plan2_native_catalog_effect_triggers,
)
from .plan2_native_catalog_effect_triggers_remaining import (
    Plan2RemainingEffectTriggerEvaluation,
    Plan2RemainingEffectTriggerHandoff,
    Plan2RemainingEffectTriggerSnapshot,
    RemainingEffectTriggerBoundary,
    evaluate_plan2_remaining_effect_trigger,
    load_plan2_native_catalog_effect_triggers_remaining,
)
from .plan2_native_program_catalog import (
    Plan2NativeCatalogBlocker,
    compile_plan2_native_program_catalog,
)
from .plan2_timer_lesson_depend_review import (
    IDO_CARD_ID,
    load_plan2_timer_review_catalog,
)


PLAN2_NATIVE_CATALOG_TRIGGERED_TIMERS_SCHEMA_VERSION: Final = 1
TRIGGERED_TIMER_ADAPTER_ID: Final = "plan2.native.catalog.triggered_timers"
EXPECTED_TARGET_OCCURRENCE_COUNT: Final = 8
EXPECTED_DIRECT_OCCURRENCE_COUNT: Final = 8
EXPECTED_CO_BLOCKED_OCCURRENCE_COUNT: Final = 0
EXPECTED_TARGET_VERSION_COUNT: Final = 4
EXPECTED_DIRECT_VERSION_COUNT: Final = 4
EXPECTED_CO_BLOCKED_VERSION_COUNT: Final = 0

_DIRECT_REVIEW_EFFECT_ID: Final = "e_effect-exam_review-0002"
_VALUES: Final = ((3000, 5000), (4400, 5500), (4600, 5800), (4900, 6100))


def _child_id(value: int) -> str:
    return f"e_effect-exam_lesson_depend_exam_review-{value:04d}-01"


def _timer_id(delay: int, value: int) -> str:
    return f"e_effect-exam_effect_timer-{delay:04d}-01-{_child_id(value)}"


EXPECTED_OCCURRENCES: Final = tuple(
    (
        IDO_CARD_ID,
        upgrade,
        slot_index,
        _timer_id(slot_index, value),
        f"e_trigger-none-review_up-{10 if slot_index == 1 else 15}",
    )
    for upgrade, values in enumerate(_VALUES)
    for slot_index, value in enumerate(values, start=1)
)
EXPECTED_TARGET_REFS: Final = tuple((IDO_CARD_ID, upgrade) for upgrade in range(4))


class Plan2NativeTriggeredTimerContractError(ValueError):
    """Current compiler, Master linkage, or exact owner changed."""


@dataclass(frozen=True, slots=True)
class Plan2NativeTriggeredTimerHandoff:
    base_trigger: Plan2NativeEffectTriggerHandoff
    predicate: Plan2RemainingEffectTriggerHandoff
    timer_program: Plan2NativeEffectChainProgram
    compiler_blocker: Plan2NativeCatalogBlocker
    target_executable: bool = True
    companion_effect_executable_here: bool = False
    whole_card_executable_here: bool = False
    predicate_snapshot: str = "pre-payment-direct-effect-build"
    timer_install_boundary: str = "Playing/direct-effect-slot"

    @property
    def card_id(self) -> str:
        return self.timer_program.card_id

    @property
    def upgrade(self) -> int:
        return self.timer_program.upgrade

    @property
    def slot_index(self) -> int:
        return self.timer_program.slot_index

    @property
    def ref(self) -> tuple[str, int]:
        return self.timer_program.ref

    @property
    def occurrence_key(self) -> tuple[str, int, int]:
        return self.card_id, self.upgrade, self.slot_index

    @property
    def timer_effect_id(self) -> str:
        return self.timer_program.timer_effect_id

    @property
    def trigger_effect_ids(self) -> tuple[str, ...]:
        return self.base_trigger.trigger_effect_ids

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return self.timer_program.ordered_effect_ids

    @property
    def effects_before(self) -> tuple[str, ...]:
        return self.timer_program.prior_effect_ids


@dataclass(frozen=True, slots=True)
class Plan2NativeTriggeredTimerVersion:
    card_id: str
    upgrade: int
    ordered_effect_ids: tuple[str, ...]
    timers: tuple[Plan2NativeTriggeredTimerHandoff, ...]
    current_compiler_blockers: tuple[Plan2NativeCatalogBlocker, ...]
    direct_after_this_leaf: bool
    whole_card_executable_here: bool = False

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade


@dataclass(frozen=True, slots=True)
class Plan2NativeTriggeredTimerAccounting:
    target_occurrence_count: int
    direct_occurrence_count: int
    co_blocked_occurrence_count: int
    target_version_count: int
    direct_version_count: int
    co_blocked_version_count: int
    target_refs: tuple[tuple[str, int], ...]
    direct_refs: tuple[tuple[str, int], ...]
    co_blocked_refs: tuple[tuple[str, int], ...]
    blocker_code_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class Plan2NativeTriggeredTimerCatalog:
    schema_version: int
    adapter_id: str
    versions: tuple[Plan2NativeTriggeredTimerVersion, ...]
    accounting: Plan2NativeTriggeredTimerAccounting

    @property
    def handoffs(self) -> tuple[Plan2NativeTriggeredTimerHandoff, ...]:
        return tuple(timer for version in self.versions for timer in version.timers)

    def version(self, card_id: str, upgrade: int) -> Plan2NativeTriggeredTimerVersion:
        matches = tuple(row for row in self.versions if row.ref == (card_id, upgrade))
        if len(matches) != 1:
            raise KeyError((card_id, upgrade))
        return matches[0]

    def occurrence(
        self, card_id: str, upgrade: int, slot_index: int
    ) -> Plan2NativeTriggeredTimerHandoff:
        matches = tuple(
            row
            for row in self.handoffs
            if row.occurrence_key == (card_id, upgrade, slot_index)
        )
        if len(matches) != 1:
            raise KeyError((card_id, upgrade, slot_index))
        return matches[0]


def _raise(code: str) -> None:
    raise Plan2NativeTriggeredTimerContractError(code)


def _handoff_is_exact(handoff: Plan2NativeTriggeredTimerHandoff) -> bool:
    base = handoff.base_trigger
    predicate = handoff.predicate
    program = handoff.timer_program
    blocker = handoff.compiler_blocker
    expected = (
        handoff.card_id,
        handoff.upgrade,
        handoff.slot_index,
        handoff.timer_effect_id,
        predicate.trigger_id,
    )
    if expected not in EXPECTED_OCCURRENCES:
        return False
    expected_value = _VALUES[handoff.upgrade][handoff.slot_index - 1]
    expected_order = (
        _DIRECT_REVIEW_EFFECT_ID,
        _timer_id(1, _VALUES[handoff.upgrade][0]),
        _timer_id(2, _VALUES[handoff.upgrade][1]),
    )
    return (
        base == predicate.base
        and base.occurrence_key == handoff.occurrence_key
        and base.effect_id == program.timer_effect_id
        and base.effect_type == EFFECT_TIMER
        and base.effect_value1 == handoff.slot_index
        and base.effect_value2 == 0
        and base.effect_count == 1
        and base.effect_turn == 0
        and base.chain_effect_id == _child_id(expected_value)
        and base.ordered_effect_ids == program.ordered_effect_ids == expected_order
        and base.trigger_effect_ids == (predicate.trigger_id,)
        and predicate.threshold == (10 if handoff.slot_index == 1 else 15)
        and predicate.supported_boundaries
        == (RemainingEffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,)
        and predicate.target_predicate_executable
        and not predicate.companion_effect_executable_here
        and not predicate.whole_card_executable
        and program.executable
        and program.delay == handoff.slot_index
        and program.effect_count == 1
        and program.effect_turn == 0
        and program.child_effect_id == _child_id(expected_value)
        and program.child_effect_type == CHILD_LESSON_REVIEW
        and program.child_value1 == expected_value
        and program.child_value2 == 0
        and program.child_count == 1
        and program.child_turn == 0
        and program.child_executor == EXECUTOR_LESSON_REVIEW
        and blocker.code == "effect-chain-runtime-unbound"
        and blocker.ref == program.ref
        and blocker.detail == program.timer_effect_id
        and handoff.target_executable
        and not handoff.companion_effect_executable_here
        and not handoff.whole_card_executable_here
    )


def compile_plan2_native_catalog_triggered_timers(
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeTriggeredTimerCatalog:
    """Bind exactly the central compiler's eight triggered Timer blockers."""

    database_path = Path(database).resolve()
    central = compile_plan2_native_program_catalog(database=database_path)
    target_blockers = tuple(
        blocker
        for blocker in central.blockers
        if blocker.code == "effect-chain-runtime-unbound"
    )
    observed_blockers = tuple(
        (row.card_id, row.upgrade, row.detail) for row in target_blockers
    )
    expected_blockers = tuple(
        (card_id, upgrade, timer_id)
        for card_id, upgrade, _slot, timer_id, _trigger in EXPECTED_OCCURRENCES
    )
    if observed_blockers == ():
        target_blockers = tuple(
            Plan2NativeCatalogBlocker(
                "effect-chain-runtime-unbound",
                card_id,
                upgrade,
                timer_id,
            )
            for card_id, upgrade, timer_id in expected_blockers
        )
    elif observed_blockers != expected_blockers:
        _raise("current-compiler-triggered-timer-blockers-changed")

    direct_chains = compile_plan2_native_catalog_effect_chains(database=database_path)
    if any(
        program.ref in EXPECTED_TARGET_REFS
        for program in direct_chains.catalog.programs
    ):
        _raise("direct-chain-owner-overlap")

    base_catalog = load_plan2_native_catalog_effect_triggers(database_path)
    predicate_catalog = load_plan2_native_catalog_effect_triggers_remaining(
        database_path
    )
    timer_catalog = load_plan2_timer_review_catalog(database=database_path)
    blockers_by_key = {
        (row.card_id, row.upgrade, row.detail): row for row in target_blockers
    }

    handoffs: list[Plan2NativeTriggeredTimerHandoff] = []
    for card_id, upgrade, slot_index, timer_id, trigger_id in EXPECTED_OCCURRENCES:
        base = base_catalog.occurrence(card_id, upgrade, slot_index)
        predicate = predicate_catalog.occurrence(card_id, upgrade, slot_index)
        version = timer_catalog.version(card_id, upgrade)
        timer = next(
            (row for row in version.timers if row.slot_index == slot_index), None
        )
        if timer is None:
            _raise("exact-timer-owner-missing")
        ordered_effect_ids = tuple(effect.id for effect in version.card.effects)
        child = timer.child.effect
        program = Plan2NativeEffectChainProgram(
            card_id=card_id,
            upgrade=upgrade,
            slot_index=slot_index,
            ordered_effect_ids=ordered_effect_ids,
            timer_effect_id=timer.effect.id,
            delay=timer.delay,
            effect_count=timer.effect.effect_count,
            effect_turn=timer.effect.effect_turn,
            child_effect_id=child.id,
            child_effect_type=child.effect_type,
            child_value1=child.value1,
            child_value2=child.value2,
            child_count=child.effect_count,
            child_turn=child.effect_turn,
            child_executor=EXECUTOR_LESSON_REVIEW,
        )
        handoff = Plan2NativeTriggeredTimerHandoff(
            base,
            predicate,
            program,
            blockers_by_key[(card_id, upgrade, timer_id)],
        )
        if (
            timer.effect.id != timer_id
            or timer.trigger_id != trigger_id
            or timer.trigger_threshold != (10 if slot_index == 1 else 15)
            or not _handoff_is_exact(handoff)
        ):
            _raise("triggered-timer-owner-linkage-changed")
        handoffs.append(handoff)

    if tuple(
        (
            row.card_id,
            row.upgrade,
            row.slot_index,
            row.timer_effect_id,
            row.predicate.trigger_id,
        )
        for row in handoffs
    ) != EXPECTED_OCCURRENCES:
        _raise("target-occurrence-accounting-changed")

    versions: list[Plan2NativeTriggeredTimerVersion] = []
    for ref in EXPECTED_TARGET_REFS:
        timers = tuple(row for row in handoffs if row.ref == ref)
        blockers = tuple(row for row in target_blockers if row.ref == ref)
        direct = len(timers) == 2 and blockers == tuple(
            row.compiler_blocker for row in timers
        )
        versions.append(
            Plan2NativeTriggeredTimerVersion(
                ref[0], ref[1], timers[0].ordered_effect_ids, timers, blockers, direct
            )
        )
    direct_refs = tuple(row.ref for row in versions if row.direct_after_this_leaf)
    co_blocked_refs = tuple(row.ref for row in versions if not row.direct_after_this_leaf)
    accounting = Plan2NativeTriggeredTimerAccounting(
        len(handoffs),
        sum(row.target_executable for row in handoffs),
        sum(not row.target_executable for row in handoffs),
        len(versions),
        len(direct_refs),
        len(co_blocked_refs),
        EXPECTED_TARGET_REFS,
        direct_refs,
        co_blocked_refs,
        tuple(sorted(Counter(row.code for row in target_blockers).items())),
    )
    if (
        accounting.target_occurrence_count != EXPECTED_TARGET_OCCURRENCE_COUNT
        or accounting.direct_occurrence_count != EXPECTED_DIRECT_OCCURRENCE_COUNT
        or accounting.co_blocked_occurrence_count
        != EXPECTED_CO_BLOCKED_OCCURRENCE_COUNT
        or accounting.target_version_count != EXPECTED_TARGET_VERSION_COUNT
        or accounting.direct_version_count != EXPECTED_DIRECT_VERSION_COUNT
        or accounting.co_blocked_version_count != EXPECTED_CO_BLOCKED_VERSION_COUNT
    ):
        _raise("target-direct-co-accounting-changed")
    return Plan2NativeTriggeredTimerCatalog(
        PLAN2_NATIVE_CATALOG_TRIGGERED_TIMERS_SCHEMA_VERSION,
        TRIGGERED_TIMER_ADAPTER_ID,
        tuple(versions),
        accounting,
    )


load_plan2_native_catalog_triggered_timers = (
    compile_plan2_native_catalog_triggered_timers
)


@dataclass(frozen=True, slots=True)
class Plan2NativeTriggeredTimerBuildInput:
    source_guid: object
    review_snapshot: object
    origin: object = EffectTriggerOrigin.NORMAL
    phase: object = PHASE_NONE
    boundary: object = RemainingEffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD
    payment_committed_before_build: object = False
    direct_effects_executed_before_build: object = 0
    later_same_card_review: object | None = None
    accepted: object = True
    install_phase_known: object = True


@dataclass(frozen=True, slots=True)
class Plan2NativeTriggeredTimerSlotTransition:
    handoff: Plan2NativeTriggeredTimerHandoff
    predicate: Plan2RemainingEffectTriggerEvaluation
    install: Plan2NativeEffectChainInstallTransition | None
    trigger_fires: bool | None
    slot_skipped: bool
    installed: bool
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan2NativeTriggeredTimerBuildTransition:
    before: Plan2NativeEffectChainRuntime
    after: Plan2NativeEffectChainRuntime
    version: Plan2NativeTriggeredTimerVersion | None
    execution_input: Plan2NativeTriggeredTimerBuildInput
    supported: bool
    slots: tuple[Plan2NativeTriggeredTimerSlotTransition, ...]
    unresolved: tuple[str, ...]
    trace: tuple[str, ...]

    @property
    def fail_closed(self) -> bool:
        return not self.supported


def _invalid_input_reason(value: Plan2NativeTriggeredTimerBuildInput) -> str | None:
    if not isinstance(value.source_guid, str) or not value.source_guid:
        return "source-guid-unknown"
    try:
        EffectTriggerOrigin(value.origin)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "card-origin-unknown"
    for name in ("accepted", "install_phase_known"):
        field = getattr(value, name)
        if field is not None and type(field) is not bool:
            return f"{name}-snapshot-shape"
    if value.accepted is None:
        return "accepted-unknown"
    if value.accepted is not True:
        return "card-not-accepted"
    if value.install_phase_known is None:
        return "install-phase-unknown"
    if value.install_phase_known is not True:
        return "install-phase-missing"
    return None


def build_plan2_native_triggered_timer_slots(
    runtime: Plan2NativeEffectChainRuntime,
    version: Plan2NativeTriggeredTimerVersion | object,
    execution_input: Plan2NativeTriggeredTimerBuildInput,
) -> Plan2NativeTriggeredTimerBuildTransition:
    """Build both predicates from one snapshot, then install only true slots."""

    if not isinstance(runtime, Plan2NativeEffectChainRuntime):
        raise TypeError("runtime must be Plan2NativeEffectChainRuntime")
    if not isinstance(execution_input, Plan2NativeTriggeredTimerBuildInput):
        raise TypeError("execution_input must be Plan2NativeTriggeredTimerBuildInput")
    if not isinstance(version, Plan2NativeTriggeredTimerVersion):
        return Plan2NativeTriggeredTimerBuildTransition(
            runtime,
            runtime,
            None,
            execution_input,
            False,
            (),
            ("unknown-version-failed-closed",),
            (),
        )
    if (
        version.ref not in EXPECTED_TARGET_REFS
        or tuple(row.slot_index for row in version.timers) != (1, 2)
        or any(not _handoff_is_exact(row) for row in version.timers)
        or any(row.ref != version.ref for row in version.timers)
        or any(row.ordered_effect_ids != version.ordered_effect_ids for row in version.timers)
        or version.ordered_effect_ids[0] != _DIRECT_REVIEW_EFFECT_ID
        or not version.direct_after_this_leaf
        or version.current_compiler_blockers
        != tuple(row.compiler_blocker for row in version.timers)
    ):
        return Plan2NativeTriggeredTimerBuildTransition(
            runtime,
            runtime,
            version,
            execution_input,
            False,
            (),
            ("version-contract-changed",),
            (),
        )
    invalid = _invalid_input_reason(execution_input)
    if invalid is not None:
        return Plan2NativeTriggeredTimerBuildTransition(
            runtime, runtime, version, execution_input, False, (), (invalid,), ()
        )

    snapshot = Plan2RemainingEffectTriggerSnapshot(
        phase=execution_input.phase,
        boundary=execution_input.boundary,
        origin=execution_input.origin,
        review=execution_input.review_snapshot,
        payment_committed_before_build=execution_input.payment_committed_before_build,
        direct_effects_executed_before_build=(
            execution_input.direct_effects_executed_before_build
        ),
        later_same_card_field_value=execution_input.later_same_card_review,
    )
    evaluations = tuple(
        evaluate_plan2_remaining_effect_trigger(row.predicate, snapshot)
        for row in version.timers
    )
    if any(not row.supported or type(row.fires) is not bool for row in evaluations):
        slots = tuple(
            Plan2NativeTriggeredTimerSlotTransition(
                handoff,
                evaluation,
                None,
                evaluation.fires,
                bool(evaluation.supported and evaluation.fires is False),
                False,
                evaluation.reasons or ("predicate-build-failed-closed",),
            )
            for handoff, evaluation in zip(version.timers, evaluations, strict=True)
        )
        reasons = tuple(
            dict.fromkeys(reason for slot in slots for reason in slot.unresolved)
        )
        return Plan2NativeTriggeredTimerBuildTransition(
            runtime, runtime, version, execution_input, False, slots, reasons, ()
        )

    after = runtime
    slots: list[Plan2NativeTriggeredTimerSlotTransition] = []
    trace: list[str] = [
        "predicate-build:pre-direct-effect-sequence:immutable-review-snapshot"
    ]
    for handoff, evaluation in zip(version.timers, evaluations, strict=True):
        if evaluation.fires is False:
            slots.append(
                Plan2NativeTriggeredTimerSlotTransition(
                    handoff, evaluation, None, False, True, False
                )
            )
            trace.append(f"slot:{handoff.slot_index}:trigger-false:skip")
            continue
        install = install_plan2_native_effect_chain(
            after,
            handoff.timer_program,
            Plan2NativeEffectChainInstallInput(
                source_guid=execution_input.source_guid,
                play_origin=EffectTriggerOrigin(execution_input.origin).value,
                completed_effect_ids=handoff.timer_program.prior_effect_ids,
                accepted=True,
                install_phase_known=True,
            ),
        )
        if not install.executable or install.installed is None:
            return Plan2NativeTriggeredTimerBuildTransition(
                runtime,
                runtime,
                version,
                execution_input,
                False,
                tuple(slots),
                install.unresolved or ("timer-install-failed-closed",),
                (),
            )
        slots.append(
            Plan2NativeTriggeredTimerSlotTransition(
                handoff, evaluation, install, True, False, True
            )
        )
        after = install.after
        trace.extend(install.trace)
    return Plan2NativeTriggeredTimerBuildTransition(
        runtime, after, version, execution_input, True, tuple(slots), (), tuple(trace)
    )


def start_plan2_native_triggered_timer_turn(
    runtime: Plan2NativeEffectChainRuntime,
    turn_input: Plan2NativeEffectChainStartTurnInput = Plan2NativeEffectChainStartTurnInput(),
) -> Plan2NativeEffectChainStartTurnTransition:
    """Delegate relative StartTurn countdown and ordered child queuing."""

    return start_plan2_native_effect_chain_turn(runtime, turn_input)


def execute_plan2_native_triggered_timer_child(
    state: Plan2NativeEffectChainExecutionState,
    command: Plan2NativeEffectChainChildCommand,
    child_input: Plan2NativeEffectChainChildInput = Plan2NativeEffectChainChildInput(),
) -> Plan2NativeEffectChainChildExecution:
    """Delegate the exact LessonDependReview expiry child executor."""

    return execute_plan2_native_effect_chain_child(state, command, child_input)


compile_triggered_timers = compile_plan2_native_catalog_triggered_timers
build_triggered_timer_slots = build_plan2_native_triggered_timer_slots
advance_triggered_timer_start_turn = start_plan2_native_triggered_timer_turn
execute_triggered_timer_child = execute_plan2_native_triggered_timer_child
