"""One-week Initial Regular Plan 1 outer acceptance seam.

This module deliberately reuses :class:`ProduceRolloutKernel`: it applies one
calendar-approved lesson action, builds the common ``InnerStageRequest`` from
the existing lesson-stage contract, invokes the injected Plan 1 terminal
adapter, projects ``WeeklyActionOutcome``, and lets the kernel create the
``REWARD_OFFERS`` request.  It is not a second outer state machine and does not
run the thirteen-week route.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, TypeAlias

from .initial_regular_inner_protocol import (
    InitialRegularInnerStageAdapterResult,
    InitialRegularInnerStageIssue,
    InitialRegularInnerStageRequest,
    InitialRegularLessonRuntimeContextContract,
    InitialRegularLessonStageFactContract,
    build_initial_regular_lesson_stage_request,
)
from .initial_regular_plan1_inner_adapter import (
    InitialRegularPlan1DeterministicScenario,
    InitialRegularPlan1InnerAdapter,
    Plan1ScenarioProvider,
    project_initial_regular_plan1_weekly_action_outcome,
)
from .plan1_native_terminal_acceptance import Plan1TerminalFixture
from .produce_rollout import (
    ChanceBranch,
    ExternalKind,
    ExternalRequest,
    ProduceAction,
    ProduceRolloutKernel,
    ProduceRolloutState,
    RewardOffer,
    RewardOffersOutcome,
    RolloutPhase,
    WeeklyActionOutcome,
)
from .route_calendar import LESSON


PLAN1_OUTER_ACCEPTANCE_ID: Final = "initial-regular.plan1.lesson-week"


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1LessonRuntimeContext:
    """Caller/server-owned runtime rows needed by the common request builder."""

    exam_extra_turn: int = 0
    battle_bonus_permille: tuple[int, int, int] = (1000, 1000, 1000)
    gimmick_group_id: str = ""

    def __post_init__(self) -> None:
        if type(self.exam_extra_turn) is not int or self.exam_extra_turn < 0:
            raise ValueError("exam_extra_turn must be a non-negative integer")
        bonuses = tuple(self.battle_bonus_permille)
        if len(bonuses) != 3 or any(type(value) is not int or value < 0 for value in bonuses):
            raise ValueError("battle_bonus_permille must contain three non-negative integers")
        if not isinstance(self.gimmick_group_id, str):
            raise TypeError("gimmick_group_id must be text")
        object.__setattr__(self, "battle_bonus_permille", bonuses)


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1LessonStageFact:
    """Small explicit lesson fact accepted by the common request builder."""

    stage_id: str = "plan1:lesson"
    plan_type: str = "plan1"
    setting_id: str = "p_exam_setting-1"
    lesson_type: str = "ProduceStepLessonType_Vocal"
    step_type_value: int = 1
    base_turns: int = 11
    runtime_turns: int | None = 11
    clear_border: int = 3
    limit_border: int | None = -1
    attribute: str = "vocal"
    is_sp: bool = False
    runtime_context: InitialRegularLessonRuntimeContextContract | None = (
        InitialRegularPlan1LessonRuntimeContext()
    )


Plan1LessonStageFactProvider: TypeAlias = Callable[
    [ProduceRolloutState, ExternalRequest],
    InitialRegularLessonStageFactContract,
]


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1LessonWeekAcceptance:
    """All checkpoints for one lesson -> WeeklyAction -> reward-offer seam."""

    initial_state: ProduceRolloutState
    after_action_state: ProduceRolloutState | None
    inner_request: InitialRegularInnerStageRequest | None
    inner_result: InitialRegularStageAdapterResult | None
    weekly_outcome: WeeklyActionOutcome | None
    reward_request: ExternalRequest | None
    reward_outcome: RewardOffersOutcome | None
    final_state: ProduceRolloutState | None
    branch: ChanceBranch
    deterministic_server_scenario: bool
    issues: tuple[InitialRegularInnerStageIssue, ...] = ()

    @property
    def completed(self) -> bool:
        return not self.issues and self.final_state is not None

    @property
    def reward_offers_ready(self) -> bool:
        return bool(
            self.completed
            and self.final_state is not None
            and self.final_state.phase is RolloutPhase.READY_FOR_REWARD
            and self.reward_outcome is not None
        )


def _issue(code: str, field: str, detail: str = "") -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


def _failed(
    initial: ProduceRolloutState,
    branch: ChanceBranch,
    code: str,
    field: str,
    detail: str = "",
    *,
    after_action_state: ProduceRolloutState | None = None,
    inner_request: InitialRegularInnerStageRequest | None = None,
    inner_result: InitialRegularStageAdapterResult | None = None,
    weekly_outcome: WeeklyActionOutcome | None = None,
    reward_request: ExternalRequest | None = None,
    reward_outcome: RewardOffersOutcome | None = None,
    final_state: ProduceRolloutState | None = None,
) -> InitialRegularPlan1LessonWeekAcceptance:
    return InitialRegularPlan1LessonWeekAcceptance(
        initial,
        after_action_state,
        inner_request,
        inner_result,
        weekly_outcome,
        reward_request,
        reward_outcome,
        final_state,
        branch,
        True,
        (_issue(code, field, detail),),
    )


def run_initial_regular_plan1_lesson_week(
    initial_state: ProduceRolloutState,
    *,
    kernel: ProduceRolloutKernel,
    idol_card_id: str,
    stage_fact: InitialRegularLessonStageFactContract | None = None,
    stage_fact_provider: Plan1LessonStageFactProvider | None = None,
    scenario: InitialRegularPlan1DeterministicScenario | Plan1TerminalFixture | None = None,
    scenario_provider: Plan1ScenarioProvider | None = None,
    branch: ChanceBranch,
    rng_before: int,
    reward_offers: Sequence[RewardOffer],
    reward_exclude_count: int = 0,
    action_id: str = LESSON,
) -> InitialRegularPlan1LessonWeekAcceptance:
    """Accept exactly one calendar lesson node and stop at ``REWARD_OFFERS``.

    ``branch``, ``stage_fact``, ``scenario`` and ``reward_offers`` are explicit
    server/caller-owned deterministic inputs.  Missing or conflicting seams
    return typed issues instead of fabricating a route, score, or reward.
    """

    if not isinstance(initial_state, ProduceRolloutState):
        raise TypeError("initial_state must be ProduceRolloutState")
    if not isinstance(kernel, ProduceRolloutKernel):
        raise TypeError("kernel must be ProduceRolloutKernel")
    if not isinstance(idol_card_id, str) or not idol_card_id:
        raise ValueError("idol_card_id must be non-empty text")
    if not isinstance(branch, ChanceBranch):
        raise TypeError("branch must be ChanceBranch")
    if type(rng_before) is not int or not 0 <= rng_before <= 0xFFFFFFFF:
        raise ValueError("rng_before must be a UInt32 integer")
    offers = tuple(reward_offers)
    if not offers or any(not isinstance(offer, RewardOffer) for offer in offers):
        raise ValueError("reward_offers must contain at least one RewardOffer")
    if type(reward_exclude_count) is not int or reward_exclude_count < 0:
        raise ValueError("reward_exclude_count must be non-negative")
    kernel.validate_state(initial_state)
    if initial_state.phase is not RolloutPhase.READY_FOR_WEEK or initial_state.pending is not None:
        return _failed(
            initial_state,
            branch,
            "plan1-outer-state-not-ready",
            "initial_state.phase",
        )
    if stage_fact is not None and stage_fact_provider is not None:
        return _failed(
            initial_state,
            branch,
            "plan1-stage-fact-input-conflict",
            "stage_fact/stage_fact_provider",
        )
    if scenario is not None and scenario_provider is not None:
        return _failed(
            initial_state,
            branch,
            "plan1-scenario-input-conflict",
            "scenario/scenario_provider",
        )
    if scenario is None and scenario_provider is None:
        return _failed(
            initial_state,
            branch,
            "plan1-scenario-required",
            "scenario/scenario_provider",
        )
    if stage_fact_provider is not None and not callable(stage_fact_provider):
        raise TypeError("stage_fact_provider must be callable")
    if scenario_provider is not None and not callable(scenario_provider):
        raise TypeError("scenario_provider must be callable")

    try:
        transition = kernel.apply_action(
            initial_state,
            ProduceAction.weekly(action_id),
        )
    except Exception as error:
        return _failed(
            initial_state,
            branch,
            "plan1-calendar-action-failed",
            "kernel.apply_action",
            f"{type(error).__name__}:{error}",
        )
    after_action = transition.state
    pending = transition.request
    if pending is None or after_action.pending is None:
        return _failed(
            initial_state,
            branch,
            "plan1-weekly-request-missing",
            "kernel.apply_action.request",
            after_action.phase.value,
            after_action_state=after_action,
        )
    if pending.kind is not ExternalKind.WEEKLY_ACTION_OUTCOME:
        return _failed(
            initial_state,
            branch,
            "plan1-weekly-request-kind-invalid",
            "kernel.apply_action.request.kind",
            pending.kind.value,
            after_action_state=after_action,
        )

    try:
        resolved_fact = (
            stage_fact
            if stage_fact is not None
            else stage_fact_provider(initial_state, pending)  # type: ignore[misc]
        )
    except Exception as error:
        return _failed(
            initial_state,
            branch,
            "plan1-stage-fact-provider-failed",
            "stage_fact_provider",
            f"{type(error).__name__}:{error}",
            after_action_state=after_action,
        )
    if resolved_fact is None:
        return _failed(
            initial_state,
            branch,
            "plan1-stage-fact-missing",
            "stage_fact",
            after_action_state=after_action,
        )
    try:
        request = build_initial_regular_lesson_stage_request(
            after_action,
            pending,
            resolved_fact,
            branch=branch,
            idol_card_id=idol_card_id,
            rng_before=rng_before,
        )
    except Exception as error:
        return _failed(
            initial_state,
            branch,
            "plan1-inner-request-build-failed",
            "stage_fact",
            f"{type(error).__name__}:{error}",
            after_action_state=after_action,
        )

    if scenario_provider is not None:
        adapter = InitialRegularPlan1InnerAdapter(scenario_provider=scenario_provider)
    else:
        adapter = InitialRegularPlan1InnerAdapter(scenario_provider=lambda _request: scenario)
    inner_result = adapter(request)
    if isinstance(inner_result, tuple):
        return _failed(
            initial_state,
            branch,
            "plan1-inner-adapter-blocked",
            "inner_result",
            ",".join(issue.code for issue in inner_result),
            after_action_state=after_action,
            inner_request=request,
            inner_result=inner_result,
        )
    try:
        weekly = project_initial_regular_plan1_weekly_action_outcome(
            request, inner_result
        )
        after_weekly = kernel.resolve_external(after_action, weekly).state
    except Exception as error:
        return _failed(
            initial_state,
            branch,
            "plan1-weekly-outcome-apply-failed",
            "kernel.resolve_external",
            f"{type(error).__name__}:{error}",
            after_action_state=after_action,
            inner_request=request,
            inner_result=inner_result,
        )
    reward_request = after_weekly.pending
    if (
        reward_request is None
        or reward_request.kind is not ExternalKind.REWARD_OFFERS
        or after_weekly.phase is not RolloutPhase.WAITING_EXTERNAL
    ):
        return _failed(
            initial_state,
            branch,
            "plan1-reward-request-missing",
            "after_weekly.pending",
            after_weekly.phase.value,
            after_action_state=after_action,
            inner_request=request,
            inner_result=inner_result,
            weekly_outcome=weekly,
            final_state=after_weekly,
        )
    reward = RewardOffersOutcome(
        request_id=reward_request.request_id,
        branch=branch,
        offers=offers,
        remaining_exclude_count=reward_exclude_count,
    )
    try:
        final = kernel.resolve_external(after_weekly, reward).state
    except Exception as error:
        return _failed(
            initial_state,
            branch,
            "plan1-reward-offers-apply-failed",
            "kernel.resolve_external.reward_offers",
            f"{type(error).__name__}:{error}",
            after_action_state=after_action,
            inner_request=request,
            inner_result=inner_result,
            weekly_outcome=weekly,
            reward_request=reward_request,
            reward_outcome=reward,
            final_state=after_weekly,
        )
    return InitialRegularPlan1LessonWeekAcceptance(
        initial_state=initial_state,
        after_action_state=after_action,
        inner_request=request,
        inner_result=inner_result,
        weekly_outcome=weekly,
        reward_request=reward_request,
        reward_outcome=reward,
        final_state=final,
        branch=branch,
        deterministic_server_scenario=True,
        issues=(),
    )


__all__ = [
    "PLAN1_OUTER_ACCEPTANCE_ID",
    "InitialRegularPlan1LessonRuntimeContext",
    "InitialRegularPlan1LessonStageFact",
    "InitialRegularPlan1LessonWeekAcceptance",
    "Plan1LessonStageFactProvider",
    "run_initial_regular_plan1_lesson_week",
]
