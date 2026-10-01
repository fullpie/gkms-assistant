"""Complete offline chance frontiers for Initial Regular outer events.

The Master database owns a selected suggestion's conditional success rate,
but it does not expose the server's event-detail roll.  Consequently callers
must supply exact full-branch weights and settled response scenarios.  This
module verifies that the weights form a complete frontier and that, within
each exact detail/suggestion group, the success/fail ratio agrees with Master.
It never invents an event-detail distribution or reward-set membership.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .initial_regular_event_effect_catalog import (
    InitialRegularEventEffectCatalog,
    InitialRegularEventEffectCatalogError,
    load_initial_regular_event_effect_catalog,
)
from .initial_regular_event_scenario import InitialRegularEventScenario
from .initial_regular_event_scenario_adapter import (
    EVENT_SCENARIO_ADAPTER_REF,
    InitialRegularEventScenarioAdapterPause,
    adapt_initial_regular_event_scenario,
)
from .initial_regular_offline_search import (
    InitialRegularOfflineBranchExpansion,
    InitialRegularWeightedResolvedWeeklyBranch,
)
from .master_db import DEFAULT_DATABASE
from .produce_rollout import (
    ChanceBranch,
    ExternalKind,
    ExternalRequest,
    ProduceRolloutState,
)
from .produce_rollout_expectimax import OuterSearchIssue
from .route_calendar import CLASS, OUTING


@dataclass(frozen=True, slots=True)
class InitialRegularWeightedEventScenario:
    """One settled response scenario and its complete outer probability."""

    probability: Fraction
    branch_id: str
    scenario: InitialRegularEventScenario
    rng_token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.probability, Fraction):
            raise TypeError("event scenario probability must be Fraction")
        if self.probability <= 0 or self.probability > 1:
            raise ValueError("event scenario probability must be in (0, 1]")
        if not isinstance(self.branch_id, str) or not self.branch_id.strip():
            raise ValueError("event scenario branch_id must be non-empty text")
        if not isinstance(self.scenario, InitialRegularEventScenario):
            raise TypeError("event scenario must be InitialRegularEventScenario")
        if self.rng_token is not None and (
            not isinstance(self.rng_token, str) or not self.rng_token.strip()
        ):
            raise ValueError("event scenario rng_token cannot be empty")


@dataclass(frozen=True, slots=True)
class InitialRegularEventScenarioFrontier:
    """Caller-owned server event distribution for one pending boundary.

    ``complete`` is deliberately explicit.  A list of observed outcomes is
    not silently treated as a probability catalog merely because it sums to
    one after accidental normalization.
    """

    branches: tuple[InitialRegularWeightedEventScenario, ...]
    complete: bool

    def __post_init__(self) -> None:
        branches = tuple(self.branches)
        if not all(
            isinstance(value, InitialRegularWeightedEventScenario)
            for value in branches
        ):
            raise TypeError("event frontier branches must be typed scenarios")
        if not isinstance(self.complete, bool):
            raise TypeError("event frontier complete must be boolean")
        object.__setattr__(self, "branches", branches)


def _issue(code: str, field: str, detail: str = "") -> OuterSearchIssue:
    return OuterSearchIssue(code, field, detail)


def _family_issue(
    request: ExternalRequest,
    branch: InitialRegularWeightedEventScenario,
) -> OuterSearchIssue | None:
    if request.adapter_ref == EVENT_SCENARIO_ADAPTER_REF:
        return None
    detail_id = branch.scenario.detail_id
    expected_fragment = {
        CLASS: "-school-",
        OUTING: "-activity-",
    }.get(request.action_id)
    if expected_fragment is None:
        return _issue(
            "event-frontier-action-family-unsupported",
            "request.action_id",
            request.action_id,
        )
    if expected_fragment not in detail_id:
        return _issue(
            "event-frontier-detail-family-mismatch",
            "frontier.branches.scenario.detail_id",
            f"action={request.action_id}:detail={detail_id}",
        )
    return None


def build_initial_regular_event_scenario_frontier(
    state: ProduceRolloutState,
    request: ExternalRequest,
    frontier: InitialRegularEventScenarioFrontier,
    *,
    attribute_cap: int,
    database: Path = DEFAULT_DATABASE,
) -> InitialRegularOfflineBranchExpansion:
    """Validate and adapt a complete exact event frontier for Expectimax."""

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(frontier, InitialRegularEventScenarioFrontier):
        raise TypeError("frontier must be InitialRegularEventScenarioFrontier")
    if request.kind is not ExternalKind.WEEKLY_ACTION_OUTCOME:
        return InitialRegularOfflineBranchExpansion.paused(
            _issue(
                "event-frontier-request-kind-mismatch",
                "request.kind",
                request.kind.value,
            )
        )
    if not frontier.complete:
        return InitialRegularOfflineBranchExpansion.paused(
            _issue(
                "event-frontier-incomplete",
                "frontier.complete",
                "server event-detail/outcome distribution was not declared complete",
            )
        )
    if not frontier.branches:
        return InitialRegularOfflineBranchExpansion.paused(
            _issue("event-frontier-empty", "frontier.branches")
        )
    branch_ids = tuple(value.branch_id for value in frontier.branches)
    if len(set(branch_ids)) != len(branch_ids):
        return InitialRegularOfflineBranchExpansion.paused(
            _issue("event-frontier-branch-id-duplicate", "frontier.branches")
        )
    total_probability = sum(
        (value.probability for value in frontier.branches), Fraction()
    )
    if total_probability != 1:
        return InitialRegularOfflineBranchExpansion.paused(
            _issue(
                "event-frontier-probability-incomplete",
                "frontier.branches.probability",
                f"actual={total_probability}",
            )
        )

    issues: list[OuterSearchIssue] = []
    loaded: list[
        tuple[InitialRegularWeightedEventScenario, InitialRegularEventEffectCatalog]
    ] = []
    database = Path(database)
    for index, branch in enumerate(frontier.branches):
        family_issue = _family_issue(request, branch)
        if family_issue is not None:
            issues.append(family_issue)
            continue
        try:
            catalog = load_initial_regular_event_effect_catalog(
                branch.scenario.detail_id,
                branch.scenario.suggestion_id,
                actual_success=branch.scenario.success,
                database=database,
            )
        except InitialRegularEventEffectCatalogError as error:
            issues.append(
                _issue(
                    f"event-catalog-{error.code}",
                    f"frontier.branches[{index}].scenario",
                    str(error),
                )
            )
            continue
        except (OSError, TypeError, ValueError) as error:
            issues.append(
                _issue(
                    "event-catalog-load-failed",
                    f"frontier.branches[{index}].scenario",
                    f"{type(error).__name__}:{error}",
                )
            )
            continue
        loaded.append((branch, catalog))
    if issues:
        return InitialRegularOfflineBranchExpansion.paused(*issues)

    grouped: dict[
        tuple[str, str, int],
        list[tuple[InitialRegularWeightedEventScenario, InitialRegularEventEffectCatalog]],
    ] = {}
    for branch, catalog in loaded:
        key = (catalog.detail_id, catalog.suggestion_id, catalog.suggestion_index)
        grouped.setdefault(key, []).append((branch, catalog))
    for key, values in grouped.items():
        group_total = sum((branch.probability for branch, _ in values), Fraction())
        first_catalog = values[0][1]
        conditional_success = (
            Fraction(1)
            if first_catalog.always_successful
            else Fraction(first_catalog.success_probability_permyriad, 10_000)
        )
        actual_success = sum(
            (
                branch.probability
                for branch, catalog in values
                if catalog.actual_success
            ),
            Fraction(),
        )
        expected_success = group_total * conditional_success
        if actual_success != expected_success:
            issues.append(
                _issue(
                    "event-frontier-success-weight-mismatch",
                    "frontier.branches.probability",
                    f"group={key}:expected={expected_success}:actual={actual_success}",
                )
            )
    if issues:
        return InitialRegularOfflineBranchExpansion.paused(*issues)

    resolved: list[InitialRegularWeightedResolvedWeeklyBranch] = []
    for index, (branch, catalog) in enumerate(loaded):
        chance = ChanceBranch(
            branch_id=branch.branch_id,
            probability=float(branch.probability),
            rng_token=branch.rng_token,
            event_id=branch.scenario.detail_id,
        )
        adapted = adapt_initial_regular_event_scenario(
            state,
            request,
            branch.scenario,
            catalog,
            branch=chance,
            attribute_cap=attribute_cap,
        )
        if isinstance(adapted, InitialRegularEventScenarioAdapterPause):
            issues.extend(
                _issue(
                    f"event-scenario-{issue.code}",
                    f"frontier.branches[{index}].{issue.field}",
                    issue.detail,
                )
                for issue in adapted.issues
            )
            continue
        resolved.append(
            InitialRegularWeightedResolvedWeeklyBranch(
                branch.probability,
                adapted.outcome,
            )
        )
    if issues:
        return InitialRegularOfflineBranchExpansion.paused(*issues)
    return InitialRegularOfflineBranchExpansion.resolved(*resolved)


__all__ = [
    "InitialRegularEventScenarioFrontier",
    "InitialRegularWeightedEventScenario",
    "build_initial_regular_event_scenario_frontier",
]
