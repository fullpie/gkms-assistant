"""Deterministic whole-run driver for an explicit Initial Regular scenario.

The outer kernel already owns all lifecycle transitions.  This driver merely
alternates policy decisions and externally supplied outcomes until terminal.
It never resolves a probability frontier implicitly: more than one branch
requires an injected selector, making recorded scenarios reproducible and
preventing a stochastic catalog from masquerading as a fixed run.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .produce_rollout import (
    ExternalOutcome,
    ExternalRequest,
    ProduceAction,
    ProduceRolloutKernel,
    ProduceRolloutState,
    RolloutPolicy,
)
from .produce_rollout_expectimax import (
    ExternalChanceExpansion,
    ExternalChanceProvider,
    OuterSearchIssue,
    WeightedExternalOutcome,
)


class InitialRegularFixedRunStepKind(StrEnum):
    DECISION = "decision"
    EXTERNAL = "external"


class InitialRegularFixedRunStop(StrEnum):
    TERMINAL = "terminal"
    POLICY = "policy"
    EXTERNAL_BLOCKED = "external-blocked"
    BRANCH_SELECTION_REQUIRED = "branch-selection-required"
    TRANSITION_LIMIT = "transition-limit"


class InitialRegularFixedBranchSelector(Protocol):
    def __call__(
        self,
        state: ProduceRolloutState,
        request: ExternalRequest,
        branches: tuple[WeightedExternalOutcome, ...],
    ) -> WeightedExternalOutcome | None: ...


@dataclass(frozen=True, slots=True)
class InitialRegularFixedRunStep:
    index: int
    kind: InitialRegularFixedRunStepKind
    before: ProduceRolloutState
    after: ProduceRolloutState
    action: ProduceAction | None = None
    request: ExternalRequest | None = None
    outcome: ExternalOutcome | None = None

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 1:
            raise ValueError("fixed-run step index must be positive")
        if not isinstance(self.kind, InitialRegularFixedRunStepKind):
            raise TypeError("kind must be InitialRegularFixedRunStepKind")
        if not isinstance(self.before, ProduceRolloutState) or not isinstance(
            self.after, ProduceRolloutState
        ):
            raise TypeError("before/after must be ProduceRolloutState")
        if self.kind is InitialRegularFixedRunStepKind.DECISION:
            if self.action is None or self.outcome is not None:
                raise ValueError("decision step requires action and no outcome")
        elif self.request is None or self.outcome is None or self.action is not None:
            raise ValueError("external step requires request/outcome and no action")


@dataclass(frozen=True, slots=True)
class InitialRegularFixedRunResult:
    initial_state: ProduceRolloutState
    final_state: ProduceRolloutState
    stop: InitialRegularFixedRunStop
    issues: tuple[OuterSearchIssue, ...]
    steps: tuple[InitialRegularFixedRunStep, ...]

    @property
    def completed(self) -> bool:
        return self.stop is InitialRegularFixedRunStop.TERMINAL and self.final_state.terminal

    @property
    def action_path(self) -> tuple[str, ...]:
        return tuple(
            step.action.choice_id
            for step in self.steps
            if step.action is not None
        )

    @property
    def branch_path(self) -> tuple[str, ...]:
        return tuple(
            step.outcome.branch.branch_id
            for step in self.steps
            if step.outcome is not None
        )


def _result(
    initial: ProduceRolloutState,
    final: ProduceRolloutState,
    stop: InitialRegularFixedRunStop,
    issues: tuple[OuterSearchIssue, ...],
    steps: list[InitialRegularFixedRunStep],
) -> InitialRegularFixedRunResult:
    return InitialRegularFixedRunResult(initial, final, stop, issues, tuple(steps))


def run_initial_regular_fixed_scenario(
    kernel: ProduceRolloutKernel,
    initial_state: ProduceRolloutState,
    *,
    policy: RolloutPolicy,
    chance_provider: ExternalChanceProvider,
    branch_selector: InitialRegularFixedBranchSelector | None = None,
    max_transitions: int = 128,
) -> InitialRegularFixedRunResult:
    """Run decisions and exact external outcomes to a hard boundary."""

    if not isinstance(kernel, ProduceRolloutKernel):
        raise TypeError("kernel must be ProduceRolloutKernel")
    if not isinstance(initial_state, ProduceRolloutState):
        raise TypeError("initial_state must be ProduceRolloutState")
    if not callable(policy) or not callable(chance_provider):
        raise TypeError("policy and chance_provider must be callable")
    if branch_selector is not None and not callable(branch_selector):
        raise TypeError("branch_selector must be callable or None")
    if type(max_transitions) is not int or max_transitions < 1:
        raise ValueError("max_transitions must be a positive integer")
    kernel.validate_state(initial_state)

    state = initial_state
    steps: list[InitialRegularFixedRunStep] = []
    for index in range(1, max_transitions + 1):
        if state.terminal:
            return _result(
                initial_state,
                state,
                InitialRegularFixedRunStop.TERMINAL,
                (),
                steps,
            )
        request = state.pending
        if request is not None:
            expansion = chance_provider(state, request)
            if not isinstance(expansion, ExternalChanceExpansion):
                raise TypeError("chance_provider must return ExternalChanceExpansion")
            if expansion.issues:
                return _result(
                    initial_state,
                    state,
                    InitialRegularFixedRunStop.EXTERNAL_BLOCKED,
                    expansion.issues,
                    steps,
                )
            branches = expansion.branches
            selected: WeightedExternalOutcome | None
            if branch_selector is None:
                selected = branches[0] if len(branches) == 1 else None
            else:
                selected = branch_selector(state, request, branches)
                if selected is not None and selected not in branches:
                    raise ValueError("branch_selector returned a branch outside the frontier")
            if selected is None:
                return _result(
                    initial_state,
                    state,
                    InitialRegularFixedRunStop.BRANCH_SELECTION_REQUIRED,
                    (
                        OuterSearchIssue(
                            "fixed-scenario-branch-selection-required",
                            "branch_selector",
                            f"request={request.request_id};branches={len(branches)}",
                        ),
                    ),
                    steps,
                )
            before = state
            transition = kernel.resolve_external(state, selected.outcome)
            state = transition.state
            steps.append(
                InitialRegularFixedRunStep(
                    index,
                    InitialRegularFixedRunStepKind.EXTERNAL,
                    before,
                    state,
                    request=request,
                    outcome=selected.outcome,
                )
            )
            continue

        legal = kernel.legal_actions(state)
        action = policy(state, legal)
        if action is None:
            return _result(
                initial_state,
                state,
                InitialRegularFixedRunStop.POLICY,
                (OuterSearchIssue("fixed-scenario-policy-stopped", "policy"),),
                steps,
            )
        if action not in legal:
            raise ValueError("policy returned an illegal action")
        before = state
        transition = kernel.apply_action(state, action)
        state = transition.state
        steps.append(
            InitialRegularFixedRunStep(
                index,
                InitialRegularFixedRunStepKind.DECISION,
                before,
                state,
                action=action,
                request=transition.request,
            )
        )

    return _result(
        initial_state,
        state,
        InitialRegularFixedRunStop.TRANSITION_LIMIT,
        (
            OuterSearchIssue(
                "fixed-scenario-transition-limit",
                "max_transitions",
                str(max_transitions),
            ),
        ),
        steps,
    )


__all__ = [
    "InitialRegularFixedBranchSelector",
    "InitialRegularFixedRunResult",
    "InitialRegularFixedRunStep",
    "InitialRegularFixedRunStepKind",
    "InitialRegularFixedRunStop",
    "run_initial_regular_fixed_scenario",
]
