"""Deterministic search-only inputs for Plan2 generated-card operations.

Native card GUIDs and status UIDs remain LocalSave/game authority.  Bounded
offline search nevertheless needs identities for hypothetical cards it may
create and a concrete legal selection for exact ForcePlay branches.  This
module derives those values from a settled horizon and its already-compiled
Master operations.  Symbolic GUIDs are deliberately namespaced and must never
be submitted to Maa or compared with a later LocalSave identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import sqlite3
from typing import Final

from .master_db import DEFAULT_DATABASE
from .plan2_native_horizon import (
    Plan2NativeBlocker,
    Plan2NativeGeneratedAllocatorInput,
    Plan2NativeHorizonState,
    Plan2NativeProgramCatalog,
    _next_horizon_status_uid,
    _plan3_state_from_ordered,
)
from .plan2_native_program_catalog import (
    Plan2MasterGeneratedBuffForceOperation,
    Plan2MasterGeneratedCreateOperation,
    Plan2MasterGeneratedForceSettlementOperation,
    Plan2MasterPlayingExecutor,
    Plan2MasterTriggeredOperation,
)
from .plan3_force_play_card_search import (
    ForcePlayExecutionInput,
    ForcePlayResolvedBranch,
    contract_for_effect,
    plan_force_play_card_search,
)


PLAN2_SYMBOLIC_GUID_PREFIX: Final = "__plan2_search_only__-"
_GENERATED_EFFECT_MARKERS: Final = (
    "exam_card_create_id",
    "exam_card_create_search",
    "exam_card_search_effect_play_count_buff",
    "exam_force_play_card_search",
)


@dataclass(frozen=True, slots=True)
class Plan2SymbolicGeneratedInputResult:
    state: Plan2NativeHorizonState
    added_inputs: tuple[Plan2NativeGeneratedAllocatorInput, ...]
    blockers: tuple[Plan2NativeBlocker, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, Plan2NativeHorizonState):
            raise TypeError("state must be a Plan2NativeHorizonState")
        if any(
            not isinstance(value, Plan2NativeGeneratedAllocatorInput)
            for value in self.added_inputs
        ):
            raise TypeError("added_inputs must contain typed allocator inputs")
        if any(not isinstance(value, Plan2NativeBlocker) for value in self.blockers):
            raise TypeError("blockers must contain Plan2NativeBlocker")


def is_plan2_symbolic_guid(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PLAN2_SYMBOLIC_GUID_PREFIX)


def _unwrapped_operations(
    executor: Plan2MasterPlayingExecutor,
) -> tuple[object, ...]:
    return tuple(
        value.operation if isinstance(value, Plan2MasterTriggeredOperation) else value
        for value in executor.operations
    )


def _program_for_ref(
    catalog: Plan2NativeProgramCatalog,
    ref: tuple[str, int],
):
    return next((value for value in catalog.programs if value.ref == ref), None)


def _symbolic_guid(
    *,
    source_guid: str,
    effect_id: str,
    ordinal: int,
    used_guids: set[str],
) -> str:
    attempt = 0
    while True:
        digest = hashlib.sha256(
            f"{source_guid}\n{effect_id}\n{ordinal}\n{attempt}".encode("utf-8")
        ).hexdigest()[:32]
        value = f"{PLAN2_SYMBOLIC_GUID_PREFIX}{digest}"
        if value not in used_guids:
            used_guids.add(value)
            return value
        attempt += 1


def _select_force_play_guid(
    state: Plan2NativeHorizonState,
    *,
    source_guid: str,
    effect_id: str,
    database: Path,
) -> tuple[str | None, Plan2NativeBlocker | None]:
    """Choose the first candidate accepted by the exact native search plan."""

    try:
        native = _plan3_state_from_ordered(state.zones)
        contract = contract_for_effect(effect_id, database)
        candidates = native.card_move_candidates(
            contract.search,
            playing_guid=source_guid,
        )
        for candidate in candidates:
            planned = plan_force_play_card_search(
                native,
                contract,
                playing_guid=source_guid,
                execution_input=ForcePlayExecutionInput((candidate.guid,)),
                database=database,
            )
            if (
                not planned.unresolved
                and isinstance(planned.branch, ForcePlayResolvedBranch)
                and planned.branch.selected_guids == (candidate.guid,)
            ):
                return candidate.guid, None
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
        return None, Plan2NativeBlocker(
            "plan2-symbolic-force-play-resolution-failed",
            f"{source_guid}:{effect_id}:{type(error).__name__}:{error}",
        )
    return None, Plan2NativeBlocker(
        "plan2-symbolic-force-play-candidate-missing",
        f"{source_guid}:{effect_id}",
    )


def prepare_plan2_symbolic_generated_inputs(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    *,
    database: str | Path = DEFAULT_DATABASE,
    expansion_depth: int = 1,
) -> Plan2SymbolicGeneratedInputResult:
    """Attach deterministic hypothetical inputs without changing live identity.

    Existing caller/LocalSave-bound inputs always win.  ``expansion_depth``
    preallocates fixed-ID descendants deeply enough for the caller's bounded
    search; descendants beyond that depth cannot be played within that search.
    """

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be a Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be a Plan2NativeProgramCatalog")
    if isinstance(expansion_depth, bool) or not isinstance(expansion_depth, int):
        raise TypeError("expansion_depth must be an integer")
    if expansion_depth < 1:
        raise ValueError("expansion_depth must be positive")

    database_path = Path(database)
    existing = {value.source_guid: value for value in state.generated_inputs}
    used_guids = {value.guid for value in state.zones.card_universe}
    for value in state.generated_inputs:
        used_guids.update(value.guid_tokens or ())
    additions: dict[str, Plan2NativeGeneratedAllocatorInput] = {}
    blockers: list[Plan2NativeBlocker] = []
    status_uid = _next_horizon_status_uid(state)
    reserved_status_uids = {
        value.native_status_uid
        for value in state.generated_inputs
        if value.native_status_uid is not None
    }

    def add_for_program(
        source_guid: str,
        program: object,
        remaining_depth: int,
    ) -> None:
        nonlocal status_uid
        if source_guid in existing or source_guid in additions:
            return
        executor = getattr(program, "native_playing_executor", None)
        executor_id = str(getattr(program, "native_playing_executor_id", ""))
        if not isinstance(executor, Plan2MasterPlayingExecutor):
            if any(marker in executor_id for marker in _GENERATED_EFFECT_MARKERS):
                blockers.append(
                    Plan2NativeBlocker(
                        "plan2-symbolic-generated-executor-uninspectable",
                        f"{source_guid}:{executor_id}",
                    )
                )
                # Supplying a typed, deliberately incomplete value preserves
                # the useful blocker above and avoids the misleading generic
                # generated-card-input-missing transition failure.
                additions[source_guid] = Plan2NativeGeneratedAllocatorInput(
                    source_guid,
                    guid_tokens=(),
                )
            return

        operations = _unwrapped_operations(executor)
        create_operations = tuple(
            value
            for value in operations
            if isinstance(value, Plan2MasterGeneratedCreateOperation)
        )
        force_operations: list[object] = []
        for value in operations:
            force_program = None
            if isinstance(value, Plan2MasterGeneratedBuffForceOperation):
                force_program = value.force_program
            elif isinstance(value, Plan2MasterGeneratedForceSettlementOperation):
                force_program = value.program
            if force_program is not None and all(
                getattr(current, "effect_id", None) != force_program.effect_id
                for current in force_operations
            ):
                force_operations.append(force_program)
        if not create_operations and not force_operations:
            return

        guid_tokens: tuple[str, ...] | None = None
        ignore_ids: tuple[str, ...] | None = None
        created_descendants: list[tuple[str, tuple[str, int]]] = []
        if create_operations:
            selected_create = create_operations[0].program
            if len(create_operations) != 1:
                blockers.append(
                    Plan2NativeBlocker(
                        "plan2-symbolic-multiple-card-create-unbound",
                        f"{source_guid}:count={len(create_operations)}",
                    )
                )
            if (
                selected_create.count_min < 1
                or selected_create.count_min != selected_create.count_max
            ):
                blockers.append(
                    Plan2NativeBlocker(
                        "plan2-symbolic-card-create-count-unbound",
                        f"{source_guid}:{selected_create.effect_id}:"
                        f"{selected_create.count_min}-{selected_create.count_max}",
                    )
                )
                guid_tokens = ()
            else:
                values = tuple(
                    _symbolic_guid(
                        source_guid=source_guid,
                        effect_id=selected_create.effect_id,
                        ordinal=index,
                        used_guids=used_guids,
                    )
                    for index in range(selected_create.count_max)
                )
                guid_tokens = values
                if selected_create.target_card_id:
                    created_descendants.extend(
                        (
                            value,
                            (
                                selected_create.target_card_id,
                                selected_create.target_upgrade,
                            ),
                        )
                        for value in values
                    )
            if selected_create.operation == "card-create-search":
                # The horizon has no observed PlanIgnoreCardIds collection.
                # Empty is the deterministic neutral input accepted by the
                # exact search owner; it does not manufacture card identity.
                ignore_ids = ()

        selected_guid: str | None = None
        native_status_uid: int | None = None
        if force_operations:
            selected_force = force_operations[0]
            if len(force_operations) != 1:
                blockers.append(
                    Plan2NativeBlocker(
                        "plan2-symbolic-multiple-force-play-unbound",
                        f"{source_guid}:count={len(force_operations)}",
                    )
                )
            selected_guid, blocker = _select_force_play_guid(
                state,
                source_guid=source_guid,
                effect_id=selected_force.effect_id,
                database=database_path,
            )
            if blocker is not None:
                blockers.append(blocker)
            if any(
                isinstance(value, Plan2MasterGeneratedBuffForceOperation)
                for value in operations
            ):
                while status_uid in reserved_status_uids:
                    status_uid += 1
                native_status_uid = status_uid
                reserved_status_uids.add(status_uid)
                status_uid += 1

        additions[source_guid] = Plan2NativeGeneratedAllocatorInput(
            source_guid=source_guid,
            guid_tokens=guid_tokens,
            plan_ignore_card_ids=ignore_ids,
            force_selected_guid=selected_guid,
            native_status_uid=native_status_uid,
        )

        if remaining_depth <= 1:
            return
        for child_guid, child_ref in created_descendants:
            child_program = _program_for_ref(catalog, child_ref)
            if child_program is not None:
                add_for_program(child_guid, child_program, remaining_depth - 1)

    for card in sorted(state.zones.card_universe, key=lambda value: value.guid):
        program = catalog.get(card)
        if program is not None:
            add_for_program(card.guid, program, expansion_depth)

    added = tuple(additions[key] for key in sorted(additions))
    prepared = (
        state
        if not added
        else replace(state, generated_inputs=(*state.generated_inputs, *added))
    )
    return Plan2SymbolicGeneratedInputResult(
        prepared,
        added,
        tuple(dict.fromkeys(blockers)),
    )


__all__ = [
    "PLAN2_SYMBOLIC_GUID_PREFIX",
    "Plan2SymbolicGeneratedInputResult",
    "is_plan2_symbolic_guid",
    "prepare_plan2_symbolic_generated_inputs",
]
