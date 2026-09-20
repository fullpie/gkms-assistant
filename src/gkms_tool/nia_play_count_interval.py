"""Exact NIA v3.2.3 ``ExamPlayCountInterval(3/5)`` listener adapter.

The native counter belongs to each ``TriggerEffectStatusEffect`` instance.
At card-play batch construction, the game increments that instance's phase
counter, evaluates the modulo predicate, and queues matching listener effects
before the played card's own direct effects.  This module models that narrow
boundary without changing the Plan 3 search kernel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .nia_add_grow_effect import (
    NiaAddGrowMutation,
    NiaDeckAllState,
    execute_nia_add_grow_programs,
)
from .nia_status_enchant import (
    NiaStatusEnchantProgram,
    PHASE_PLAY_COUNT_INTERVAL,
    nia_play_count_interval_listener_errors,
)
from .plan3_engine import ActivePlan3StatusEnchant


_INT32_MAX = 2**31 - 1


class NiaPlayCountIntervalContractError(ValueError):
    """A listener, program, or phase count is outside the proven profile."""


@dataclass(frozen=True, slots=True)
class NiaPlayCountPhaseCounter:
    instance_id: str
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.instance_id, str) or not self.instance_id:
            raise TypeError("instance_id must be non-empty text")
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or not 0 <= self.count <= _INT32_MAX
        ):
            raise NiaPlayCountIntervalContractError(
                f"phase count is outside non-negative Int32: {self.count!r}"
            )


@dataclass(frozen=True, slots=True)
class NiaPlayCountIntervalState:
    deck_all: NiaDeckAllState
    active: tuple[ActivePlan3StatusEnchant, ...]
    phase_counts: tuple[NiaPlayCountPhaseCounter, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.deck_all, NiaDeckAllState):
            raise TypeError("deck_all must be NiaDeckAllState")
        active = tuple(self.active)
        if not all(isinstance(item, ActivePlan3StatusEnchant) for item in active):
            raise TypeError("active must contain ActivePlan3StatusEnchant values")
        active_ids = [item.instance_id for item in active]
        if (
            any(not value for value in active_ids)
            or len(active_ids) != len(set(active_ids))
        ):
            raise NiaPlayCountIntervalContractError(
                "active status instance IDs must be non-empty and unique"
            )
        counters = tuple(self.phase_counts)
        if not all(isinstance(item, NiaPlayCountPhaseCounter) for item in counters):
            raise TypeError("phase_counts must contain NiaPlayCountPhaseCounter values")
        counter_ids = [item.instance_id for item in counters]
        if len(counter_ids) != len(set(counter_ids)):
            raise NiaPlayCountIntervalContractError(
                "phase counter instance IDs must be unique"
            )
        unknown = set(counter_ids).difference(active_ids)
        if unknown:
            raise NiaPlayCountIntervalContractError(
                f"phase counters reference inactive listeners: {sorted(unknown)!r}"
            )
        object.__setattr__(self, "active", active)
        object.__setattr__(self, "phase_counts", counters)


@dataclass(frozen=True, slots=True)
class NiaPlayCountIntervalFire:
    listener_sequence: int
    instance_id: str
    source_effect_id: str
    status_enchant_id: str
    interval: int
    count_before: int
    count_after: int
    nested_effect_ids: tuple[str, ...]
    mutation_start: int
    mutation_count: int


@dataclass(frozen=True, slots=True)
class NiaPlayCountIntervalExecution:
    state: NiaPlayCountIntervalState
    fires: tuple[NiaPlayCountIntervalFire, ...]
    mutations: tuple[NiaAddGrowMutation, ...]


def _program_index(
    programs: Iterable[NiaStatusEnchantProgram],
) -> dict[str, NiaStatusEnchantProgram]:
    if isinstance(programs, (str, bytes)):
        raise TypeError("programs must be an iterable of NiaStatusEnchantProgram")
    result: dict[str, NiaStatusEnchantProgram] = {}
    for program in tuple(programs):
        if not isinstance(program, NiaStatusEnchantProgram):
            raise TypeError("programs must contain NiaStatusEnchantProgram values")
        rule = program.effect.status_enchant
        if rule is None:
            raise NiaPlayCountIntervalContractError(
                f"program has no status listener: {program.effect.id}"
            )
        errors = nia_play_count_interval_listener_errors(rule)
        if errors:
            raise NiaPlayCountIntervalContractError(
                f"unsupported interval program {program.effect.id}: {errors!r}"
            )
        status_id = program.effect.status_enchant_id
        if not status_id:
            raise NiaPlayCountIntervalContractError(
                f"program has no status listener id: {program.effect.id}"
            )
        existing = result.get(status_id)
        if existing is not None and existing != program:
            raise NiaPlayCountIntervalContractError(
                f"conflicting duplicate interval status program: {status_id}"
            )
        result[status_id] = program
    return result


def execute_nia_play_count_interval_card_start(
    state: NiaPlayCountIntervalState,
    programs: Iterable[NiaStatusEnchantProgram],
) -> NiaPlayCountIntervalExecution:
    """Advance one accepted play at native pre-direct-effect timing.

    Call this after the card has become ``PlayingCard`` and before its direct
    effect commands execute.  Exact interval listeners are incremented and
    evaluated independently in active collection order.  Other listener
    phases are preserved but not executed by this standalone adapter.
    """

    if not isinstance(state, NiaPlayCountIntervalState):
        raise TypeError("state must be NiaPlayCountIntervalState")
    by_status_id = _program_index(programs)
    old_counts = {item.instance_id: item.count for item in state.phase_counts}
    interval_active: list[
        tuple[int, ActivePlan3StatusEnchant, NiaStatusEnchantProgram]
    ] = []
    for sequence, listener in enumerate(state.active):
        phases = listener.rule.trigger.phase_types
        has_interval = PHASE_PLAY_COUNT_INTERVAL in phases
        if not has_interval:
            if listener.instance_id in old_counts:
                raise NiaPlayCountIntervalContractError(
                    "phase counter belongs to a non-interval listener: "
                    f"{listener.instance_id}"
                )
            continue
        if phases != (PHASE_PLAY_COUNT_INTERVAL,):
            raise NiaPlayCountIntervalContractError(
                f"unsupported active interval phase shape: {listener.instance_id}"
            )
        program = by_status_id.get(listener.rule.id)
        if program is None:
            raise NiaPlayCountIntervalContractError(
                f"missing interval program for active listener: {listener.rule.id}"
            )
        if listener.rule != program.effect.status_enchant:
            raise NiaPlayCountIntervalContractError(
                f"active listener/program rule mismatch: {listener.instance_id}"
            )
        if not (
            listener.max_uses == 0
            and listener.uses == 0
            and listener.max_uses_per_turn == 0
            and listener.uses_this_turn == 0
            and listener.remaining_turns == -1
            and listener.is_item_direct is False
        ):
            raise NiaPlayCountIntervalContractError(
                f"unsupported active interval lifecycle: {listener.instance_id}"
            )
        interval_active.append((sequence, listener, program))

    deck = state.deck_all
    counters: list[NiaPlayCountPhaseCounter] = []
    fires: list[NiaPlayCountIntervalFire] = []
    mutations: list[NiaAddGrowMutation] = []
    for sequence, listener, program in interval_active:
        before = old_counts.get(listener.instance_id, 0)
        if before == _INT32_MAX:
            raise NiaPlayCountIntervalContractError(
                f"phase count Int32 overflow: {listener.instance_id}"
            )
        after = before + 1
        counters.append(NiaPlayCountPhaseCounter(listener.instance_id, after))
        interval = listener.rule.trigger.phase_values[0]
        if after % interval:
            continue
        mutation_start = len(mutations)
        growth = execute_nia_add_grow_programs(
            deck, program.nested_add_grow_programs
        )
        deck = growth.state
        mutations.extend(growth.mutations)
        fires.append(
            NiaPlayCountIntervalFire(
                listener_sequence=sequence,
                instance_id=listener.instance_id,
                source_effect_id=program.effect.id,
                status_enchant_id=program.effect.status_enchant_id,
                interval=interval,
                count_before=before,
                count_after=after,
                nested_effect_ids=tuple(
                    item.effect.effect_id
                    for item in program.nested_add_grow_programs
                ),
                mutation_start=mutation_start,
                mutation_count=len(growth.mutations),
            )
        )

    return NiaPlayCountIntervalExecution(
        NiaPlayCountIntervalState(deck, state.active, tuple(counters)),
        tuple(fires),
        tuple(mutations),
    )


__all__ = [
    "NiaPlayCountIntervalContractError",
    "NiaPlayCountIntervalExecution",
    "NiaPlayCountIntervalFire",
    "NiaPlayCountIntervalState",
    "NiaPlayCountPhaseCounter",
    "execute_nia_play_count_interval_card_start",
]
