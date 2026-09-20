"""Run one already-bootstrapped Plan2 lesson or audition to terminal.

The stage bootstrap owns identity, RNG, deck, loadout runtime, and native
mode construction.  This module owns only the repeated planner -> transition
loop.  It is therefore reusable by Normal/SP/Hard lessons and MID1/FINAL
auditions, and exposes the exact final state needed by the outer simulator.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from .plan2_native_expectimax import (
    Plan2NativeEvaluationWeights,
    Plan2NativeExpectimaxError,
    Plan2NativeExpectimaxLimits,
    Plan2NativeExpectimaxResult,
    plan_plan2_native_expectimax,
)
from .plan2_native_horizon import (
    Plan2NativeAction,
    Plan2NativeBlocker,
    Plan2NativeDrinkAction,
    Plan2NativeHorizonState,
    Plan2NativeOfflineAction,
    Plan2NativeProgramCatalog,
    Plan2NativeTransition,
    simulate_plan2_native_action,
)
from .plan2_native_stage_bootstrap import Plan2NativeStageBootstrap


STOP_TERMINAL: Final = "terminal"
STOP_BOOTSTRAP_BLOCKED: Final = "bootstrap-blocked"
STOP_PLANNER_BLOCKED: Final = "planner-blocked"
STOP_TRANSITION_BLOCKED: Final = "transition-blocked"
STOP_ACTION_LIMIT: Final = "action-limit"

StagePlanner = Callable[
    [
        Plan2NativeHorizonState,
        Plan2NativeProgramCatalog,
        Plan2NativeEvaluationWeights,
        Plan2NativeExpectimaxLimits,
    ],
    Plan2NativeExpectimaxResult,
]
StageTransitioner = Callable[
    [
        Plan2NativeHorizonState,
        Plan2NativeOfflineAction,
        Plan2NativeProgramCatalog,
    ],
    Plan2NativeTransition,
]


def _default_planner(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    weights: Plan2NativeEvaluationWeights,
    limits: Plan2NativeExpectimaxLimits,
) -> Plan2NativeExpectimaxResult:
    return plan_plan2_native_expectimax(
        state,
        catalog,
        weights=weights,
        limits=limits,
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeStageRunStep:
    index: int
    action: Plan2NativeOfflineAction
    predicted_value: float
    principal_path: tuple[str, ...]
    before: Plan2NativeHorizonState
    after: Plan2NativeHorizonState
    transition_trace: tuple[str, ...]
    search: Plan2NativeExpectimaxResult

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 1:
            raise ValueError("stage run step index must be positive")
        if not isinstance(
            self.action, (Plan2NativeAction, Plan2NativeDrinkAction)
        ):
            raise TypeError("stage run action must be a typed offline action")
        if not isinstance(self.search, Plan2NativeExpectimaxResult):
            raise TypeError("stage run search must be Plan2NativeExpectimaxResult")


@dataclass(frozen=True, slots=True)
class Plan2NativeStageRunResult:
    initial_state: Plan2NativeHorizonState | None
    final_state: Plan2NativeHorizonState | None
    stop_reason: str
    blockers: tuple[Plan2NativeBlocker, ...]
    steps: tuple[Plan2NativeStageRunStep, ...]

    @property
    def terminal_reached(self) -> bool:
        return self.final_state is not None and self.final_state.terminal

    @property
    def completed(self) -> bool:
        return self.terminal_reached and not self.blockers

    @property
    def action_path(self) -> tuple[str, ...]:
        return tuple(step.action.action_id for step in self.steps)


def _result(
    initial: Plan2NativeHorizonState | None,
    final: Plan2NativeHorizonState | None,
    stop_reason: str,
    blockers: tuple[Plan2NativeBlocker, ...],
    steps: list[Plan2NativeStageRunStep],
) -> Plan2NativeStageRunResult:
    return Plan2NativeStageRunResult(
        initial_state=initial,
        final_state=final,
        stop_reason=stop_reason,
        blockers=tuple(dict.fromkeys(blockers)),
        steps=tuple(steps),
    )


def run_plan2_native_stage_to_terminal(
    bootstrap: Plan2NativeStageBootstrap,
    catalog: Plan2NativeProgramCatalog,
    *,
    max_actions: int = 100,
    weights: Plan2NativeEvaluationWeights = Plan2NativeEvaluationWeights(),
    limits: Plan2NativeExpectimaxLimits = Plan2NativeExpectimaxLimits(),
    planner: StagePlanner = _default_planner,
    transitioner: StageTransitioner = simulate_plan2_native_action,
) -> Plan2NativeStageRunResult:
    """Repeatedly choose and apply one native action until a hard boundary."""

    if not isinstance(bootstrap, Plan2NativeStageBootstrap):
        raise TypeError("bootstrap must be Plan2NativeStageBootstrap")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if type(max_actions) is not int or max_actions < 1:
        raise ValueError("max_actions must be a positive integer")
    if not isinstance(weights, Plan2NativeEvaluationWeights):
        raise TypeError("weights must be Plan2NativeEvaluationWeights")
    if not isinstance(limits, Plan2NativeExpectimaxLimits):
        raise TypeError("limits must be Plan2NativeExpectimaxLimits")
    if not callable(planner) or not callable(transitioner):
        raise TypeError("planner and transitioner must be callable")

    if not bootstrap.simulation_ready or bootstrap.state is None:
        blockers = bootstrap.blockers or (
            Plan2NativeBlocker("stage-bootstrap-state-missing"),
        )
        return _result(None, None, STOP_BOOTSTRAP_BLOCKED, blockers, [])

    initial = bootstrap.state
    state = initial
    steps: list[Plan2NativeStageRunStep] = []
    if state.terminal:
        return _result(initial, state, STOP_TERMINAL, (), steps)

    for index in range(1, max_actions + 1):
        try:
            search = planner(state, catalog, weights, limits)
        except Plan2NativeExpectimaxError as error:
            return _result(
                initial,
                state,
                STOP_PLANNER_BLOCKED,
                (error.blocker,),
                steps,
            )
        if not isinstance(search, Plan2NativeExpectimaxResult):
            return _result(
                initial,
                state,
                STOP_PLANNER_BLOCKED,
                (Plan2NativeBlocker("stage-planner-contract"),),
                steps,
            )
        if search.best_action is None:
            return _result(
                initial,
                state,
                STOP_PLANNER_BLOCKED,
                search.blockers
                or (Plan2NativeBlocker("stage-planner-no-action"),),
                steps,
            )

        transition = transitioner(state, search.best_action, catalog)
        if not isinstance(transition, Plan2NativeTransition):
            return _result(
                initial,
                state,
                STOP_TRANSITION_BLOCKED,
                (Plan2NativeBlocker("stage-transition-contract"),),
                steps,
            )
        if not transition.supported or transition.after is None:
            return _result(
                initial,
                state,
                STOP_TRANSITION_BLOCKED,
                transition.blockers
                or (Plan2NativeBlocker("stage-transition-unsupported"),),
                steps,
            )

        after = transition.after
        steps.append(
            Plan2NativeStageRunStep(
                index=index,
                action=search.best_action,
                predicted_value=search.value,
                principal_path=search.action_path,
                before=state,
                after=after,
                transition_trace=transition.trace,
                search=search,
            )
        )
        state = after
        if state.terminal:
            return _result(initial, state, STOP_TERMINAL, (), steps)

    return _result(
        initial,
        state,
        STOP_ACTION_LIMIT,
        (Plan2NativeBlocker("stage-action-limit", str(max_actions)),),
        steps,
    )


__all__ = [
    "Plan2NativeStageRunResult",
    "Plan2NativeStageRunStep",
    "STOP_ACTION_LIMIT",
    "STOP_BOOTSTRAP_BLOCKED",
    "STOP_PLANNER_BLOCKED",
    "STOP_TERMINAL",
    "STOP_TRANSITION_BLOCKED",
    "StagePlanner",
    "StageTransitioner",
    "run_plan2_native_stage_to_terminal",
]
