"""Pure outer-rollout to Plan 3 full-horizon adapter.

The adapter owns no OCR, LocalSave, game, or server reads.  It accepts an
already pending outer ``INNER_EXAM_OUTCOME`` request, explicit exam-start
chance inputs, and an injectable full-horizon search callable.  Unknown
bootstrap, search, rank, or post-exam facts remain typed pauses; the adapter
never manufactures an ``InnerExamOutcome`` from a partial result.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol, TypeAlias

from .audition_rules import FINAL
from .master_db import DEFAULT_DATABASE
from .plan3_engine import DEFAULT_MASTER_DIR, Plan3ExamSettings, Plan3State
from .plan3_exam_start import (
    Plan3ExamStartInputs,
    Plan3ExamStartPause,
    Plan3ExamStartReady,
    Plan3ExamStartResult,
    bootstrap_plan3_exam_start,
)
from .plan3_native_state import Plan3NativeState
from .produce_rollout import (
    ChanceBranch,
    ExternalKind,
    ExternalRequest,
    InnerExamOutcome,
    ProduceRolloutState,
    ProduceStatePatch,
    RolloutPhase,
)


class Plan3InnerRolloutStatus(StrEnum):
    READY = "ready"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True, order=True)
class Plan3InnerRolloutIssue:
    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.code or not self.field:
            raise ValueError("issue code and field must be non-empty")


@dataclass(frozen=True, slots=True)
class Plan3InnerChanceSelection:
    """One explicit outer branch bound to complete exam-start inputs."""

    start_inputs: Plan3ExamStartInputs
    branch: ChanceBranch

    def __post_init__(self) -> None:
        if not isinstance(self.start_inputs, Plan3ExamStartInputs):
            raise TypeError("start_inputs must be Plan3ExamStartInputs")
        if not isinstance(self.branch, ChanceBranch):
            raise TypeError("branch must be ChanceBranch")


class Plan3InnerChanceProvider(Protocol):
    def __call__(
        self, state: ProduceRolloutState, request: ExternalRequest
    ) -> Plan3InnerChanceSelection: ...


@dataclass(frozen=True, slots=True)
class Plan3InnerRuntimeRefs:
    """Immutable references passed through to the selected search adapter."""

    setting_id: str = ""
    gimmick_ref: str = ""
    item_refs: tuple[str, ...] = ()
    support_refs: tuple[str, ...] = ()
    drink_refs: tuple[str, ...] = ()
    passive_refs: tuple[str, ...] = ()
    battle_parameter_schedule: tuple[int, ...] | None = None
    force_end_score: int = 0
    force_end_stamina_recovery: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.setting_id, str) or not isinstance(
            self.gimmick_ref, str
        ):
            raise ValueError("setting_id and gimmick_ref must be text")
        for name in (
            "item_refs",
            "support_refs",
            "drink_refs",
            "passive_refs",
        ):
            values = tuple(getattr(self, name))
            if any(
                not isinstance(value, str) or not value.strip()
                for value in values
            ):
                raise ValueError(f"{name} must contain non-empty text")
            if name != "drink_refs" and len(set(values)) != len(values):
                raise ValueError(f"{name} must contain unique references")
            object.__setattr__(self, name, values)
        if self.battle_parameter_schedule is not None:
            schedule = tuple(self.battle_parameter_schedule)
            if any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value not in range(4)
                for value in schedule
            ):
                raise ValueError(
                    "battle_parameter_schedule must contain native parameter types"
                )
            object.__setattr__(self, "battle_parameter_schedule", schedule)
        for name in ("force_end_score", "force_end_stamina_recovery"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.force_end_stamina_recovery and not self.force_end_score:
            raise ValueError(
                "force_end_stamina_recovery requires force_end_score"
            )


@dataclass(frozen=True, slots=True)
class Plan3InnerStageContract:
    """Outer lifecycle contract; it does not infer terminality from score."""

    terminal_stage_types: tuple[str, ...] = (FINAL,)

    def __post_init__(self) -> None:
        values = tuple(self.terminal_stage_types)
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("terminal_stage_types must contain non-empty text")
        if len(set(values)) != len(values):
            raise ValueError("terminal_stage_types must be unique")
        object.__setattr__(self, "terminal_stage_types", values)

    def is_outer_terminal(self, request: ExternalRequest) -> bool:
        return request.stage_type in self.terminal_stage_types


@dataclass(frozen=True, slots=True)
class Plan3InnerSearchRequest:
    outer_state: ProduceRolloutState
    external_request: ExternalRequest
    bootstrap: Plan3ExamStartReady
    runtime_refs: Plan3InnerRuntimeRefs
    beam_width: int = 64
    depth: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.beam_width, int)
            or isinstance(self.beam_width, bool)
            or self.beam_width < 1
        ):
            raise ValueError("beam_width must be a positive integer")

    @property
    def state(self) -> Plan3State:
        return self.bootstrap.state

    @property
    def native_state(self) -> Plan3NativeState:
        return self.bootstrap.native_state

    @property
    def settings(self) -> Plan3ExamSettings:
        return self.bootstrap.settings


Plan3InnerSearchCallable: TypeAlias = Callable[[Plan3InnerSearchRequest], object]


@dataclass(frozen=True, slots=True)
class Plan3InnerBattleResolution:
    """Explicit audition rank-boundary result for the searched final state."""

    rank: int
    cleared: bool

    def __post_init__(self) -> None:
        if not isinstance(self.rank, int) or isinstance(self.rank, bool) or self.rank < 0:
            raise ValueError("rank must be a non-negative integer")
        if not isinstance(self.cleared, bool):
            raise ValueError("cleared must be a boolean")


@dataclass(frozen=True, slots=True)
class Plan3InnerResolutionContext:
    outer_state: ProduceRolloutState
    external_request: ExternalRequest
    bootstrap: Plan3ExamStartReady
    search_result: object
    final_state: Plan3State
    final_native_state: Plan3NativeState


Plan3InnerBattleResolver: TypeAlias = Callable[
    [Plan3InnerResolutionContext], Plan3InnerBattleResolution
]


@dataclass(frozen=True, slots=True)
class Plan3InnerPostExamResolution:
    """Explicit non-score-derived outer facts after the inner exam."""

    patch: ProduceStatePatch = field(default_factory=ProduceStatePatch)
    reward_requested: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.patch, ProduceStatePatch):
            raise TypeError("patch must be ProduceStatePatch")
        if not isinstance(self.reward_requested, bool):
            raise TypeError("reward_requested must be a boolean")


Plan3InnerOutcomeResolver: TypeAlias = Callable[
    [Plan3InnerResolutionContext], Plan3InnerPostExamResolution
]


@dataclass(frozen=True, slots=True)
class Plan3InnerRolloutReady:
    outer_state: ProduceRolloutState
    request: ExternalRequest
    chance: Plan3InnerChanceSelection
    bootstrap: Plan3ExamStartReady
    search_request: Plan3InnerSearchRequest
    search_result: object
    battle_resolution: Plan3InnerBattleResolution | None
    post_exam_resolution: Plan3InnerPostExamResolution
    outcome: InnerExamOutcome
    status: Plan3InnerRolloutStatus = field(
        default=Plan3InnerRolloutStatus.READY, init=False
    )
    issues: tuple[Plan3InnerRolloutIssue, ...] = field(default=(), init=False)

    @property
    def available(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Plan3InnerRolloutPause:
    outer_state: ProduceRolloutState
    request: ExternalRequest
    issues: tuple[Plan3InnerRolloutIssue, ...]
    chance: Plan3InnerChanceSelection | None = None
    bootstrap: Plan3ExamStartResult | None = None
    search_request: Plan3InnerSearchRequest | None = None
    search_result: object | None = None
    status: Plan3InnerRolloutStatus = field(
        default=Plan3InnerRolloutStatus.PAUSED, init=False
    )
    outcome: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not self.issues:
            raise ValueError("paused result must contain at least one issue")

    @property
    def available(self) -> bool:
        return False


Plan3InnerRolloutResult = Plan3InnerRolloutReady | Plan3InnerRolloutPause


def _issue(code: str, field: str, detail: str = "") -> Plan3InnerRolloutIssue:
    return Plan3InnerRolloutIssue(code, field, detail)


def _pause(
    state: ProduceRolloutState,
    request: ExternalRequest,
    *issues: Plan3InnerRolloutIssue,
    chance: Plan3InnerChanceSelection | None = None,
    bootstrap: Plan3ExamStartResult | None = None,
    search_request: Plan3InnerSearchRequest | None = None,
    search_result: object | None = None,
) -> Plan3InnerRolloutPause:
    return Plan3InnerRolloutPause(
        outer_state=state,
        request=request,
        issues=tuple(issues),
        chance=chance,
        bootstrap=bootstrap,
        search_request=search_request,
        search_result=search_result,
    )


def _default_runtime_refs(
    state: ProduceRolloutState, inputs: Plan3ExamStartInputs
) -> Plan3InnerRuntimeRefs:
    return Plan3InnerRuntimeRefs(
        setting_id=inputs.context.setting_id,
        gimmick_ref=inputs.context.gimmick_group_id,
        item_refs=state.item_session_refs,
        support_refs=inputs.loadout.support_card_ids,
        drink_refs=state.drink_session_refs,
        passive_refs=state.passive_session_refs,
    )


def _default_search(request: Plan3InnerSearchRequest) -> object:
    """Lazy bridge so importing this adapter does not import search/add-grow."""

    from .plan3_native_search import search_plan3_native

    return search_plan3_native(
        request.state,
        request.native_state,
        beam_width=request.beam_width,
        depth=None,
        settings=request.settings,
        battle_parameter_schedule=request.runtime_refs.battle_parameter_schedule,
        battle_ranking_resolved=False,
        force_end_score=request.runtime_refs.force_end_score,
        force_end_stamina_recovery=(
            request.runtime_refs.force_end_stamina_recovery
        ),
    )


def _resolve_chance(
    state: ProduceRolloutState,
    request: ExternalRequest,
    start_inputs: Plan3ExamStartInputs | None,
    branch: ChanceBranch | None,
    provider: Plan3InnerChanceProvider | None,
) -> tuple[Plan3InnerChanceSelection | None, Plan3InnerRolloutIssue | None]:
    if provider is not None:
        if start_inputs is not None or branch is not None:
            return None, _issue(
                "chance-input-conflict",
                "chance_provider",
                "provider is mutually exclusive with direct start_inputs/branch",
            )
        try:
            selected = provider(state, request)
        except Exception as error:
            return None, _issue(
                "chance-provider-failed", "chance_provider", str(error)
            )
        if not isinstance(selected, Plan3InnerChanceSelection):
            return None, _issue(
                "chance-provider-result-invalid", "chance_provider"
            )
        return selected, None
    if start_inputs is None or branch is None:
        return None, _issue(
            "chance-input-unresolved",
            "start_inputs/branch",
            "both direct inputs are required without a provider",
        )
    if not isinstance(start_inputs, Plan3ExamStartInputs) or not isinstance(
        branch, ChanceBranch
    ):
        return None, _issue("chance-input-invalid", "start_inputs/branch")
    return Plan3InnerChanceSelection(start_inputs, branch), None


def _validate_outer_binding(
    state: ProduceRolloutState,
    request: ExternalRequest,
    chance: Plan3InnerChanceSelection,
    runtime_refs: Plan3InnerRuntimeRefs,
) -> tuple[Plan3InnerRolloutIssue, ...]:
    inputs = chance.start_inputs
    issues: list[Plan3InnerRolloutIssue] = []
    if inputs.deck != state.deck:
        issues.append(_issue("outer-deck-mismatch", "start_inputs.deck"))
    if inputs.attributes != state.attributes:
        issues.append(
            _issue("outer-attributes-mismatch", "start_inputs.attributes")
        )
    if inputs.stamina != state.stamina or inputs.max_stamina != state.max_stamina:
        issues.append(_issue("outer-stamina-mismatch", "start_inputs.stamina"))
    if inputs.loadout.item_ids != state.item_session_refs:
        issues.append(_issue("outer-item-refs-mismatch", "start_inputs.loadout"))
    if inputs.loadout.drink_ids != state.drink_session_refs:
        issues.append(_issue("outer-drink-refs-mismatch", "start_inputs.loadout"))
    if inputs.loadout.passive_ids != state.passive_session_refs:
        issues.append(
            _issue("outer-passive-refs-mismatch", "start_inputs.loadout")
        )
    if runtime_refs.setting_id != inputs.context.setting_id:
        issues.append(_issue("setting-ref-mismatch", "runtime_refs.setting_id"))
    if runtime_refs.gimmick_ref != inputs.context.gimmick_group_id:
        issues.append(_issue("gimmick-ref-mismatch", "runtime_refs.gimmick_ref"))
    if runtime_refs.item_refs != state.item_session_refs:
        issues.append(_issue("runtime-item-refs-mismatch", "runtime_refs.item_refs"))
    if runtime_refs.support_refs != inputs.loadout.support_card_ids:
        issues.append(
            _issue("runtime-support-refs-mismatch", "runtime_refs.support_refs")
        )
    if runtime_refs.drink_refs != state.drink_session_refs:
        issues.append(
            _issue("runtime-drink-refs-mismatch", "runtime_refs.drink_refs")
        )
    if runtime_refs.passive_refs != state.passive_session_refs:
        issues.append(
            _issue("runtime-passive-refs-mismatch", "runtime_refs.passive_refs")
        )
    if chance.branch.rng_token is None:
        issues.append(_issue("branch-rng-token-unresolved", "branch.rng_token"))
    if request.stage_type is None:
        issues.append(_issue("request-stage-type-unresolved", "request.stage_type"))
    return tuple(issues)


def _merge_stamina_patch(
    resolution: Plan3InnerPostExamResolution,
    final_state: Plan3State,
) -> tuple[ProduceStatePatch | None, Plan3InnerRolloutIssue | None]:
    patch = resolution.patch
    if patch.stamina is not None and patch.stamina != final_state.stamina:
        return None, _issue(
            "post-exam-stamina-mismatch",
            "post_exam_resolution.patch.stamina",
            f"search={final_state.stamina}:resolver={patch.stamina}",
        )
    return replace(patch, stamina=final_state.stamina), None


def adapt_plan3_rollout_inner(
    state: ProduceRolloutState,
    request: ExternalRequest,
    start_inputs: Plan3ExamStartInputs | None = None,
    *,
    branch: ChanceBranch | None = None,
    chance_provider: Plan3InnerChanceProvider | None = None,
    runtime_refs: Plan3InnerRuntimeRefs | None = None,
    search: Plan3InnerSearchCallable | None = None,
    beam_width: int = 64,
    battle_resolver: Plan3InnerBattleResolver | None = None,
    post_exam_resolution: Plan3InnerPostExamResolution | None = None,
    outcome_resolver: Plan3InnerOutcomeResolver | None = None,
    stage_contract: Plan3InnerStageContract = Plan3InnerStageContract(),
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan3InnerRolloutResult:
    """Resolve one pending inner-exam request without guessing runtime facts."""

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(stage_contract, Plan3InnerStageContract):
        raise TypeError("stage_contract must be Plan3InnerStageContract")
    request_issues: list[Plan3InnerRolloutIssue] = []
    if request.kind != ExternalKind.INNER_EXAM_OUTCOME:
        request_issues.append(_issue("request-kind-mismatch", "request.kind"))
    if state.phase != RolloutPhase.WAITING_EXTERNAL or state.pending != request:
        request_issues.append(_issue("outer-request-not-pending", "state.pending"))
    if request_issues:
        return _pause(state, request, *request_issues)

    chance, chance_issue = _resolve_chance(
        state, request, start_inputs, branch, chance_provider
    )
    if chance_issue is not None:
        return _pause(state, request, chance_issue)
    assert chance is not None
    refs = (
        _default_runtime_refs(state, chance.start_inputs)
        if runtime_refs is None
        else runtime_refs
    )
    if not isinstance(refs, Plan3InnerRuntimeRefs):
        return _pause(
            state,
            request,
            _issue("runtime-refs-invalid", "runtime_refs"),
            chance=chance,
        )
    binding_issues = _validate_outer_binding(state, request, chance, refs)
    if binding_issues:
        return _pause(state, request, *binding_issues, chance=chance)

    bootstrap = bootstrap_plan3_exam_start(
        chance.start_inputs,
        database=Path(database),
        master_dir=Path(master_dir),
    )
    if isinstance(bootstrap, Plan3ExamStartPause):
        return _pause(
            state,
            request,
            *(
                _issue(
                    f"bootstrap:{issue.code}", issue.field, issue.detail
                )
                for issue in bootstrap.issues
            ),
            chance=chance,
            bootstrap=bootstrap,
        )
    assert isinstance(bootstrap, Plan3ExamStartReady)

    if search is None and any(
        (
            refs.gimmick_ref,
            refs.item_refs,
            refs.support_refs,
            refs.drink_refs,
            refs.passive_refs,
        )
    ):
        return _pause(
            state,
            request,
            _issue(
                "default-search-runtime-refs-unresolved",
                "runtime_refs",
                "inject a search callable that resolves the supplied refs",
            ),
            chance=chance,
            bootstrap=bootstrap,
        )
    try:
        search_request = Plan3InnerSearchRequest(
            outer_state=state,
            external_request=request,
            bootstrap=bootstrap,
            runtime_refs=refs,
            beam_width=beam_width,
        )
    except ValueError as error:
        return _pause(
            state,
            request,
            _issue("search-request-invalid", "beam_width", str(error)),
            chance=chance,
            bootstrap=bootstrap,
        )
    selected_search = _default_search if search is None else search
    if not callable(selected_search):
        return _pause(
            state,
            request,
            _issue("search-callable-invalid", "search"),
            chance=chance,
            bootstrap=bootstrap,
            search_request=search_request,
        )
    try:
        search_result = selected_search(search_request)
    except Exception as error:
        return _pause(
            state,
            request,
            _issue("search-failed", "search", f"{type(error).__name__}:{error}"),
            chance=chance,
            bootstrap=bootstrap,
            search_request=search_request,
        )

    sentinel = object()
    diagnostics = getattr(search_result, "diagnostics", sentinel)
    best = getattr(search_result, "best", sentinel)
    if diagnostics is sentinel or best is sentinel:
        return _pause(
            state,
            request,
            _issue("search-result-shape-invalid", "search_result"),
            chance=chance,
            bootstrap=bootstrap,
            search_request=search_request,
            search_result=search_result,
        )
    try:
        diagnostic_values = tuple(diagnostics)
    except TypeError:
        diagnostic_values = (diagnostics,)
    if diagnostic_values:
        return _pause(
            state,
            request,
            _issue(
                "search-diagnostics-present",
                "search_result.diagnostics",
                f"count={len(diagnostic_values)}",
            ),
            chance=chance,
            bootstrap=bootstrap,
            search_request=search_request,
            search_result=search_result,
        )
    if best is None:
        return _pause(
            state,
            request,
            _issue("search-best-unresolved", "search_result.best"),
            chance=chance,
            bootstrap=bootstrap,
            search_request=search_request,
            search_result=search_result,
        )
    final_state = getattr(best, "state", None)
    final_native = getattr(best, "native_state", None)
    if not isinstance(final_state, Plan3State) or not isinstance(
        final_native, Plan3NativeState
    ):
        return _pause(
            state,
            request,
            _issue("search-best-state-invalid", "search_result.best"),
            chance=chance,
            bootstrap=bootstrap,
            search_request=search_request,
            search_result=search_result,
        )
    try:
        final_native.assert_plan3_projection(final_state)
    except (TypeError, ValueError) as error:
        return _pause(
            state,
            request,
            _issue("search-best-projection-mismatch", "search_result.best", str(error)),
            chance=chance,
            bootstrap=bootstrap,
            search_request=search_request,
            search_result=search_result,
        )
    if final_state.turns_remaining != 0:
        return _pause(
            state,
            request,
            _issue(
                "search-horizon-unfinished",
                "search_result.best.state.turns_remaining",
                str(final_state.turns_remaining),
            ),
            chance=chance,
            bootstrap=bootstrap,
            search_request=search_request,
            search_result=search_result,
        )

    resolution_context = Plan3InnerResolutionContext(
        outer_state=state,
        external_request=request,
        bootstrap=bootstrap,
        search_result=search_result,
        final_state=final_state,
        final_native_state=final_native,
    )
    battle_resolution: Plan3InnerBattleResolution | None = None
    if final_state.is_battle:
        if battle_resolver is None:
            return _pause(
                state,
                request,
                _issue("audition-rank-boundary-unresolved", "battle_resolver"),
                chance=chance,
                bootstrap=bootstrap,
                search_request=search_request,
                search_result=search_result,
            )
        try:
            battle_resolution = battle_resolver(resolution_context)
        except Exception as error:
            return _pause(
                state,
                request,
                _issue(
                    "audition-rank-resolver-failed",
                    "battle_resolver",
                    f"{type(error).__name__}:{error}",
                ),
                chance=chance,
                bootstrap=bootstrap,
                search_request=search_request,
                search_result=search_result,
            )
        if not isinstance(battle_resolution, Plan3InnerBattleResolution):
            return _pause(
                state,
                request,
                _issue("audition-rank-result-invalid", "battle_resolver"),
                chance=chance,
                bootstrap=bootstrap,
                search_request=search_request,
                search_result=search_result,
            )
        cleared = battle_resolution.cleared
    else:
        cleared = final_state.is_clear
    if not cleared:
        return _pause(
            state,
            request,
            _issue("inner-exam-not-cleared", "search_result.best.state"),
            chance=chance,
            bootstrap=bootstrap,
            search_request=search_request,
            search_result=search_result,
        )

    if post_exam_resolution is not None and outcome_resolver is not None:
        return _pause(
            state,
            request,
            _issue(
                "post-exam-resolution-conflict",
                "post_exam_resolution/outcome_resolver",
            ),
            chance=chance,
            bootstrap=bootstrap,
            search_request=search_request,
            search_result=search_result,
        )
    if outcome_resolver is not None:
        try:
            resolved_post = outcome_resolver(resolution_context)
        except Exception as error:
            return _pause(
                state,
                request,
                _issue(
                    "post-exam-resolver-failed",
                    "outcome_resolver",
                    f"{type(error).__name__}:{error}",
                ),
                chance=chance,
                bootstrap=bootstrap,
                search_request=search_request,
                search_result=search_result,
            )
    else:
        resolved_post = post_exam_resolution
    if not isinstance(resolved_post, Plan3InnerPostExamResolution):
        return _pause(
            state,
            request,
            _issue("post-exam-outcome-unresolved", "post_exam_resolution"),
            chance=chance,
            bootstrap=bootstrap,
            search_request=search_request,
            search_result=search_result,
        )
    patch, patch_issue = _merge_stamina_patch(resolved_post, final_state)
    if patch_issue is not None:
        return _pause(
            state,
            request,
            patch_issue,
            chance=chance,
            bootstrap=bootstrap,
            search_request=search_request,
            search_result=search_result,
        )
    assert patch is not None
    outcome = InnerExamOutcome(
        request_id=request.request_id,
        branch=chance.branch,
        cleared=True,
        terminal=stage_contract.is_outer_terminal(request),
        patch=patch,
        reward_requested=resolved_post.reward_requested,
    )
    return Plan3InnerRolloutReady(
        outer_state=state,
        request=request,
        chance=chance,
        bootstrap=bootstrap,
        search_request=search_request,
        search_result=search_result,
        battle_resolution=battle_resolution,
        post_exam_resolution=resolved_post,
        outcome=outcome,
    )


# Compatibility-friendly verb for callers treating this as a resolver.
resolve_plan3_rollout_inner = adapt_plan3_rollout_inner


__all__ = [
    "Plan3InnerBattleResolution",
    "Plan3InnerBattleResolver",
    "Plan3InnerChanceProvider",
    "Plan3InnerChanceSelection",
    "Plan3InnerOutcomeResolver",
    "Plan3InnerPostExamResolution",
    "Plan3InnerResolutionContext",
    "Plan3InnerRolloutIssue",
    "Plan3InnerRolloutPause",
    "Plan3InnerRolloutReady",
    "Plan3InnerRolloutResult",
    "Plan3InnerRolloutStatus",
    "Plan3InnerRuntimeRefs",
    "Plan3InnerSearchCallable",
    "Plan3InnerSearchRequest",
    "Plan3InnerStageContract",
    "adapt_plan3_rollout_inner",
    "resolve_plan3_rollout_inner",
]
