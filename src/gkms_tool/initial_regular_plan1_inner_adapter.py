"""Initial Regular Plan 1 adapter over the bounded native terminal smoke.

The common inner protocol owns request identity and projection.  This adapter
only binds a caller-supplied :class:`Plan1TerminalFixture` (ordered GUID deck,
runtime card objects, and scalar checkpoint) to the existing deterministic
Plan 1 terminal acceptance.  No fixture score or generic stamina formula is
invented here; all terminal values come from the injected scenario run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final, TypeAlias

from .audition_rules import FINAL
from .initial_regular_inner_protocol import (
    InitialRegularInnerStageIssue,
    InitialRegularInnerStageKind,
    InitialRegularInnerStageOutcome,
    InitialRegularInnerStageRequest,
    project_initial_regular_inner_stage_outcome,
    validate_initial_regular_inner_stage_outcome,
    validate_initial_regular_inner_stage_request,
)
from .plan1_native_core import Plan1Blocker
from .initial_regular_plan1_stage_bootstrap import (
    Plan1StageBootstrap,
    provision_plan1_stage,
)
from .plan1_native_terminal_acceptance import (
    Plan1TerminalAcceptance,
    Plan1TerminalAcceptanceLimits,
    Plan1TerminalFixture,
    run_fktn_ssr_plan1_terminal_acceptance,
)
from .plan1_native_stage import Plan1HandAddSupportResolver
from .produce_rollout import ProduceStatePatch, WeeklyActionOutcome


PLAN1_INITIAL_REGULAR_INNER_ADAPTER_ID: Final = (
    "initial-regular.plan1.native-inner"
)
PLAN1_NATIVE_STAGE_PLAN_TYPES: Final = frozenset(
    {"plan1", "ProducePlanType_Plan1"}
)


def _issue(code: str, field: str, detail: str = "") -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1DeterministicScenario:
    """Caller-authoritative ordered deck/runtime and stage-start input."""

    fixture: Plan1TerminalFixture
    limits: Plan1TerminalAcceptanceLimits = Plan1TerminalAcceptanceLimits()
    bootstrap: Plan1StageBootstrap | None = None
    hand_add_support_resolver: Plan1HandAddSupportResolver | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fixture, Plan1TerminalFixture):
            raise TypeError("fixture must be Plan1TerminalFixture")
        if not isinstance(self.limits, Plan1TerminalAcceptanceLimits):
            raise TypeError("limits must be Plan1TerminalAcceptanceLimits")
        if self.bootstrap is not None and not isinstance(
            self.bootstrap, Plan1StageBootstrap
        ):
            raise TypeError("bootstrap must be Plan1StageBootstrap or None")
        if self.hand_add_support_resolver is not None and not callable(
            self.hand_add_support_resolver
        ):
            raise TypeError("hand_add_support_resolver must be callable or None")


Plan1ScenarioProvider: TypeAlias = Callable[
    [InitialRegularInnerStageRequest],
    InitialRegularPlan1DeterministicScenario | Plan1TerminalFixture | None,
]
Plan1RankProvider: TypeAlias = Callable[
    [InitialRegularInnerStageRequest, Plan1TerminalAcceptance], int | None
]


def _blocker_issues(
    blockers: tuple[Plan1Blocker, ...],
    *,
    field: str,
    prefix: str = "plan1-terminal",
) -> tuple[InitialRegularInnerStageIssue, ...]:
    return tuple(
        _issue(f"{prefix}:{blocker.code}", field, blocker.detail)
        for blocker in blockers
    )


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1InnerAdapter:
    """Callable plan adapter with an explicit deterministic scenario provider."""

    scenario_provider: Plan1ScenarioProvider | None = None
    lesson_reward_requested: bool = True
    rank_provider: Plan1RankProvider | None = None

    def __post_init__(self) -> None:
        if self.scenario_provider is not None and not callable(
            self.scenario_provider
        ):
            raise TypeError("scenario_provider must be callable or None")
        if self.rank_provider is not None and not callable(self.rank_provider):
            raise TypeError("rank_provider must be callable or None")
        if not isinstance(self.lesson_reward_requested, bool):
            raise TypeError("lesson_reward_requested must be boolean")

    def _mode_issues(
        self, request: InitialRegularInnerStageRequest
    ) -> tuple[InitialRegularInnerStageIssue, ...]:
        if request.plan_type not in PLAN1_NATIVE_STAGE_PLAN_TYPES:
            return (
                _issue(
                    "plan1-plan-type-mismatch",
                    "request.plan_type",
                    request.plan_type,
                ),
            )
        return ()

    def __call__(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> InitialRegularInnerStageOutcome | tuple[InitialRegularInnerStageIssue, ...]:
        if not isinstance(request, InitialRegularInnerStageRequest):
            raise TypeError("request must be InitialRegularInnerStageRequest")
        request_issues = validate_initial_regular_inner_stage_request(request)
        if request_issues:
            return request_issues
        mode_issues = self._mode_issues(request)
        if mode_issues:
            return mode_issues
        if self.scenario_provider is None:
            return (
                _issue("plan1-provider-missing", "provider.scenario_provider"),
            )
        try:
            scenario = self.scenario_provider(request)
        except Exception as error:
            code = getattr(error, "code", None)
            field = getattr(error, "field", None)
            detail = getattr(error, "detail", "")
            if (
                isinstance(code, str)
                and code
                and isinstance(field, str)
                and field
                and isinstance(detail, str)
            ):
                return (_issue(code, field, detail),)
            return (
                _issue(
                    "plan1-provider-failed",
                    "provider.scenario_provider",
                    f"{type(error).__name__}:{error}",
                ),
            )
        if isinstance(scenario, Plan1TerminalFixture):
            scenario = InitialRegularPlan1DeterministicScenario(
                scenario,
                bootstrap=scenario.bootstrap,
            )
        if not isinstance(scenario, InitialRegularPlan1DeterministicScenario):
            return (
                _issue(
                    "plan1-provider-result-invalid",
                    "provider.scenario_provider",
                ),
            )
        provision = provision_plan1_stage(
            request,
            scenario.bootstrap,
            scenario.fixture.stage,
        )
        if provision.issues:
            return tuple(
                _issue(issue.code, issue.field, issue.detail)
                for issue in provision.issues
            )
        assert provision.stage is not None
        assert scenario.bootstrap is not None
        fixture = replace(
            scenario.fixture,
            stage=provision.stage,
            bootstrap=scenario.bootstrap,
        )
        if fixture.blockers:
            return _blocker_issues(
                fixture.blockers,
                field="provider.scenario_provider",
                prefix="plan1-scenario",
            )
        try:
            acceptance = run_fktn_ssr_plan1_terminal_acceptance(
                fixture=fixture,
                limits=scenario.limits,
                hand_add_support_resolver=scenario.hand_add_support_resolver,
            )
        except Exception as error:
            return (
                _issue(
                    "plan1-terminal-run-failed",
                    "plan1.terminal",
                    f"{type(error).__name__}:{error}",
                ),
            )
        if acceptance.blockers or not acceptance.completed or not acceptance.terminal:
            blockers = acceptance.blockers or (
                Plan1Blocker(
                    "terminal-incomplete",
                    "FKTN-SSR-Plan1-terminal",
                    "terminal acceptance did not complete",
                ),
            )
            return _blocker_issues(blockers, field="plan1.terminal")

        outcome = self._outcome_from_acceptance(request, acceptance)
        if isinstance(outcome, tuple):
            return outcome
        outcome_issues = validate_initial_regular_inner_stage_outcome(
            request, outcome
        )
        return outcome_issues or outcome

    def _outcome_from_acceptance(
        self,
        request: InitialRegularInnerStageRequest,
        acceptance: Plan1TerminalAcceptance,
    ) -> InitialRegularInnerStageOutcome | tuple[InitialRegularInnerStageIssue, ...]:
        score = acceptance.score
        if request.clear_border is None:
            return (_issue("plan1-clear-border-unresolved", "request.clear_border"),)
        rank: int | None = None
        if request.stage_kind is InitialRegularInnerStageKind.LESSON:
            if request.clear_border < 0:
                return (
                    _issue(
                        "plan1-clear-border-invalid",
                        "request.clear_border",
                    ),
                )
            cleared = score >= request.clear_border
            terminal = False
            reward_requested = self.lesson_reward_requested
        else:
            if request.clear_border < 1:
                return (
                    _issue(
                        "plan1-audition-clear-border-unresolved",
                        "request.clear_border",
                    ),
                )
            if self.rank_provider is None:
                return (
                    _issue("plan1-audition-rank-unresolved", "provider.rank_provider"),
                )
            try:
                rank = self.rank_provider(request, acceptance)
            except Exception as error:
                return (
                    _issue(
                        "plan1-rank-provider-failed",
                        "provider.rank_provider",
                        f"{type(error).__name__}:{error}",
                    ),
                )
            if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
                return (
                    _issue("plan1-rank-invalid", "provider.rank_provider"),
                )
            cleared = rank <= request.clear_border
            terminal = request.stage_type == FINAL
            reward_requested = self.lesson_reward_requested
        assert acceptance.run is not None
        final_zones = acceptance.run.final.zones
        patch = ProduceStatePatch(stamina=acceptance.stamina)
        trace = (
            f"adapter:{PLAN1_INITIAL_REGULAR_INNER_ADAPTER_ID}",
            "authority:caller-deterministic-scenario",
            f"bootstrap-authority:{acceptance.fixture.bootstrap.authority.value}",
            *(f"action:{step.action.card.card_id}:{step.action.guid}" for step in acceptance.run.steps),
            f"terminal-score:{score}",
            f"terminal-zones:grave={len(final_zones.grave)}:lost={len(final_zones.lost)}",
            f"terminal-stamina:{acceptance.stamina}",
            f"terminal-parameter-buff-turns:{acceptance.parameter_buff_turns}",
            f"terminal-parameter-buff-multiple-per-turn-turns:{acceptance.parameter_buff_multiple_per_turn_turns}",
        )
        return InitialRegularInnerStageOutcome(
            request_id=request.external_request_id,
            branch=request.branch,
            patch=patch,
            cleared=cleared,
            terminal=terminal,
            reward_requested=reward_requested,
            rng_after=final_zones.random_state,
            score=score,
            rank=rank,
            trace=trace,
        )


def project_initial_regular_plan1_weekly_action_outcome(
    request: InitialRegularInnerStageRequest,
    outcome: InitialRegularInnerStageOutcome,
) -> WeeklyActionOutcome:
    """Project one validated Plan 1 lesson result to WeeklyActionOutcome."""

    if request.stage_kind is not InitialRegularInnerStageKind.LESSON:
        raise ValueError("Plan 1 WeeklyActionOutcome requires a lesson request")
    projected = project_initial_regular_inner_stage_outcome(request, outcome)
    if not isinstance(projected, WeeklyActionOutcome):
        raise TypeError("inner protocol projected an unexpected outcome type")
    return projected


def build_initial_regular_plan1_inner_adapter(
    **kwargs: object,
) -> InitialRegularPlan1InnerAdapter:
    """Factory convenient for common registry construction."""

    return InitialRegularPlan1InnerAdapter(**kwargs)  # type: ignore[arg-type]


InitialRegularPlan1InnerAdapterResult: TypeAlias = (
    InitialRegularInnerStageOutcome
    | tuple[InitialRegularInnerStageIssue, ...]
)


__all__ = [
    "PLAN1_INITIAL_REGULAR_INNER_ADAPTER_ID",
    "PLAN1_NATIVE_STAGE_PLAN_TYPES",
    "InitialRegularPlan1DeterministicScenario",
    "InitialRegularPlan1InnerAdapter",
    "InitialRegularPlan1InnerAdapterResult",
    "Plan1RankProvider",
    "Plan1ScenarioProvider",
    "build_initial_regular_plan1_inner_adapter",
    "project_initial_regular_plan1_weekly_action_outcome",
]
