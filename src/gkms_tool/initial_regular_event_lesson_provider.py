"""Offline branch provider wire for an event-022 same-week Plan2 lesson."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .initial_regular_event_lesson_catalog import (
    DEFAULT_MASTER_DIR,
    InitialRegularEventLessonCatalog,
    InitialRegularEventLessonRuntimeFacts,
    build_initial_regular_event_lesson_inner_stage_request,
)
from .initial_regular_inner_protocol import InitialRegularInnerRuntimeRefs
from .initial_regular_offline_search import (
    InitialRegularOfflineBranchExpansion,
    InitialRegularWeightedInnerBranch,
)
from .master_db import DEFAULT_DATABASE
from .produce_rollout import ChanceBranch, ExternalRequest, ProduceRolloutState
from .produce_rollout_expectimax import OuterSearchIssue


@dataclass(frozen=True, slots=True)
class InitialRegularEventLessonScenarioInput:
    probability: Fraction
    branch_id: str
    rng_before: int
    idol_card_id: str
    runtime_facts: InitialRegularEventLessonRuntimeFacts
    rng_token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.probability, Fraction):
            raise TypeError("event lesson probability must be Fraction")
        if self.probability <= 0 or self.probability > 1:
            raise ValueError("event lesson probability must be in (0, 1]")
        for name in ("branch_id", "idol_card_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"event lesson {name} must be non-empty text")
        if self.rng_token is not None and (
            not isinstance(self.rng_token, str) or not self.rng_token.strip()
        ):
            raise ValueError("event lesson rng_token cannot be empty")
        if (
            isinstance(self.rng_before, bool)
            or not isinstance(self.rng_before, int)
            or not 0 <= self.rng_before <= 0xFFFFFFFF
        ):
            raise ValueError("event lesson rng_before must be uint32")
        if not isinstance(
            self.runtime_facts, InitialRegularEventLessonRuntimeFacts
        ):
            raise TypeError("event lesson runtime_facts must be typed")


def build_initial_regular_event_lesson_offline_branch(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularEventLessonScenarioInput,
    *,
    catalog: InitialRegularEventLessonCatalog | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
    database: Path = DEFAULT_DATABASE,
) -> InitialRegularOfflineBranchExpansion:
    """Build the common inner request or preserve every typed blocker."""

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(scenario, InitialRegularEventLessonScenarioInput):
        raise TypeError("scenario must be InitialRegularEventLessonScenarioInput")
    branch = ChanceBranch(
        branch_id=scenario.branch_id,
        probability=float(scenario.probability),
        rng_token=scenario.rng_token,
        event_id=request.action_id,
    )
    runtime_refs = InitialRegularInnerRuntimeRefs(
        item_session_refs=state.item_session_refs,
        drink_session_refs=state.drink_session_refs,
        passive_session_refs=state.passive_session_refs,
    )
    built = build_initial_regular_event_lesson_inner_stage_request(
        state,
        request,
        branch=branch,
        rng_before=scenario.rng_before,
        idol_card_id=scenario.idol_card_id,
        runtime_refs=runtime_refs,
        runtime_facts=scenario.runtime_facts,
        catalog=catalog,
        master_dir=master_dir,
        database=database,
    )
    if built.issues:
        return InitialRegularOfflineBranchExpansion.paused(
            *(
                OuterSearchIssue(
                    f"event-lesson-{issue.code}",
                    f"event_lesson.{issue.field}",
                    issue.detail,
                )
                for issue in built.issues
            )
        )
    if built.request is None:
        return InitialRegularOfflineBranchExpansion.paused(
            OuterSearchIssue(
                "event-lesson-inner-request-missing",
                "event_lesson.request",
            )
        )
    return InitialRegularOfflineBranchExpansion.resolved(
        InitialRegularWeightedInnerBranch(
            scenario.probability,
            built.request,
        )
    )


__all__ = [
    "InitialRegularEventLessonScenarioInput",
    "build_initial_regular_event_lesson_offline_branch",
]
