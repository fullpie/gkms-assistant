"""Exact immutable executors for the remaining Plan2 status-enchant children.

The parent status owner captures listeners, spends limits, and emits an
ordered command queue.  This leaf validates that handoff and executes only the
20 currently unbound child occurrences.  Caller-owned GUIDs and execution
snapshots are never inferred; a missing input rolls back both parent spending
and all child mutations.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Final, Literal, TypeAlias

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    AddingParameterAdditionalData,
    AddingParameterSettings,
    AddingParameterStatus,
    ParameterApplication,
    ParameterApplicationStatus,
    apply_parameter_add,
    calculate_adding_parameter,
    ceil_f32_to_i32,
    f32,
    permille_to_f32,
)
from .plan2_block_depend_block_consumption_sum import (
    Plan2BlockDependConsumptionSumRuntime,
    apply_plan2_set_block,
    execute_plan2_block_depend_consumption_sum_child,
    load_plan2_block_depend_consumption_sum_program,
)
from .plan2_card_create_id import (
    apply_plan2_card_create_id,
    resolve_plan2_card_create_contract,
)
from .plan2_card_move_remaining import (
    RemainingCardMoveExecutionContext,
    RemainingCardMoveHandoff,
    RemainingCardMoveState,
    load_remaining_card_move_contracts,
    simulate_remaining_card_move,
)
from .plan2_end_turn_trigger import _lesson_depend_block_raw_score
from .plan2_force_play_search import (
    contract_for_effect,
    execute_plan2_force_play_queue,
    plan_force_play_card_search,
)
from .plan2_native_catalog_block_dynamic import (
    Plan2NativeDynamicBlockExecutionInput,
    Plan2NativeDynamicBlockProgram,
    Plan2NativeDynamicBlockRuntime,
    execute_plan2_native_dynamic_block,
)
from .plan2_native_catalog_stamina_recover_multiple import (
    Plan2NativeStaminaRecoverMultipleEffect,
    Plan2NativeStaminaRecoverMultipleSnapshot,
    evaluate_stamina_recover_multiple,
)
from .plan2_native_catalog_status_enchant import (
    Plan2NativeStatusEnchantEvent,
    Plan2NativeStatusEnchantRuntime,
    Plan2NativeStatusEnchantTransition,
    Plan2StatusEnchantChildCommand,
    Plan2StatusEnchantChildProgram,
    Plan2StatusEnchantInstallProgram,
    compile_plan2_native_catalog_status_enchant,
    resolve_plan2_native_status_enchant_event,
)
from .plan2_review_count_add_interval3 import (
    Plan2ReviewCountAddInterval3State,
    _apply_review_count_add_child,
    load_plan2_review_count_add_interval3_program,
)
from .plan2_state import Plan2State
from .plan3_native_state import Plan3NativeState


SCHEMA_VERSION: Final = 1

LESSON_DEPEND_BLOCK: Final = "ProduceExamEffectType_ExamLessonDependBlock"
STAMINA_RECOVER_MULTIPLE: Final = (
    "ProduceExamEffectType_ExamStaminaRecoverMultiple"
)
CARD_CREATE_ID: Final = "ProduceExamEffectType_ExamCardCreateId"
REVIEW_COUNT_ADD: Final = "ProduceExamEffectType_ExamReviewCountAdd"
BLOCK_DEPEND_CONSUMPTION: Final = (
    "ProduceExamEffectType_ExamBlockDependBlockConsumptionSum"
)
CARD_MOVE: Final = "ProduceExamEffectType_ExamCardMove"
CARD_DRAW: Final = "ProduceExamEffectType_ExamCardDraw"
FORCE_PLAY: Final = "ProduceExamEffectType_ExamForcePlayCardSearch"
BLOCK_ADD_MULTIPLE_AGGRESSIVE: Final = (
    "ProduceExamEffectType_ExamBlockAddMultipleAggressive"
)

TARGET_CHILD_TYPES: Final = frozenset(
    (
        STAMINA_RECOVER_MULTIPLE,
        CARD_CREATE_ID,
        REVIEW_COUNT_ADD,
        BLOCK_DEPEND_CONSUMPTION,
        CARD_MOVE,
        CARD_DRAW,
        FORCE_PLAY,
        BLOCK_ADD_MULTIPLE_AGGRESSIVE,
    )
)
QUEUE_CHILD_TYPES: Final = frozenset((*TARGET_CHILD_TYPES, LESSON_DEPEND_BLOCK))

EXPECTED_TARGET_OCCURRENCE_COUNT: Final = 20
EXPECTED_VERSION_COUNT: Final = 19
EXPECTED_TARGET_EFFECT_TYPE_COUNT: Final = 8
EXPECTED_QUEUE_CHILD_COUNT: Final = 24
EXPECTED_WHOLE_CARD_DIRECT_COUNT: Final = 19
EXPECTED_WHOLE_CARD_CO_BLOCKED_COUNT: Final = 0
EXPECTED_COMPANION_BLOCKER_COUNT: Final = 0
EXPECTED_TYPE_COUNTS: Final = MappingProxyType(
    {
        STAMINA_RECOVER_MULTIPLE: 4,
        CARD_CREATE_ID: 4,
        REVIEW_COUNT_ADD: 4,
        BLOCK_DEPEND_CONSUMPTION: 4,
        CARD_MOVE: 1,
        CARD_DRAW: 1,
        FORCE_PLAY: 1,
        BLOCK_ADD_MULTIPLE_AGGRESSIVE: 1,
    }
)

Operation: TypeAlias = Literal[
    "lesson-depend-block",
    "stamina-recover-multiple",
    "card-create-id",
    "review-count-add",
    "block-depend-consumption",
    "card-move",
    "card-draw",
    "force-play",
    "block-add-multiple-aggressive",
]


class Plan2NativeStatusChildError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeStatusChildBlocker:
    code: str
    card_id: str
    upgrade: int
    installer_effect_id: str = ""
    child_effect_id: str = ""
    detail: str = ""

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusChildBinding:
    install: Plan2StatusEnchantInstallProgram
    child_index: int
    child: Plan2StatusEnchantChildProgram
    operation: Operation
    target: bool

    def __post_init__(self) -> None:
        if self.child_index >= len(self.install.children):
            raise Plan2NativeStatusChildError("child-index-out-of-range")
        if self.install.children[self.child_index] != self.child:
            raise Plan2NativeStatusChildError("child-order-drift", self.child.effect_id)

    @property
    def ref(self) -> tuple[str, int]:
        return self.install.ref


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusChildCatalog:
    bindings: tuple[Plan2NativeStatusChildBinding, ...]

    def for_install(
        self, card_id: str, upgrade: int, installer_effect_id: str
    ) -> tuple[Plan2NativeStatusChildBinding, ...]:
        return tuple(
            item
            for item in self.bindings
            if item.ref == (card_id, upgrade)
            and item.install.installer_effect_id == installer_effect_id
        )

    def for_command(
        self, command: Plan2StatusEnchantChildCommand
    ) -> Plan2NativeStatusChildBinding:
        found = tuple(
            item
            for item in self.bindings
            if item.ref == (command.source_card_id, command.source_upgrade)
            and item.install.installer_effect_id == command.installer_effect_id
            and item.install.status_enchant_id == command.status_enchant_id
            and item.child_index == command.child_sequence
            and item.child == command.child
        )
        if len(found) != 1:
            raise Plan2NativeStatusChildError(
                "unknown-status-child-handoff", command.child.effect_id
            )
        return found[0]


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusChildCompilation:
    schema_version: int
    database: str
    catalog: Plan2NativeStatusChildCatalog
    blockers: tuple[Plan2NativeStatusChildBlocker, ...] = ()
    companion_blockers: tuple[Plan2NativeStatusChildBlocker, ...] = ()
    whole_card_direct_refs: tuple[tuple[str, int], ...] = ()
    whole_card_co_blocked_refs: tuple[tuple[str, int], ...] = ()

    @property
    def fully_compiled(self) -> bool:
        return not self.blockers

    @property
    def target_bindings(self) -> tuple[Plan2NativeStatusChildBinding, ...]:
        return tuple(item for item in self.catalog.bindings if item.target)

    @property
    def target_occurrence_count(self) -> int:
        return len(self.target_bindings)

    @property
    def queue_child_count(self) -> int:
        return len(self.catalog.bindings)

    @property
    def affected_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted({item.ref for item in self.target_bindings}))

    @property
    def target_type_counts(self) -> Mapping[str, int]:
        return dict(Counter(item.child.effect_type for item in self.target_bindings))

    @property
    def version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def whole_card_direct_count(self) -> int:
        return len(self.whole_card_direct_refs)

    @property
    def whole_card_co_blocked_count(self) -> int:
        return len(self.whole_card_co_blocked_refs)

    @property
    def companion_blocker_count(self) -> int:
        return len(self.companion_blockers)


_OPERATION_BY_TYPE: Mapping[str, Operation] = {
    LESSON_DEPEND_BLOCK: "lesson-depend-block",
    STAMINA_RECOVER_MULTIPLE: "stamina-recover-multiple",
    CARD_CREATE_ID: "card-create-id",
    REVIEW_COUNT_ADD: "review-count-add",
    BLOCK_DEPEND_CONSUMPTION: "block-depend-consumption",
    CARD_MOVE: "card-move",
    CARD_DRAW: "card-draw",
    FORCE_PLAY: "force-play",
    BLOCK_ADD_MULTIPLE_AGGRESSIVE: "block-add-multiple-aggressive",
}


def _effect_rows(database: Path) -> dict[str, sqlite3.Row]:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return {
            str(row["id"]): row
            for row in connection.execute("SELECT * FROM effect").fetchall()
        }


def _validate_child(
    binding: Plan2NativeStatusChildBinding,
    *,
    rows: Mapping[str, sqlite3.Row],
    database: Path,
) -> None:
    child = binding.child
    if binding.operation == "lesson-depend-block":
        if not (
            child.value1 in {300, 400}
            and child.value2 == 0
            and child.count == 1
            and child.turn == 0
        ):
            raise Plan2NativeStatusChildError("lesson-child-shape-drift")
    elif binding.operation == "stamina-recover-multiple":
        Plan2NativeStaminaRecoverMultipleEffect.from_mapping(
            dict(rows[child.effect_id])
        )
    elif binding.operation == "card-create-id":
        contract = resolve_plan2_card_create_contract(
            rows[child.effect_id], plan_type="ProducePlanType_Plan2"
        )
        if (
            contract.target_card_id != child.target_card_id
            or contract.upgrade != child.target_upgrade
            or contract.count_min != 1
            or contract.count_max != 1
            or child.count != 0
        ):
            raise Plan2NativeStatusChildError("create-id-child-shape-drift")
    elif binding.operation == "review-count-add":
        exact = load_plan2_review_count_add_interval3_program(database).child
        if (exact.effect_id, exact.value1, exact.turn) != (
            child.effect_id,
            child.value1,
            child.turn,
        ):
            raise Plan2NativeStatusChildError("review-count-add-child-shape-drift")
    elif binding.operation == "block-depend-consumption":
        exact = load_plan2_block_depend_consumption_sum_program(database).effects[0]
        if (exact.effect_id, exact.value1, exact.count) != (
            child.effect_id,
            child.value1,
            child.count,
        ):
            raise Plan2NativeStatusChildError("block-consumption-child-shape-drift")
    elif binding.operation == "card-move":
        exact = next(
            item
            for item in load_remaining_card_move_contracts(database)
            if item.effect_id == child.effect_id
            and item.handoff_kind == "status-trigger-child"
        )
        if exact.effect_index != binding.child_index:
            raise Plan2NativeStatusChildError("card-move-child-order-drift")
    elif binding.operation == "card-draw":
        if not (
            child.value1 == 1
            and child.value2 == child.count == child.turn == 0
            and not child.card_search_id
        ):
            raise Plan2NativeStatusChildError("card-draw-child-shape-drift")
    elif binding.operation == "force-play":
        exact = contract_for_effect(child.effect_id, database)
        if not exact.executable:
            raise Plan2NativeStatusChildError("force-play-child-unresolved")
    elif binding.operation == "block-add-multiple-aggressive":
        Plan2NativeDynamicBlockProgram(
            binding.install.card_id,
            binding.install.upgrade,
            0,
            (child.effect_id,),
            child.effect_id,
            child.effect_type,
            child.value1,
            child.value2,
            child.count,
            child.turn,
            "none",
        )


def compile_plan2_native_catalog_status_children(
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeStatusChildCompilation:
    """Compile the exact 20-child slice without importing coverage layers."""

    database = Path(database).resolve()
    parent = compile_plan2_native_catalog_status_enchant(database=database)
    rows = _effect_rows(database)
    candidates: list[Plan2NativeStatusChildBinding] = []
    blockers: list[Plan2NativeStatusChildBlocker] = []
    for version in parent.catalog.programs:
        for install in version.installs:
            if not any(child.effect_type in TARGET_CHILD_TYPES for child in install.children):
                continue
            for index, child in enumerate(install.children):
                operation = _OPERATION_BY_TYPE.get(child.effect_type)
                if operation is None:
                    blockers.append(
                        Plan2NativeStatusChildBlocker(
                            "unknown-queued-child-type",
                            *install.ref,
                            install.installer_effect_id,
                            child.effect_id,
                            child.effect_type,
                        )
                    )
                    continue
                binding = Plan2NativeStatusChildBinding(
                    install, index, child, operation, child.effect_type in TARGET_CHILD_TYPES
                )
                try:
                    _validate_child(binding, rows=rows, database=database)
                except (Exception,) as error:
                    blockers.append(
                        Plan2NativeStatusChildBlocker(
                            "child-contract-unresolved",
                            *install.ref,
                            install.installer_effect_id,
                            child.effect_id,
                            str(error),
                        )
                    )
                else:
                    candidates.append(binding)

    catalog = Plan2NativeStatusChildCatalog(tuple(candidates))
    target_refs = tuple(sorted({item.ref for item in candidates if item.target}))
    observed_type_counts = dict(
        Counter(
            item.child.effect_type for item in candidates if item.target
        )
    )
    observed = (
        sum(item.target for item in candidates),
        len(target_refs),
        len(observed_type_counts),
        len(candidates),
    )
    expected = (
        EXPECTED_TARGET_OCCURRENCE_COUNT,
        EXPECTED_VERSION_COUNT,
        EXPECTED_TARGET_EFFECT_TYPE_COUNT,
        EXPECTED_QUEUE_CHILD_COUNT,
    )
    if observed != expected or observed_type_counts != EXPECTED_TYPE_COUNTS:
        blockers.append(
            Plan2NativeStatusChildBlocker(
                "target-inventory-drift",
                "<catalog>",
                0,
                detail=f"observed={observed!r};types={observed_type_counts!r}",
            )
        )
    result = Plan2NativeStatusChildCompilation(
        SCHEMA_VERSION,
        str(database),
        catalog,
        tuple(sorted(blockers)),
        (),
        target_refs,
        (),
    )
    if database == Path(DEFAULT_DATABASE).resolve() and not blockers:
        observed = (
            result.target_occurrence_count,
            len(result.affected_refs),
            len(result.target_type_counts),
            result.queue_child_count,
            len(result.whole_card_direct_refs),
            len(result.whole_card_co_blocked_refs),
            len(result.companion_blockers),
        )
        expected = (
            EXPECTED_TARGET_OCCURRENCE_COUNT,
            EXPECTED_VERSION_COUNT,
            EXPECTED_TARGET_EFFECT_TYPE_COUNT,
            EXPECTED_QUEUE_CHILD_COUNT,
            EXPECTED_WHOLE_CARD_DIRECT_COUNT,
            EXPECTED_WHOLE_CARD_CO_BLOCKED_COUNT,
            EXPECTED_COMPANION_BLOCKER_COUNT,
        )
        if observed != expected or result.target_type_counts != EXPECTED_TYPE_COUNTS:
            raise Plan2NativeStatusChildError(
                "target-cardinality-drift", f"observed={observed!r}"
            )
    return result


load_plan2_native_catalog_status_children = (
    compile_plan2_native_catalog_status_children
)


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusChildState:
    plan2_state: Plan2State = field(default_factory=Plan2State)
    native_state: Plan3NativeState | None = None
    remaining_move_state: RemainingCardMoveState | None = None
    review_state: Plan2ReviewCountAddInterval3State | None = None
    block_runtime: Plan2BlockDependConsumptionSumRuntime | None = None
    dynamic_block_runtime: Plan2NativeDynamicBlockRuntime | None = None
    application_status: ParameterApplicationStatus | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.plan2_state, Plan2State):
            raise TypeError("plan2_state must be Plan2State")


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusChildInput:
    guid_tokens: tuple[str, ...] | None = None
    playing_guid: str | None = None
    hand_limit: int | None = None
    lesson_type: str | None = None
    support_upgrades: object | None = None
    support_card_searches: Mapping[str, object] | None = None
    stamina_recover_restricted: bool | None = None
    stamina_recover_add_permil: int | None = None
    stamina_recover_add_permil_known: bool = False
    lesson_inputs_known: bool = False
    lesson_is_buff_active: bool = True
    adding_status: AddingParameterStatus = field(default_factory=AddingParameterStatus)
    adding_settings: AddingParameterSettings = field(default_factory=AddingParameterSettings)
    additional: AddingParameterAdditionalData = field(
        default_factory=AddingParameterAdditionalData
    )

    def __post_init__(self) -> None:
        if self.guid_tokens is not None:
            tokens = tuple(self.guid_tokens)
            if any(not isinstance(item, str) or not item for item in tokens):
                raise Plan2NativeStatusChildError("invalid-guid-token")
            object.__setattr__(self, "guid_tokens", tokens)


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusChildExecution:
    command: Plan2StatusEnchantChildCommand
    binding: Plan2NativeStatusChildBinding
    exact_result: object


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusChildrenResult:
    before_parent: Plan2NativeStatusEnchantRuntime
    after_parent: Plan2NativeStatusEnchantRuntime
    before_state: Plan2NativeStatusChildState
    after_state: Plan2NativeStatusChildState
    parent_transition: Plan2NativeStatusEnchantTransition
    executions: tuple[Plan2NativeStatusChildExecution, ...] = ()
    unresolved: tuple[str, ...] = ()
    trace: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def commands(self) -> tuple[Plan2StatusEnchantChildCommand, ...]:
        return self.parent_transition.commands


def _updated_application_status(
    status: ParameterApplicationStatus, application: ParameterApplication
) -> ParameterApplicationStatus:
    return replace(
        status,
        judge_parameter=application.after,
        current_turn_total_add_parameter=application.current_turn_total_add_parameter,
        judge_parameter_vocal=application.judge_parameter_vocal,
        judge_parameter_dance=application.judge_parameter_dance,
        judge_parameter_visual=application.judge_parameter_visual,
    )


def _preflight(
    state: Plan2NativeStatusChildState,
    inputs: Plan2NativeStatusChildInput,
    bindings: Sequence[Plan2NativeStatusChildBinding],
) -> tuple[str, ...]:
    reasons: list[str] = []
    operations = {item.operation for item in bindings}
    if operations & {"card-create-id", "card-draw", "force-play"} and state.native_state is None:
        reasons.append("native-state")
    if "card-create-id" in operations and inputs.guid_tokens is None:
        reasons.append("guid_tokens")
    if "card-create-id" in operations and inputs.guid_tokens is not None:
        expected = sum(1 for item in bindings if item.operation == "card-create-id")
        if len(inputs.guid_tokens) != expected:
            reasons.append("guid-token-count")
    if "force-play" in operations and not inputs.playing_guid:
        reasons.append("playing_guid")
    if "card-draw" in operations:
        if inputs.hand_limit is None:
            reasons.append("hand_limit")
        if inputs.lesson_type is None:
            reasons.append("lesson_type")
        if inputs.support_upgrades is None:
            reasons.append("support_upgrades")
        if inputs.support_card_searches is None:
            reasons.append("support_card_searches")
    if "card-move" in operations:
        if state.remaining_move_state is None:
            reasons.append("remaining_move_state")
        if inputs.hand_limit is None:
            reasons.append("hand_limit")
    if "review-count-add" in operations:
        if state.review_state is None:
            reasons.append("review_state")
        elif state.review_state.plan2_state != state.plan2_state:
            reasons.append("review-plan2-state-drift")
    if operations & {"lesson-depend-block", "block-depend-consumption"}:
        if state.block_runtime is None:
            reasons.append("block_runtime")
        elif state.block_runtime.block != state.plan2_state.block:
            reasons.append("block-runtime-drift")
    if "lesson-depend-block" in operations:
        if state.application_status is None:
            reasons.append("application_status")
        elif state.application_status.judge_parameter != state.plan2_state.score:
            reasons.append("application-score-drift")
        if not inputs.lesson_inputs_known:
            reasons.append("lesson_inputs")
    if "stamina-recover-multiple" in operations:
        if inputs.stamina_recover_restricted is None:
            reasons.append("stamina_recover_restricted")
        if not inputs.stamina_recover_add_permil_known:
            reasons.append("stamina_recover_add_permil")
    if "block-add-multiple-aggressive" in operations:
        if state.dynamic_block_runtime is None:
            reasons.append("dynamic_block_runtime")
        elif state.dynamic_block_runtime.block != state.plan2_state.block:
            reasons.append("dynamic-block-runtime-drift")
        elif (
            state.dynamic_block_runtime.aggressive
            != state.plan2_state.card_play_aggressive
        ):
            reasons.append("dynamic-aggressive-runtime-drift")
    return tuple(dict.fromkeys(reasons))


def _validate_transition_handoff(
    transition: Plan2NativeStatusEnchantTransition,
    catalog: Plan2NativeStatusChildCatalog,
) -> tuple[tuple[Plan2NativeStatusChildBinding, ...], tuple[str, ...]]:
    bindings: list[Plan2NativeStatusChildBinding] = []
    reasons: list[str] = list(transition.unresolved)
    expected_order = tuple(
        (command.listener_sequence, command.child_sequence)
        for command in transition.commands
    )
    if expected_order != tuple(sorted(expected_order)):
        reasons.append("parent-command-order-drift")
    for command in transition.commands:
        if (
            command.event_sequence != transition.after.event_sequence
            or command.phase != transition.event.phase
        ):
            reasons.append("parent-command-phase-sequence-drift")
            continue
        listener = next(
            (
                item
                for item in transition.before.listeners
                if item.status_uid == command.status_uid
            ),
            None,
        )
        if listener is None or (
            listener.source_guid,
            listener.source_card_id,
            listener.source_upgrade,
            listener.program.installer_effect_id,
            listener.program.status_enchant_id,
        ) != (
            command.source_guid,
            command.source_card_id,
            command.source_upgrade,
            command.installer_effect_id,
            command.status_enchant_id,
        ):
            reasons.append("parent-command-source-uid-drift")
            continue
        if command.play_origin != transition.event.play_origin:
            reasons.append("parent-command-origin-drift")
            continue
        try:
            binding = catalog.for_command(command)
        except Plan2NativeStatusChildError as error:
            reasons.append(error.code)
        else:
            bindings.append(binding)
    return tuple(bindings), tuple(dict.fromkeys(reasons))


def _event_snapshot_reasons(
    transition: Plan2NativeStatusEnchantTransition,
    state: Plan2NativeStatusChildState,
    bindings: Sequence[Plan2NativeStatusChildBinding],
) -> tuple[str, ...]:
    operations = {item.operation for item in bindings}
    event = transition.event
    reasons: list[str] = []
    if "stamina-recover-multiple" in operations and (
        event.stamina != state.plan2_state.stamina
        or event.max_stamina != state.plan2_state.max_stamina
    ):
        reasons.append("trigger-stamina-snapshot-drift")
    if "block-depend-consumption" in operations and (
        event.block != state.plan2_state.block
    ):
        reasons.append("trigger-block-snapshot-drift")
    return tuple(reasons)


def execute_plan2_native_status_child_handoff(
    parent_transition: Plan2NativeStatusEnchantTransition,
    state: Plan2NativeStatusChildState,
    execution_input: Plan2NativeStatusChildInput = Plan2NativeStatusChildInput(),
    *,
    compilation: Plan2NativeStatusChildCompilation | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeStatusChildrenResult:
    """Execute one already-captured queue atomically in native command order."""

    if compilation is None:
        compilation = compile_plan2_native_catalog_status_children(database)
    bindings, reasons = _validate_transition_handoff(parent_transition, compilation.catalog)
    if compilation.blockers:
        reasons = (
            *reasons,
            *(f"catalog:{item.code}" for item in compilation.blockers),
        )
    reasons = (
        *reasons,
        *_event_snapshot_reasons(parent_transition, state, bindings),
        *_preflight(state, execution_input, bindings),
    )
    if reasons:
        return Plan2NativeStatusChildrenResult(
            parent_transition.before,
            parent_transition.before,
            state,
            state,
            parent_transition,
            unresolved=tuple(dict.fromkeys(reasons)),
            trace=("fail-closed-before-child-execution",),
        )

    current = state
    executions: list[Plan2NativeStatusChildExecution] = []
    guid_offset = 0
    try:
        for command, binding in zip(parent_transition.commands, bindings, strict=True):
            child = binding.child
            exact: object
            if binding.operation == "lesson-depend-block":
                assert current.block_runtime is not None
                assert current.application_status is not None
                raw = _lesson_depend_block_raw_score(current.block_runtime.block, child.value1)
                calculated = calculate_adding_parameter(
                    raw,
                    is_buff_active=execution_input.lesson_is_buff_active,
                    status=execution_input.adding_status,
                    settings=execution_input.adding_settings,
                    additional=execution_input.additional,
                )
                application = apply_parameter_add(
                    calculated, status=current.application_status
                )
                application_after = _updated_application_status(
                    current.application_status, application
                )
                retention = f32(f32(1.0) - permille_to_f32(child.value2))
                target_block = ceil_f32_to_i32(
                    f32(retention * f32(current.block_runtime.block))
                )
                block_after = current.block_runtime
                set_block = None
                if target_block != block_after.block:
                    set_block = apply_plan2_set_block(
                        block_after, target_block, is_consumption=True
                    )
                    block_after = set_block.after
                exact = (raw, calculated, application, set_block)
                current = replace(
                    current,
                    plan2_state=replace(
                        current.plan2_state,
                        score=application_after.judge_parameter,
                        block=block_after.block,
                    ),
                    application_status=application_after,
                    block_runtime=block_after,
                )
            elif binding.operation == "stamina-recover-multiple":
                effect = Plan2NativeStaminaRecoverMultipleEffect(
                    child.effect_id,
                    child.value1,
                    child.value2,
                    child.count,
                    child.turn,
                    child.effect_group_ids,
                )
                exact = evaluate_stamina_recover_multiple(
                    effect,
                    Plan2NativeStaminaRecoverMultipleSnapshot(
                        current.plan2_state.stamina,
                        current.plan2_state.max_stamina,
                        bool(execution_input.stamina_recover_restricted),
                        execution_input.stamina_recover_add_permil,
                        current.plan2_state.block,
                    ),
                )
                if not exact.executable:
                    raise Plan2NativeStatusChildError(
                        "stamina-recover-unresolved", exact.reason or ""
                    )
                current = replace(
                    current,
                    plan2_state=replace(
                        current.plan2_state, stamina=exact.runtime_after.stamina
                    ),
                )
            elif binding.operation == "card-create-id":
                assert current.native_state is not None
                tokens = execution_input.guid_tokens or ()
                child_tokens = tokens[guid_offset : guid_offset + 1]
                guid_offset += 1
                with closing(sqlite3.connect(Path(database))) as connection:
                    connection.row_factory = sqlite3.Row
                    row = connection.execute(
                        "SELECT * FROM effect WHERE id = ?", (child.effect_id,)
                    ).fetchone()
                if row is None:
                    raise Plan2NativeStatusChildError("missing-effect-row")
                contract = resolve_plan2_card_create_contract(
                    row, plan_type="ProducePlanType_Plan2"
                )
                exact = apply_plan2_card_create_id(
                    current.native_state, contract, guid_tokens=child_tokens
                )
                if not exact.resolved:
                    raise Plan2NativeStatusChildError("card-create-unresolved")
                current = replace(current, native_state=exact.after)
            elif binding.operation == "review-count-add":
                assert current.review_state is not None
                program = load_plan2_review_count_add_interval3_program(database)
                review_after, applied, new_uid, merged_uid = _apply_review_count_add_child(
                    current.review_state, program
                )
                exact = (applied, new_uid, merged_uid)
                current = replace(
                    current,
                    review_state=review_after,
                    plan2_state=review_after.plan2_state,
                )
            elif binding.operation == "block-depend-consumption":
                assert current.block_runtime is not None
                effect = load_plan2_block_depend_consumption_sum_program(database).effects[0]
                exact = execute_plan2_block_depend_consumption_sum_child(
                    current.block_runtime, effect
                )
                current = replace(
                    current,
                    block_runtime=exact.after,
                    plan2_state=replace(current.plan2_state, block=exact.after.block),
                )
            elif binding.operation == "card-move":
                assert current.remaining_move_state is not None
                contract = next(
                    item
                    for item in load_remaining_card_move_contracts(database)
                    if item.effect_id == child.effect_id
                    and item.handoff_kind == "status-trigger-child"
                )
                exact = simulate_remaining_card_move(
                    current.remaining_move_state,
                    contract,
                    RemainingCardMoveHandoff(
                        "status-trigger-child",
                        command.status_enchant_id,
                        "status-change-trigger-child",
                        binding.child_index,
                        "callback",
                    ),
                    RemainingCardMoveExecutionContext(execution_input.hand_limit or 0),
                    database=database,
                )
                current = replace(current, remaining_move_state=exact.after)
            elif binding.operation == "card-draw":
                assert current.native_state is not None
                exact = current.native_state.draw_to_hand(
                    child.value1,
                    hand_limit=execution_input.hand_limit or 0,
                    lesson_type=execution_input.lesson_type or "",
                    support_upgrades=execution_input.support_upgrades,  # type: ignore[arg-type]
                    support_card_searches=execution_input.support_card_searches,  # type: ignore[arg-type]
                    effect_draw=True,
                )
                current = replace(current, native_state=exact.after)
            elif binding.operation == "force-play":
                assert current.native_state is not None
                contract = contract_for_effect(child.effect_id, database)
                planned = plan_force_play_card_search(
                    current.native_state,
                    current.plan2_state,
                    contract,
                    playing_guid=execution_input.playing_guid or "",
                    database=database,
                )
                if not planned.resolved:
                    raise Plan2NativeStatusChildError(
                        "force-play-plan-unresolved", planned.branch.reason
                    )
                exact = execute_plan2_force_play_queue(
                    current.plan2_state,
                    planned.after,
                    planned.commands,
                    database=database,
                )
                current = replace(
                    current,
                    plan2_state=exact.after_plan2,
                    native_state=exact.after_native,
                )
            elif binding.operation == "block-add-multiple-aggressive":
                assert current.dynamic_block_runtime is not None
                program = Plan2NativeDynamicBlockProgram(
                    binding.install.card_id,
                    binding.install.upgrade,
                    0,
                    (child.effect_id,),
                    child.effect_id,
                    child.effect_type,
                    child.value1,
                    child.value2,
                    child.count,
                    child.turn,
                    "none",
                )
                exact = execute_plan2_native_dynamic_block(
                    current.dynamic_block_runtime,
                    program,
                    Plan2NativeDynamicBlockExecutionInput(
                        command.source_guid,
                        command.play_origin,
                    ),
                )
                if not exact.executable:
                    raise Plan2NativeStatusChildError(
                        "dynamic-block-unresolved", ",".join(exact.unresolved)
                    )
                current = replace(
                    current,
                    dynamic_block_runtime=exact.after,
                    plan2_state=replace(current.plan2_state, block=exact.after.block),
                )
            else:
                raise Plan2NativeStatusChildError("unknown-child-operation")
            executions.append(Plan2NativeStatusChildExecution(command, binding, exact))
    except Exception as error:
        code = getattr(error, "code", "child-execution-unresolved")
        return Plan2NativeStatusChildrenResult(
            parent_transition.before,
            parent_transition.before,
            state,
            state,
            parent_transition,
            unresolved=(f"{code}:{error}",),
            trace=("rollback-parent-spend-and-child-mutations",),
        )
    return Plan2NativeStatusChildrenResult(
        parent_transition.before,
        parent_transition.after,
        state,
        current,
        parent_transition,
        tuple(executions),
        trace=(
            "parent-capture-and-spend",
            "ordered-child-queue",
            "child-execution-commit",
        ),
    )


def dispatch_plan2_native_status_children(
    parent_runtime: Plan2NativeStatusEnchantRuntime,
    event: Plan2NativeStatusEnchantEvent,
    state: Plan2NativeStatusChildState,
    execution_input: Plan2NativeStatusChildInput = Plan2NativeStatusChildInput(),
    *,
    compilation: Plan2NativeStatusChildCompilation | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeStatusChildrenResult:
    """Capture/spend the parent, then dispatch its immutable child queue."""

    transition = resolve_plan2_native_status_enchant_event(parent_runtime, event)
    return execute_plan2_native_status_child_handoff(
        transition,
        state,
        execution_input,
        compilation=compilation,
        database=database,
    )


execute_plan2_native_catalog_status_children = dispatch_plan2_native_status_children
