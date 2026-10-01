"""Offline scenario-provider wire for regular Initial Produce lessons.

The lesson catalog owns the static stage rows and SP probabilities.  The
scenario owns runtime-only values (the selected SP rows, native RNG roots,
and the current passive modifiers).  This module only joins those two typed
inputs and returns the plan-neutral inner branches consumed by the offline
chance adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .initial_regular_lesson_branch_catalog import (
    DEFAULT_MASTER_DIR,
    InitialRegularInnerLessonBranchExecution,
    InitialRegularInnerLessonExecutionInputs,
    InitialRegularLessonRuntimeContext,
    InitialRegularSpRatePermils,
    build_initial_regular_lesson_branch_catalog,
    build_initial_regular_weighted_weekly_frontier,
)
from .initial_regular_offline_search import InitialRegularOfflineBranchExpansion
from .initial_regular_weekly_adapter import InitialRegularLessonAttribute
from .loadout_runtime_bridge import PreparedLoadoutRuntime
from .passive_runtime import PassiveRuntimeResult, RunModifiers
from .produce_rollout import ExternalRequest, ProduceRolloutState
from .produce_rollout_expectimax import OuterSearchIssue


@dataclass(frozen=True, slots=True)
class InitialRegularLessonScenarioInput:
    """Exact runtime inputs for one player-selected lesson attribute."""

    attribute: InitialRegularLessonAttribute | str
    idol_card_id: str
    runtime_context: InitialRegularLessonRuntimeContext
    branches: tuple[InitialRegularInnerLessonBranchExecution, ...]
    base_sp_rate_permils: InitialRegularSpRatePermils | None = None
    passive_runtime: (
        PassiveRuntimeResult | PreparedLoadoutRuntime | RunModifiers | None
    ) = None
    sp_modifiers_known_absent: bool | None = None
    selected_sp_stage_ids: tuple[
        tuple[InitialRegularLessonAttribute, str], ...
    ] = ()

    def __post_init__(self) -> None:
        try:
            attribute = InitialRegularLessonAttribute(self.attribute)
        except (TypeError, ValueError) as error:
            raise ValueError("lesson scenario attribute is invalid") from error
        object.__setattr__(self, "attribute", attribute)
        if not isinstance(self.idol_card_id, str) or not self.idol_card_id.strip():
            raise ValueError("lesson scenario idol_card_id must be non-empty text")
        if not isinstance(self.runtime_context, InitialRegularLessonRuntimeContext):
            raise TypeError("lesson scenario runtime_context must be typed")
        branches = tuple(self.branches)
        if not all(
            isinstance(value, InitialRegularInnerLessonBranchExecution)
            for value in branches
        ):
            raise TypeError("lesson scenario branches must be typed")
        branch_keys = tuple(value.branch_key for value in branches)
        if len(branch_keys) != len(set(branch_keys)):
            raise ValueError("lesson scenario branch keys must be unique")
        object.__setattr__(self, "branches", branches)
        if self.base_sp_rate_permils is not None and not isinstance(
            self.base_sp_rate_permils, InitialRegularSpRatePermils
        ):
            raise TypeError("base_sp_rate_permils must be typed or None")
        if self.passive_runtime is not None and not isinstance(
            self.passive_runtime,
            (PassiveRuntimeResult, PreparedLoadoutRuntime, RunModifiers),
        ):
            raise TypeError("passive_runtime must be a supported typed runtime or None")
        if self.sp_modifiers_known_absent is not None and not isinstance(
            self.sp_modifiers_known_absent, bool
        ):
            raise TypeError("sp_modifiers_known_absent must be bool or None")
        selected = tuple(self.selected_sp_stage_ids)
        normalized: list[tuple[InitialRegularLessonAttribute, str]] = []
        for raw_attribute, stage_id in selected:
            try:
                selected_attribute = InitialRegularLessonAttribute(raw_attribute)
            except (TypeError, ValueError) as error:
                raise ValueError("selected SP attribute is invalid") from error
            if not isinstance(stage_id, str) or not stage_id.strip():
                raise ValueError("selected SP stage id must be non-empty text")
            normalized.append((selected_attribute, stage_id))
        selected_attributes = tuple(value[0] for value in normalized)
        if len(selected_attributes) != len(set(selected_attributes)):
            raise ValueError("selected SP attributes must be unique")
        object.__setattr__(self, "selected_sp_stage_ids", tuple(normalized))

    @property
    def selected_sp_stage_map(self) -> Mapping[InitialRegularLessonAttribute, str]:
        return dict(self.selected_sp_stage_ids)


def build_initial_regular_lesson_offline_branches(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularLessonScenarioInput,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> InitialRegularOfflineBranchExpansion:
    """Build common inner lesson requests while preserving every blocker."""

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(scenario, InitialRegularLessonScenarioInput):
        raise TypeError("scenario must be InitialRegularLessonScenarioInput")
    catalog = build_initial_regular_lesson_branch_catalog(
        state,
        request,
        idol_card_id=scenario.idol_card_id,
        base_sp_rate_permils=scenario.base_sp_rate_permils,
        passive_runtime=scenario.passive_runtime,
        sp_modifiers_known_absent=scenario.sp_modifiers_known_absent,
        selected_sp_stage_ids=scenario.selected_sp_stage_map,
        runtime_context=scenario.runtime_context,
        master_dir=master_dir,
    )
    if catalog.issues:
        return InitialRegularOfflineBranchExpansion.paused(
            *(
                OuterSearchIssue(
                    f"lesson-catalog-{issue.code}",
                    f"lesson.{issue.field}",
                    issue.detail,
                )
                for issue in catalog.issues
            )
        )
    return build_initial_regular_weighted_weekly_frontier(
        catalog,
        scenario.attribute,
        inner_execution=InitialRegularInnerLessonExecutionInputs(
            scenario.idol_card_id,
            scenario.branches,
        ),
    )


__all__ = [
    "InitialRegularLessonScenarioInput",
    "build_initial_regular_lesson_offline_branches",
]
