"""Exact outer-Produce Expectimax over explicit chance frontiers.

The rollout kernel deliberately pauses at server-owned or RNG-owned facts.
This module turns those pauses into a search tree only when a caller supplies
every branch and an exact rational probability.  It never averages states,
invents missing outcomes, or treats a partially enumerated action as safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Protocol, TypeAlias

from .produce_rollout import (
    ExternalOutcome,
    ExternalRequest,
    ProduceAction,
    ProduceRolloutKernel,
    ProduceRolloutState,
)


Utility: TypeAlias = int | Fraction


@dataclass(frozen=True, slots=True, order=True)
class OuterSearchIssue:
    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.code or not self.field:
            raise ValueError("search issue code and field must be non-empty")


@dataclass(frozen=True, slots=True)
class WeightedExternalOutcome:
    probability: Fraction
    outcome: ExternalOutcome

    def __post_init__(self) -> None:
        probability = self.probability
        if isinstance(probability, bool) or not isinstance(probability, Fraction):
            raise TypeError("external probability must be fractions.Fraction")
        if probability <= 0 or probability > 1:
            raise ValueError("external probability must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class ExternalChanceExpansion:
    """Either one complete probability frontier or a typed pause."""

    branches: tuple[WeightedExternalOutcome, ...] = ()
    issues: tuple[OuterSearchIssue, ...] = ()

    def __post_init__(self) -> None:
        branches = tuple(self.branches)
        issues = tuple(self.issues)
        object.__setattr__(self, "branches", branches)
        object.__setattr__(self, "issues", issues)
        if bool(branches) == bool(issues):
            raise ValueError("chance expansion must contain branches xor issues")
        if branches and sum((branch.probability for branch in branches), Fraction()) != 1:
            raise ValueError("external chance probabilities must sum exactly to one")
        if branches:
            branch_ids = tuple(branch.outcome.branch.branch_id for branch in branches)
            if len(set(branch_ids)) != len(branch_ids):
                raise ValueError("external chance branch IDs must be unique")

    @classmethod
    def resolved(
        cls, *branches: WeightedExternalOutcome
    ) -> "ExternalChanceExpansion":
        return cls(branches=tuple(branches))

    @classmethod
    def paused(
        cls, *issues: OuterSearchIssue
    ) -> "ExternalChanceExpansion":
        return cls(issues=tuple(issues))


class ExternalChanceProvider(Protocol):
    def __call__(
        self, state: ProduceRolloutState, request: ExternalRequest
    ) -> ExternalChanceExpansion: ...


class OuterTerminalEvaluator(Protocol):
    def __call__(self, state: ProduceRolloutState) -> Utility: ...


@dataclass(frozen=True, slots=True)
class OuterActionValue:
    action: ProduceAction
    value: Fraction


@dataclass(frozen=True, slots=True)
class OuterExpectimaxReady:
    state: ProduceRolloutState
    value: Fraction
    best_action: ProduceAction | None
    action_values: tuple[OuterActionValue, ...] = ()
    terminal: bool = False
    explored_nodes: int = 0
    available: bool = field(default=True, init=False)
    issues: tuple[OuterSearchIssue, ...] = field(default=(), init=False)


@dataclass(frozen=True, slots=True)
class OuterExpectimaxPause:
    state: ProduceRolloutState
    issues: tuple[OuterSearchIssue, ...]
    explored_nodes: int = 0
    available: bool = field(default=False, init=False)
    value: None = field(default=None, init=False)
    best_action: None = field(default=None, init=False)
    action_values: tuple[OuterActionValue, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        if not self.issues:
            raise ValueError("paused outer search requires at least one issue")


OuterExpectimaxResult: TypeAlias = OuterExpectimaxReady | OuterExpectimaxPause


def search_produce_rollout_expectimax(
    kernel: ProduceRolloutKernel,
    state: ProduceRolloutState,
    *,
    chance_provider: ExternalChanceProvider,
    evaluator: OuterTerminalEvaluator,
    max_transitions: int = 128,
) -> OuterExpectimaxResult:
    """Return an exact best action only when the whole reachable tree resolves.

    ``max_transitions`` counts both decisions and external chance resolutions.
    A terminal state is always evaluable, including exactly at the boundary.
    """

    if not isinstance(kernel, ProduceRolloutKernel):
        raise TypeError("kernel must be ProduceRolloutKernel")
    if not isinstance(max_transitions, int) or isinstance(max_transitions, bool):
        raise TypeError("max_transitions must be an integer")
    if max_transitions < 1:
        raise ValueError("max_transitions must be positive")
    kernel.validate_state(state)
    # Every field of ProduceRolloutState is immutable/hashable.  Using the
    # structural state directly avoids serializing the growing chance history
    # at every one of the many outer-tree nodes.
    cache: dict[tuple[ProduceRolloutState, int], OuterExpectimaxResult] = {}
    explored = [0]

    def visit(current: ProduceRolloutState, remaining: int) -> OuterExpectimaxResult:
        key = (current, remaining)
        cached = cache.get(key)
        if cached is not None:
            return cached
        explored[0] += 1
        if current.terminal:
            result: OuterExpectimaxResult = OuterExpectimaxReady(
                current,
                _utility(evaluator(current)),
                None,
                terminal=True,
                explored_nodes=explored[0],
            )
            cache[key] = result
            return result
        if remaining == 0:
            result = OuterExpectimaxPause(
                current,
                (
                    OuterSearchIssue(
                        "transition-limit",
                        "max_transitions",
                        f"week={current.week}",
                    ),
                ),
                explored_nodes=explored[0],
            )
            cache[key] = result
            return result
        request = current.pending
        if request is not None:
            expansion = chance_provider(current, request)
            if not isinstance(expansion, ExternalChanceExpansion):
                raise TypeError("chance_provider must return ExternalChanceExpansion")
            if expansion.issues:
                result = OuterExpectimaxPause(
                    current, expansion.issues, explored_nodes=explored[0]
                )
                cache[key] = result
                return result
            total = Fraction()
            for branch in expansion.branches:
                if branch.outcome.request_id != request.request_id:
                    raise ValueError("external branch does not match pending request")
                next_state = kernel.resolve_external(current, branch.outcome).state
                child = visit(next_state, remaining - 1)
                if isinstance(child, OuterExpectimaxPause):
                    cache[key] = child
                    return child
                total += branch.probability * child.value
            result = OuterExpectimaxReady(
                current,
                total,
                None,
                explored_nodes=explored[0],
            )
            cache[key] = result
            return result

        legal = kernel.legal_actions(current)
        if not legal:
            result = OuterExpectimaxPause(
                current,
                (OuterSearchIssue("no-legal-action", "state.phase"),),
                explored_nodes=explored[0],
            )
            cache[key] = result
            return result
        values: list[OuterActionValue] = []
        for action in legal:
            next_state = kernel.apply_action(current, action).state
            child = visit(next_state, remaining - 1)
            if isinstance(child, OuterExpectimaxPause):
                issue = OuterSearchIssue(
                    "unresolved-action",
                    action.choice_id,
                    child.issues[0].code,
                )
                result = OuterExpectimaxPause(
                    current,
                    (issue, *child.issues),
                    explored_nodes=explored[0],
                )
                cache[key] = result
                return result
            values.append(OuterActionValue(action, child.value))
        best = max(values, key=lambda entry: entry.value)
        result = OuterExpectimaxReady(
            current,
            best.value,
            best.action,
            tuple(values),
            explored_nodes=explored[0],
        )
        cache[key] = result
        return result

    result = visit(state, max_transitions)
    # Cached descendants record the count at their creation.  The root result
    # reports the complete memoized traversal count instead.
    if isinstance(result, OuterExpectimaxReady):
        return OuterExpectimaxReady(
            result.state,
            result.value,
            result.best_action,
            result.action_values,
            result.terminal,
            explored[0],
        )
    return OuterExpectimaxPause(result.state, result.issues, explored[0])


def _utility(value: Utility) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, Fraction)):
        raise TypeError("outer evaluator must return int or Fraction")
    return Fraction(value)


__all__ = [
    "ExternalChanceExpansion",
    "ExternalChanceProvider",
    "OuterActionValue",
    "OuterExpectimaxPause",
    "OuterExpectimaxReady",
    "OuterExpectimaxResult",
    "OuterSearchIssue",
    "OuterTerminalEvaluator",
    "WeightedExternalOutcome",
    "search_produce_rollout_expectimax",
]
