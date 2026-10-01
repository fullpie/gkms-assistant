"""Pure offline whole-run orchestration for Initial Regular.

This module adapts complete, exact-rational branch catalogs to the generic
outer Expectimax.  Plan-neutral lesson/audition branches dispatch through an
explicit registry; the original Plan3 branch shapes remain as lazy legacy
compatibility paths.  Reward offers and replacements must already be explicit
typed outcomes.  Missing catalogs, adapters, runtime facts, or results become
typed outer pauses.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from math import isclose
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, TypeAlias

from .initial_regular_inner_protocol import (
    InitialRegularInnerStageAdapterRegistry,
    InitialRegularInnerStageKind,
    InitialRegularInnerStageOutcome,
    InitialRegularInnerStageRequest,
    project_initial_regular_inner_stage_outcome,
    validate_initial_regular_inner_branch_probability,
)
from .initial_regular_rollout import INITIAL_REGULAR_PRODUCE_ID
from .initial_regular_weekly_adapter import (
    InitialRegularExplicitEventResult,
    InitialRegularLessonResolutionInput,
    InitialRegularWeeklyEventNode,
    InitialRegularWeeklyLessonNode,
    InitialRegularWeeklyNode,
    InitialRegularWeeklyPause,
    InitialRegularWeeklyReady,
    open_initial_regular_weekly_request,
    resolve_initial_regular_weekly,
)
from .master_db import DEFAULT_DATABASE
from .produce_rollout import (
    ChanceBranch,
    ExternalKind,
    ExternalRequest,
    InnerExamOutcome,
    ProduceRolloutKernel,
    ProduceRolloutState,
    RewardOffersOutcome,
    RewardReplacementOutcome,
    RolloutPhase,
    WeeklyActionOutcome,
)
from .produce_rollout_expectimax import (
    ExternalChanceExpansion,
    OuterExpectimaxResult,
    OuterSearchIssue,
    OuterTerminalEvaluator,
    WeightedExternalOutcome,
    search_produce_rollout_expectimax,
)
from .route_calendar import DEFAULT_MASTER_DIR


if TYPE_CHECKING:
    from .plan3_exam_start import Plan3ExamStartInputs
    from .plan3_rollout_inner_adapter import (
        Plan3InnerBattleResolution,
        Plan3InnerPostExamResolution,
        Plan3InnerRuntimeRefs,
        Plan3InnerSearchCallable,
    )


@dataclass(frozen=True, slots=True)
class InitialRegularWeightedWeeklyBranch:
    probability: Fraction
    lesson_inputs: InitialRegularLessonResolutionInput | None = None
    event_result: InitialRegularExplicitEventResult | None = None

    def __post_init__(self) -> None:
        _validate_probability(self.probability)
        if (self.lesson_inputs is None) == (self.event_result is None):
            raise ValueError(
                "weekly branch must contain lesson_inputs xor event_result"
            )
        if self.lesson_inputs is not None and not isinstance(
            self.lesson_inputs, InitialRegularLessonResolutionInput
        ):
            raise TypeError(
                "lesson_inputs must be InitialRegularLessonResolutionInput"
            )
        if self.event_result is not None and not isinstance(
            self.event_result, InitialRegularExplicitEventResult
        ):
            raise TypeError("event_result must be InitialRegularExplicitEventResult")


@dataclass(frozen=True, slots=True)
class InitialRegularWeightedInnerBranch:
    """One plan-neutral lesson or audition branch dispatched by registry."""

    probability: Fraction
    inner_request: InitialRegularInnerStageRequest

    def __post_init__(self) -> None:
        _validate_probability(self.probability)
        if not isinstance(self.inner_request, InitialRegularInnerStageRequest):
            raise TypeError("inner_request must be InitialRegularInnerStageRequest")

    @property
    def request(self) -> InitialRegularInnerStageRequest:
        return self.inner_request


@dataclass(frozen=True, slots=True)
class InitialRegularWeightedResolvedWeeklyBranch:
    """One already validated weekly outcome, such as an exact event scenario."""

    probability: Fraction
    outcome: WeeklyActionOutcome

    def __post_init__(self) -> None:
        _validate_probability(self.probability)
        if not isinstance(self.outcome, WeeklyActionOutcome):
            raise TypeError("outcome must be WeeklyActionOutcome")


@dataclass(frozen=True, slots=True)
class InitialRegularWeightedAuditionBranch:
    probability: Fraction
    start_inputs: Plan3ExamStartInputs | None
    branch: ChanceBranch | None
    post_exam_resolution: Plan3InnerPostExamResolution | None
    battle_resolution: Plan3InnerBattleResolution | None
    runtime_refs: Plan3InnerRuntimeRefs | None = None

    def __post_init__(self) -> None:
        _validate_probability(self.probability)
        if self.start_inputs is not None:
            from .plan3_exam_start import Plan3ExamStartInputs as _Plan3ExamStartInputs

            if not isinstance(self.start_inputs, _Plan3ExamStartInputs):
                raise TypeError("start_inputs must be Plan3ExamStartInputs or None")
        if self.branch is not None and not isinstance(self.branch, ChanceBranch):
            raise TypeError("branch must be ChanceBranch or None")
        if (
            self.post_exam_resolution is not None
            or self.battle_resolution is not None
            or self.runtime_refs is not None
        ):
            from .plan3_rollout_inner_adapter import (
                Plan3InnerBattleResolution as _Plan3InnerBattleResolution,
                Plan3InnerPostExamResolution as _Plan3InnerPostExamResolution,
                Plan3InnerRuntimeRefs as _Plan3InnerRuntimeRefs,
            )

            if self.post_exam_resolution is not None and not isinstance(
                self.post_exam_resolution, _Plan3InnerPostExamResolution
            ):
                raise TypeError(
                    "post_exam_resolution must be Plan3InnerPostExamResolution or None"
                )
            if self.battle_resolution is not None and not isinstance(
                self.battle_resolution, _Plan3InnerBattleResolution
            ):
                raise TypeError(
                    "battle_resolution must be Plan3InnerBattleResolution or None"
                )
            if self.runtime_refs is not None and not isinstance(
                self.runtime_refs, _Plan3InnerRuntimeRefs
            ):
                raise TypeError("runtime_refs must be Plan3InnerRuntimeRefs or None")


@dataclass(frozen=True, slots=True)
class InitialRegularWeightedRewardBranch:
    probability: Fraction
    outcome: RewardOffersOutcome | RewardReplacementOutcome

    def __post_init__(self) -> None:
        _validate_probability(self.probability)
        if not isinstance(
            self.outcome, (RewardOffersOutcome, RewardReplacementOutcome)
        ):
            raise TypeError("reward outcome must be offers or replacement")


InitialRegularOfflineRawBranch: TypeAlias = (
    InitialRegularWeightedWeeklyBranch
    | InitialRegularWeightedInnerBranch
    | InitialRegularWeightedResolvedWeeklyBranch
    | InitialRegularWeightedAuditionBranch
    | InitialRegularWeightedRewardBranch
)


@dataclass(frozen=True, slots=True)
class InitialRegularOfflineBranchExpansion:
    """One complete raw branch frontier or an explicit catalog pause."""

    branches: tuple[InitialRegularOfflineRawBranch, ...] = ()
    issues: tuple[OuterSearchIssue, ...] = ()

    def __post_init__(self) -> None:
        branches = tuple(self.branches)
        issues = tuple(self.issues)
        object.__setattr__(self, "branches", branches)
        object.__setattr__(self, "issues", issues)
        if bool(branches) == bool(issues):
            raise ValueError("offline expansion must contain branches xor issues")
        if any(
            not isinstance(
                branch,
                (
                    InitialRegularWeightedWeeklyBranch,
                    InitialRegularWeightedInnerBranch,
                    InitialRegularWeightedResolvedWeeklyBranch,
                    InitialRegularWeightedAuditionBranch,
                    InitialRegularWeightedRewardBranch,
                ),
            )
            for branch in branches
        ):
            raise TypeError("offline expansion contains an invalid branch")
        if branches and sum(
            (branch.probability for branch in branches), Fraction()
        ) != Fraction(1):
            raise ValueError("offline branch probabilities must sum exactly to one")
        if issues and any(not isinstance(issue, OuterSearchIssue) for issue in issues):
            raise TypeError("offline expansion issues must be OuterSearchIssue")

    @classmethod
    def resolved(
        cls, *branches: InitialRegularOfflineRawBranch
    ) -> "InitialRegularOfflineBranchExpansion":
        return cls(branches=tuple(branches))

    @classmethod
    def paused(
        cls, *issues: OuterSearchIssue
    ) -> "InitialRegularOfflineBranchExpansion":
        return cls(issues=tuple(issues))


class InitialRegularOfflineBranchProvider(Protocol):
    """Enumerate every branch for one exact pending external request."""

    def __call__(
        self,
        state: ProduceRolloutState,
        request: ExternalRequest,
        weekly_node: InitialRegularWeeklyNode | None,
    ) -> InitialRegularOfflineBranchExpansion: ...


InitialRegularAdapterCacheKey: TypeAlias = Callable[
    [ProduceRolloutState, ExternalRequest, InitialRegularOfflineRawBranch], object
]
InitialRegularFrontierCacheKey: TypeAlias = Callable[
    [ProduceRolloutState, ExternalRequest], object
]


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularOfflineBoundary:
    week: int
    kind: ExternalKind
    action_id: str
    stage_type: str | None
    history_length: int


def history_independent_adapter_cache_key(
    state: ProduceRolloutState,
    request: ExternalRequest,
    branch: InitialRegularOfflineRawBranch,
) -> object:
    """Opt-in key for callbacks certified independent of chance history.

    All state that adapters consume is retained.  Only the accumulated
    provenance history and pending request identity are omitted.  Callers
    must not use this key when their injected search depends on those fields.
    """

    return (
        state.mode_id,
        state.character_id,
        state.week,
        state.total_weeks,
        state.stamina,
        state.max_stamina,
        state.attributes,
        state.deck,
        state.produce_points,
        state.item_session_refs,
        state.drink_session_refs,
        state.passive_session_refs,
        state.excluded_reward_card_ids,
        request.kind,
        request.action_id,
        request.stage_type,
        repr(branch),
    )


def history_independent_frontier_cache_key(
    state: ProduceRolloutState,
    request: ExternalRequest,
) -> object:
    """Opt-in frontier key for a history-independent branch provider/search."""

    return (
        state.mode_id,
        state.character_id,
        state.week,
        state.total_weeks,
        state.stamina,
        state.max_stamina,
        state.attributes,
        state.deck,
        state.produce_points,
        state.item_session_refs,
        state.drink_session_refs,
        state.passive_session_refs,
        state.excluded_reward_card_ids,
        state.reward_context,
        state.reward_offers,
        state.remaining_reward_excludes,
        request.kind,
        request.action_id,
        request.stage_type,
    )


class InitialRegularOfflineChanceAdapter:
    """Translate raw complete catalogs into Expectimax external outcomes."""

    def __init__(
        self,
        branch_provider: InitialRegularOfflineBranchProvider,
        *,
        search: Plan3InnerSearchCallable | None = None,
        inner_registry: InitialRegularInnerStageAdapterRegistry | None = None,
        beam_width: int = 64,
        adapter_cache_key: InitialRegularAdapterCacheKey | None = None,
        frontier_cache_key: InitialRegularFrontierCacheKey | None = None,
        database: Path = DEFAULT_DATABASE,
        master_dir: Path = DEFAULT_MASTER_DIR,
    ) -> None:
        if not callable(branch_provider):
            raise TypeError("branch_provider must be callable")
        if search is not None and not callable(search):
            raise TypeError("search must be callable or None")
        if inner_registry is not None and not isinstance(
            inner_registry, InitialRegularInnerStageAdapterRegistry
        ):
            raise TypeError(
                "inner_registry must be InitialRegularInnerStageAdapterRegistry or None"
            )
        if (
            not isinstance(beam_width, int)
            or isinstance(beam_width, bool)
            or beam_width < 1
        ):
            raise ValueError("beam_width must be a positive integer")
        if adapter_cache_key is not None and not callable(adapter_cache_key):
            raise TypeError("adapter_cache_key must be callable or None")
        if frontier_cache_key is not None and not callable(frontier_cache_key):
            raise TypeError("frontier_cache_key must be callable or None")
        self.branch_provider = branch_provider
        self.search = search
        self.inner_registry = inner_registry
        self.beam_width = beam_width
        self.adapter_cache_key = adapter_cache_key
        self.frontier_cache_key = frontier_cache_key
        self.database = Path(database)
        self.master_dir = Path(master_dir)
        self._adapter_cache: dict[object, WeeklyActionOutcome | InnerExamOutcome] = {}
        self._frontier_cache: dict[object, ExternalChanceExpansion] = {}
        self._boundaries: set[InitialRegularOfflineBoundary] = set()
        self._adapter_calls = 0
        self._adapter_cache_hits = 0
        self._frontier_cache_hits = 0

    @property
    def visited_boundaries(self) -> tuple[InitialRegularOfflineBoundary, ...]:
        return tuple(sorted(self._boundaries))

    @property
    def adapter_calls(self) -> int:
        return self._adapter_calls

    @property
    def adapter_cache_hits(self) -> int:
        return self._adapter_cache_hits

    @property
    def frontier_cache_hits(self) -> int:
        return self._frontier_cache_hits

    def __call__(
        self, state: ProduceRolloutState, request: ExternalRequest
    ) -> ExternalChanceExpansion:
        if not isinstance(state, ProduceRolloutState):
            raise TypeError("state must be ProduceRolloutState")
        if not isinstance(request, ExternalRequest):
            raise TypeError("request must be ExternalRequest")
        self._boundaries.add(
            InitialRegularOfflineBoundary(
                week=request.week,
                kind=request.kind,
                action_id=request.action_id,
                stage_type=request.stage_type,
                history_length=len(state.chance_history),
            )
        )
        if state.phase != RolloutPhase.WAITING_EXTERNAL or state.pending != request:
            return ExternalChanceExpansion.paused(
                OuterSearchIssue("outer-request-not-pending", "state.pending")
            )
        frontier_key_or_issue = self._frontier_key(state, request)
        if isinstance(frontier_key_or_issue, OuterSearchIssue):
            return ExternalChanceExpansion.paused(frontier_key_or_issue)
        if frontier_key_or_issue is not None:
            cached_frontier = self._frontier_cache.get(frontier_key_or_issue)
            if cached_frontier is not None:
                self._frontier_cache_hits += 1
                return _rebind_cached_frontier(cached_frontier, request)
        weekly_node: InitialRegularWeeklyNode | None = None
        if request.kind == ExternalKind.WEEKLY_ACTION_OUTCOME:
            opened = open_initial_regular_weekly_request(
                state, request, master_dir=self.master_dir
            )
            if isinstance(opened, InitialRegularWeeklyPause):
                return _external_pause("weekly", opened.issues)
            weekly_node = opened
        elif request.kind not in {
            ExternalKind.INNER_EXAM_OUTCOME,
            ExternalKind.REWARD_OFFERS,
            ExternalKind.REWARD_REPLACEMENT,
        }:
            return ExternalChanceExpansion.paused(
                OuterSearchIssue(
                    "offline-request-kind-unsupported",
                    "request.kind",
                    request.kind.value,
                )
            )
        try:
            expansion = self.branch_provider(state, request, weekly_node)
        except Exception as error:
            return ExternalChanceExpansion.paused(
                OuterSearchIssue(
                    "offline-branch-provider-failed",
                    "branch_provider",
                    f"{type(error).__name__}:{error}",
                )
            )
        if not isinstance(expansion, InitialRegularOfflineBranchExpansion):
            return ExternalChanceExpansion.paused(
                OuterSearchIssue(
                    "offline-branch-expansion-invalid", "branch_provider"
                )
            )
        if expansion.issues:
            return ExternalChanceExpansion(issues=expansion.issues)
        expected_types: tuple[type[object], ...]
        if request.kind == ExternalKind.WEEKLY_ACTION_OUTCOME:
            assert weekly_node is not None
            expected_types = (
                (
                    InitialRegularWeightedWeeklyBranch,
                    InitialRegularWeightedInnerBranch,
                    InitialRegularWeightedResolvedWeeklyBranch,
                )
                if isinstance(weekly_node, InitialRegularWeeklyLessonNode)
                else (
                    InitialRegularWeightedWeeklyBranch,
                    InitialRegularWeightedResolvedWeeklyBranch,
                )
            )
        elif request.kind == ExternalKind.INNER_EXAM_OUTCOME:
            expected_types = (
                InitialRegularWeightedAuditionBranch,
                InitialRegularWeightedInnerBranch,
            )
        else:
            expected_types = (InitialRegularWeightedRewardBranch,)
        if any(
            not isinstance(branch, expected_types) for branch in expansion.branches
        ):
            return ExternalChanceExpansion.paused(
                OuterSearchIssue(
                    "offline-branch-kind-mismatch",
                    "branch_provider",
                    request.kind.value,
                )
            )
        resolved: list[WeightedExternalOutcome] = []
        for raw_branch in expansion.branches:
            outcome_or_issue = self._resolve_branch(
                state, request, weekly_node, raw_branch
            )
            if isinstance(outcome_or_issue, OuterSearchIssue):
                return ExternalChanceExpansion.paused(outcome_or_issue)
            resolved.append(
                WeightedExternalOutcome(raw_branch.probability, outcome_or_issue)
            )
        completed = ExternalChanceExpansion.resolved(*resolved)
        if frontier_key_or_issue is not None:
            self._frontier_cache[frontier_key_or_issue] = completed
        return completed

    def _resolve_branch(
        self,
        state: ProduceRolloutState,
        request: ExternalRequest,
        weekly_node: InitialRegularWeeklyNode | None,
        raw_branch: InitialRegularOfflineRawBranch,
    ) -> WeeklyActionOutcome | InnerExamOutcome | RewardOffersOutcome | RewardReplacementOutcome | OuterSearchIssue:
        if isinstance(raw_branch, InitialRegularWeightedResolvedWeeklyBranch):
            if request.kind != ExternalKind.WEEKLY_ACTION_OUTCOME:
                return OuterSearchIssue(
                    "resolved-weekly-request-kind-mismatch",
                    "request.kind",
                    request.kind.value,
                )
            outcome = raw_branch.outcome
            if outcome.request_id != request.request_id:
                return OuterSearchIssue(
                    "resolved-weekly-request-id-mismatch",
                    "branch_provider.outcome.request_id",
                )
            if outcome.branch.probability is not None and not isclose(
                outcome.branch.probability,
                float(raw_branch.probability),
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                return OuterSearchIssue(
                    "resolved-weekly-probability-mismatch",
                    "branch_provider.outcome.branch.probability",
                )
            return outcome

        if isinstance(raw_branch, InitialRegularWeightedRewardBranch):
            outcome = raw_branch.outcome
            expected = (
                RewardOffersOutcome
                if request.kind == ExternalKind.REWARD_OFFERS
                else RewardReplacementOutcome
            )
            if not isinstance(outcome, expected):
                return OuterSearchIssue(
                    "reward-outcome-kind-mismatch",
                    "branch_provider.outcome",
                    request.kind.value,
                )
            if outcome.request_id != request.request_id:
                return OuterSearchIssue(
                    "reward-request-id-mismatch", "branch_provider.outcome.request_id"
                )
            return outcome

        if isinstance(raw_branch, InitialRegularWeightedInnerBranch):
            inner_request = raw_branch.inner_request
            if inner_request.outer_state != state:
                return OuterSearchIssue(
                    "inner-outer-state-mismatch",
                    "branch_provider.inner_request.outer_state",
                )
            expected_kind = (
                InitialRegularInnerStageKind.LESSON
                if request.kind == ExternalKind.WEEKLY_ACTION_OUTCOME
                else InitialRegularInnerStageKind.AUDITION
            )
            if inner_request.stage_kind is not expected_kind:
                return OuterSearchIssue(
                    "inner-stage-kind-mismatch",
                    "branch_provider.inner_request.stage_kind",
                    f"expected={expected_kind.value}:actual={inner_request.stage_kind.value}",
                )
            if (
                expected_kind is InitialRegularInnerStageKind.LESSON
                and not isinstance(weekly_node, InitialRegularWeeklyLessonNode)
            ):
                return OuterSearchIssue(
                    "inner-lesson-node-mismatch",
                    "branch_provider.inner_request.stage_kind",
                )
            if self.inner_registry is None:
                return OuterSearchIssue(
                    "inner-adapter-registry-unresolved", "inner_registry"
                )
            self._adapter_calls += 1
            common = self.inner_registry.resolve(inner_request)
            if isinstance(common, tuple):
                return _first_adapter_issue("inner", common)
            assert isinstance(common, InitialRegularInnerStageOutcome)
            probability_issues = validate_initial_regular_inner_branch_probability(
                raw_branch.probability, common
            )
            if probability_issues:
                return _first_adapter_issue("inner", probability_issues)
            return project_initial_regular_inner_stage_outcome(
                inner_request, common
            )

        cache_key_or_issue = self._cache_key(state, request, raw_branch)
        if isinstance(cache_key_or_issue, OuterSearchIssue):
            return cache_key_or_issue
        if cache_key_or_issue is not None:
            cached = self._adapter_cache.get(cache_key_or_issue)
            if cached is not None:
                self._adapter_cache_hits += 1
                return _rebind_cached_outcome(cached, request, raw_branch)

        if isinstance(raw_branch, InitialRegularWeightedWeeklyBranch):
            assert weekly_node is not None
            if isinstance(weekly_node, InitialRegularWeeklyLessonNode):
                if raw_branch.lesson_inputs is None:
                    return OuterSearchIssue(
                        "weekly-lesson-branch-unresolved",
                        "branch_provider.lesson_inputs",
                    )
                if self.search is None:
                    return OuterSearchIssue(
                        "inner-search-unresolved", "search", "weekly lesson"
                    )
                weekly = resolve_initial_regular_weekly(
                    state,
                    request,
                    lesson_inputs=raw_branch.lesson_inputs,
                    search=self.search,
                    beam_width=self.beam_width,
                    database=self.database,
                    master_dir=self.master_dir,
                )
            else:
                assert isinstance(weekly_node, InitialRegularWeeklyEventNode)
                if raw_branch.event_result is None:
                    return OuterSearchIssue(
                        "weekly-event-branch-unresolved",
                        "branch_provider.event_result",
                    )
                weekly = resolve_initial_regular_weekly(
                    state,
                    request,
                    event_result=raw_branch.event_result,
                    database=self.database,
                    master_dir=self.master_dir,
                )
            if isinstance(weekly, InitialRegularWeeklyPause):
                return _first_adapter_issue("weekly", weekly.issues)
            assert isinstance(weekly, InitialRegularWeeklyReady)
            outcome: WeeklyActionOutcome | InnerExamOutcome = weekly.outcome
        else:
            assert isinstance(raw_branch, InitialRegularWeightedAuditionBranch)
            missing = _audition_missing_issue(raw_branch, self.search)
            if missing is not None:
                return missing
            assert raw_branch.start_inputs is not None
            assert raw_branch.branch is not None
            assert raw_branch.post_exam_resolution is not None
            assert raw_branch.battle_resolution is not None
            assert self.search is not None
            if not raw_branch.start_inputs.context.is_battle:
                return OuterSearchIssue(
                    "audition-context-not-battle",
                    "branch_provider.start_inputs.context.is_battle",
                )
            self._adapter_calls += 1
            # This is the explicit legacy compatibility branch.  Common
            # registry branches above never import or call Plan3.
            from .plan3_rollout_inner_adapter import (
                Plan3InnerRolloutPause as _Plan3InnerRolloutPause,
                Plan3InnerRolloutReady as _Plan3InnerRolloutReady,
                adapt_plan3_rollout_inner,
            )

            inner = adapt_plan3_rollout_inner(
                state,
                request,
                raw_branch.start_inputs,
                branch=raw_branch.branch,
                runtime_refs=raw_branch.runtime_refs,
                search=self.search,
                beam_width=self.beam_width,
                battle_resolver=lambda _context: raw_branch.battle_resolution,
                post_exam_resolution=raw_branch.post_exam_resolution,
                database=self.database,
                master_dir=self.master_dir,
            )
            if isinstance(inner, _Plan3InnerRolloutPause):
                return _first_adapter_issue("audition", inner.issues)
            assert isinstance(inner, _Plan3InnerRolloutReady)
            outcome = inner.outcome
        if isinstance(raw_branch, InitialRegularWeightedWeeklyBranch):
            self._adapter_calls += 1
        if cache_key_or_issue is not None:
            self._adapter_cache[cache_key_or_issue] = outcome
        return outcome

    def _cache_key(
        self,
        state: ProduceRolloutState,
        request: ExternalRequest,
        branch: InitialRegularOfflineRawBranch,
    ) -> object | None | OuterSearchIssue:
        if self.adapter_cache_key is None:
            return None
        try:
            key = self.adapter_cache_key(state, request, branch)
            hash(key)
        except Exception as error:
            return OuterSearchIssue(
                "adapter-cache-key-invalid",
                "adapter_cache_key",
                f"{type(error).__name__}:{error}",
            )
        return (request.kind, key)

    def _frontier_key(
        self,
        state: ProduceRolloutState,
        request: ExternalRequest,
    ) -> object | None | OuterSearchIssue:
        if self.frontier_cache_key is None:
            return None
        try:
            key = self.frontier_cache_key(state, request)
            hash(key)
        except Exception as error:
            return OuterSearchIssue(
                "frontier-cache-key-invalid",
                "frontier_cache_key",
                f"{type(error).__name__}:{error}",
            )
        return (request.kind, key)


def search_initial_regular_offline(
    kernel: ProduceRolloutKernel,
    state: ProduceRolloutState,
    *,
    branch_provider: InitialRegularOfflineBranchProvider,
    evaluator: OuterTerminalEvaluator,
    search: Plan3InnerSearchCallable | None = None,
    inner_registry: InitialRegularInnerStageAdapterRegistry | None = None,
    beam_width: int = 64,
    max_transitions: int = 128,
    adapter_cache_key: InitialRegularAdapterCacheKey | None = None,
    frontier_cache_key: InitialRegularFrontierCacheKey | None = None,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> OuterExpectimaxResult:
    """Run exact outer Expectimax with the Initial Regular adapters."""

    if not isinstance(kernel, ProduceRolloutKernel):
        raise TypeError("kernel must be ProduceRolloutKernel")
    if state.mode_id != INITIAL_REGULAR_PRODUCE_ID:
        raise ValueError("state is not Initial Regular")
    if kernel.calendar.produce_id != INITIAL_REGULAR_PRODUCE_ID:
        raise ValueError("kernel is not Initial Regular")
    chance_adapter = InitialRegularOfflineChanceAdapter(
        branch_provider,
        search=search,
        inner_registry=inner_registry,
        beam_width=beam_width,
        adapter_cache_key=adapter_cache_key,
        frontier_cache_key=frontier_cache_key,
        database=database,
        master_dir=master_dir,
    )
    return search_produce_rollout_expectimax(
        kernel,
        state,
        chance_provider=chance_adapter,
        evaluator=evaluator,
        max_transitions=max_transitions,
    )


def _validate_probability(probability: Fraction) -> None:
    if isinstance(probability, bool) or not isinstance(probability, Fraction):
        raise TypeError("branch probability must be fractions.Fraction")
    if probability <= 0 or probability > 1:
        raise ValueError("branch probability must be in (0, 1]")


def _external_pause(prefix: str, issues: tuple[object, ...]) -> ExternalChanceExpansion:
    return ExternalChanceExpansion.paused(
        *(
            OuterSearchIssue(
                f"{prefix}:{getattr(issue, 'code', 'unknown')}",
                str(getattr(issue, "field", prefix)),
                str(getattr(issue, "detail", "")),
            )
            for issue in issues
        )
    )


def _first_adapter_issue(prefix: str, issues: tuple[object, ...]) -> OuterSearchIssue:
    issue = issues[0]
    return OuterSearchIssue(
        f"{prefix}:{getattr(issue, 'code', 'unknown')}",
        str(getattr(issue, "field", prefix)),
        str(getattr(issue, "detail", "")),
    )


def _audition_missing_issue(
    branch: InitialRegularWeightedAuditionBranch,
    search: Plan3InnerSearchCallable | None,
) -> OuterSearchIssue | None:
    for value, code, field in (
        (branch.start_inputs, "audition-start-inputs-unresolved", "start_inputs"),
        (branch.branch, "audition-chance-unresolved", "branch"),
        (
            branch.post_exam_resolution,
            "audition-post-patch-unresolved",
            "post_exam_resolution",
        ),
        (
            branch.battle_resolution,
            "audition-rank-unresolved",
            "battle_resolution",
        ),
        (search, "inner-search-unresolved", "search"),
    ):
        if value is None:
            return OuterSearchIssue(code, f"branch_provider.{field}")
    return None


def _raw_chance_branch(
    branch: InitialRegularOfflineRawBranch,
) -> ChanceBranch:
    if isinstance(branch, InitialRegularWeightedInnerBranch):
        raise ValueError(
            "common inner branch chance is produced by its registered adapter"
        )
    if isinstance(branch, InitialRegularWeightedWeeklyBranch):
        if branch.lesson_inputs is not None:
            assert branch.lesson_inputs.branch is not None
            return branch.lesson_inputs.branch
        assert branch.event_result is not None
        return branch.event_result.branch
    if isinstance(branch, InitialRegularWeightedResolvedWeeklyBranch):
        return branch.outcome.branch
    if isinstance(branch, InitialRegularWeightedAuditionBranch):
        assert branch.branch is not None
        return branch.branch
    return branch.outcome.branch


def _rebind_cached_outcome(
    outcome: WeeklyActionOutcome | InnerExamOutcome,
    request: ExternalRequest,
    branch: InitialRegularOfflineRawBranch,
) -> WeeklyActionOutcome | InnerExamOutcome:
    return replace(
        outcome,
        request_id=request.request_id,
        branch=_raw_chance_branch(branch),
    )


def _rebind_cached_frontier(
    expansion: ExternalChanceExpansion,
    request: ExternalRequest,
) -> ExternalChanceExpansion:
    return ExternalChanceExpansion.resolved(
        *(
            WeightedExternalOutcome(
                branch.probability,
                replace(branch.outcome, request_id=request.request_id),
            )
            for branch in expansion.branches
        )
    )


__all__ = [
    "InitialRegularAdapterCacheKey",
    "InitialRegularFrontierCacheKey",
    "InitialRegularOfflineBoundary",
    "InitialRegularOfflineBranchExpansion",
    "InitialRegularOfflineBranchProvider",
    "InitialRegularOfflineChanceAdapter",
    "InitialRegularOfflineRawBranch",
    "InitialRegularWeightedAuditionBranch",
    "InitialRegularWeightedInnerBranch",
    "InitialRegularWeightedRewardBranch",
    "InitialRegularWeightedResolvedWeeklyBranch",
    "InitialRegularWeightedWeeklyBranch",
    "history_independent_adapter_cache_key",
    "history_independent_frontier_cache_key",
    "search_initial_regular_offline",
]
