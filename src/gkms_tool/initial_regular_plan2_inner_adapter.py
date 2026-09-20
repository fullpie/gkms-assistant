"""Plan2 implementation of the Initial Regular common inner-stage contract.

This adapter is a pure orchestration boundary.  Stage facts come from the
common request; compiled card/runtime objects, ordered-zone binding, and
scheduled gimmick hooks come from injected providers.  It never reads live
state, manufactures provenance, or infers persistent item/passive changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, TypeAlias

from .audition_native_ordered_zones import NativeOrderedZoneEvidenceBinding
from .audition_rules import FINAL
from .initial_regular_inner_protocol import (
    InitialRegularInnerStageIssue,
    InitialRegularInnerStageKind,
    InitialRegularInnerStageOutcome,
    InitialRegularInnerStageRequest,
    validate_initial_regular_inner_stage_outcome,
    validate_initial_regular_inner_stage_request,
)
from .native_exam_formula import ProduceParameterType
from .plan2_exam_mode import (
    Plan2ExamOutcome,
    Plan2ScheduledGimmickHook,
    Plan2ScheduledGimmickHookKey,
    plan2_scheduled_gimmick_hook_key,
)
from .plan2_native_expectimax import (
    Plan2NativeEvaluationWeights,
    Plan2NativeExpectimaxLimits,
)
from .plan2_native_horizon import Plan2NativeProgramCatalog
from .plan2_native_stage_bootstrap import (
    PLAN2_NATIVE_STAGE_PLAN_TYPES,
    Plan2NativeStageAuthority,
    Plan2NativeStageBootstrap,
    Plan2NativeStageChance,
    Plan2NativeStageFacts,
    Plan2NativeStageGimmickFact,
    Plan2NativeStageRuntime,
    bootstrap_plan2_native_stage,
)
from .plan2_native_stage_runner import (
    Plan2NativeStageRunResult,
    StagePlanner,
    StageTransitioner,
    run_plan2_native_stage_to_terminal,
)
from .produce_rollout import AttributeValues, ProduceStatePatch


PLAN2_INITIAL_REGULAR_INNER_ADAPTER_ID: Final = (
    "initial-regular.plan2.native-inner"
)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _refs_or_none(
    values: tuple[str, ...] | None,
    label: str,
) -> tuple[str, ...] | None:
    if values is None:
        return None
    result = tuple(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{label} must contain non-empty text")
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must be unique")
    return result


def _issue(
    code: str,
    field_name: str,
    detail: str = "",
) -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field_name, detail)


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2GimmickProvision:
    """Exact scheduled rows and hook registry for one request group."""

    gimmick_group_id: str
    facts: tuple[Plan2NativeStageGimmickFact, ...] = ()
    hooks: tuple[
        tuple[Plan2ScheduledGimmickHookKey, Plan2ScheduledGimmickHook], ...
    ] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.gimmick_group_id, str):
            raise TypeError("gimmick_group_id must be text")
        facts = tuple(self.facts)
        hooks = tuple(self.hooks)
        if any(
            not isinstance(value, Plan2NativeStageGimmickFact)
            for value in facts
        ):
            raise TypeError("facts must contain Plan2NativeStageGimmickFact")
        if any(
            not isinstance(key, tuple)
            or len(key) != 3
            or isinstance(key[0], bool)
            or not isinstance(key[0], int)
            or key[0] < 1
            or not isinstance(key[1], str)
            or not key[1]
            or not isinstance(key[2], str)
            or not key[2]
            or not isinstance(hook, Plan2ScheduledGimmickHook)
            or hook.effect_id != key[2]
            for key, hook in hooks
        ):
            raise TypeError("hooks must bind schedule-row keys to typed hooks")
        hook_keys = tuple(value[0] for value in hooks)
        if len(hook_keys) != len(set(hook_keys)):
            raise ValueError("gimmick hook schedule-row keys must be unique")
        if any(
            fact.gimmick_group_id != self.gimmick_group_id
            for fact in facts
        ):
            raise ValueError("gimmick facts disagree with the provision group")
        fact_keys = tuple(
            plan2_scheduled_gimmick_hook_key(
                fact.turn,
                fact.gimmick_group_id,
                fact.gimmick_effect_id,
            )
            for fact in facts
        )
        if hook_keys != fact_keys:
            raise ValueError("gimmick hooks must align with scheduled facts")
        object.__setattr__(self, "facts", facts)
        object.__setattr__(self, "hooks", hooks)

    @property
    def hook_map(
        self,
    ) -> dict[Plan2ScheduledGimmickHookKey, Plan2ScheduledGimmickHook]:
        return dict(self.hooks)


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2PostRuntimeRefs:
    """Exact post-stage identities; ``None`` explicitly preserves a field."""

    item_session_refs: tuple[str, ...] | None = None
    drink_session_refs: tuple[str, ...] | None = None
    passive_session_refs: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        for name in (
            "item_session_refs",
            "drink_session_refs",
            "passive_session_refs",
        ):
            object.__setattr__(
                self,
                name,
                _refs_or_none(getattr(self, name), name),
            )


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2AuditionResolution:
    """Injected rank/reward facts not derivable from the terminal score."""

    rank: int
    reward_requested: bool

    def __post_init__(self) -> None:
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ValueError("rank must be a positive integer")
        if not isinstance(self.reward_requested, bool):
            raise TypeError("reward_requested must be boolean")


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2TerminalContext:
    request: InitialRegularInnerStageRequest
    facts: Plan2NativeStageFacts
    bootstrap: Plan2NativeStageBootstrap
    run: Plan2NativeStageRunResult

    @property
    def final_state(self):
        return self.run.final_state


Plan2CatalogProvider: TypeAlias = Callable[
    [InitialRegularInnerStageRequest], Plan2NativeProgramCatalog | None
]
Plan2BindingProvider: TypeAlias = Callable[
    [InitialRegularInnerStageRequest], NativeOrderedZoneEvidenceBinding | None
]
Plan2RuntimeProvider: TypeAlias = Callable[
    [InitialRegularInnerStageRequest], Plan2NativeStageRuntime | None
]
Plan2GimmickProvider: TypeAlias = Callable[
    [InitialRegularInnerStageRequest], InitialRegularPlan2GimmickProvision | None
]
Plan2AttributeCapProvider: TypeAlias = Callable[
    [InitialRegularInnerStageRequest], int | None
]
Plan2PostRuntimeRefsProvider: TypeAlias = Callable[
    [InitialRegularPlan2TerminalContext], InitialRegularPlan2PostRuntimeRefs | None
]
Plan2AuditionResolutionProvider: TypeAlias = Callable[
    [InitialRegularPlan2TerminalContext], InitialRegularPlan2AuditionResolution | None
]


_ATTRIBUTE_BY_PARAMETER: Final = {
    ProduceParameterType.VOCAL: "vocal",
    ProduceParameterType.DANCE: "dance",
    ProduceParameterType.VISUAL: "visual",
}


def plan2_stage_facts_from_inner_request(
    request: InitialRegularInnerStageRequest,
    gimmicks: InitialRegularPlan2GimmickProvision,
    *,
    draw_count: int = 3,
    hand_limit: int = 5,
    plays_remaining: int = 1,
) -> Plan2NativeStageFacts:
    """Perform the direct common-request -> Plan2 stage-facts mapping."""

    if not isinstance(request, InitialRegularInnerStageRequest):
        raise TypeError("request must be InitialRegularInnerStageRequest")
    if not isinstance(gimmicks, InitialRegularPlan2GimmickProvision):
        raise TypeError("gimmicks must be InitialRegularPlan2GimmickProvision")
    required = (
        "stage_id",
        "setting_id",
        "exam_type",
        "step_type_value",
        "limit_turn",
        "extra_turn",
        "clear_border",
        "limit_border",
        "battle_bonus_permille",
        "turn_parameter_types",
    )
    missing = tuple(name for name in required if getattr(request, name) is None)
    if missing:
        raise ValueError("unresolved request fields: " + ",".join(missing))
    if request.gimmick_group_id != gimmicks.gimmick_group_id:
        raise ValueError("request and gimmick provider group IDs disagree")
    assert request.stage_id is not None
    assert request.setting_id is not None
    assert request.exam_type is not None
    assert request.step_type_value is not None
    assert request.limit_turn is not None
    assert request.extra_turn is not None
    assert request.clear_border is not None
    assert request.limit_border is not None
    assert request.battle_bonus_permille is not None
    assert request.turn_parameter_types is not None
    return Plan2NativeStageFacts(
        stage_id=request.stage_id,
        setting_id=request.setting_id,
        plan_type=request.plan_type,
        exam_type=request.exam_type,
        step_type_value=request.step_type_value,
        limit_turn=request.limit_turn,
        extra_turn=request.extra_turn,
        clear_border=request.clear_border,
        limit_border=request.limit_border,
        turn_parameter_types=request.turn_parameter_types,
        battle_bonus_permille=request.battle_bonus_permille,
        draw_count=draw_count,
        hand_limit=hand_limit,
        plays_remaining=plays_remaining,
        gimmicks=gimmicks.facts,
    )


def _provider_missing(name: str) -> tuple[InitialRegularInnerStageIssue, ...]:
    return (
        _issue(
            "plan2-provider-missing",
            f"provider.{name}",
        ),
    )


def _provider_failed(
    name: str,
    error: Exception,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    return (
        _issue(
            "plan2-provider-failed",
            f"provider.{name}",
            f"{type(error).__name__}:{error}",
        ),
    )


def _master_runtime_blocked_issues(
    error: Exception,
) -> tuple[InitialRegularInnerStageIssue, ...] | None:
    """Project only the typed Master-runtime failure without coupling imports."""

    # The Master provider imports this adapter's protocol and Plan2 runtime
    # owners.  Keep the optional glue at the exception boundary so the core
    # adapter has no eager provider-module dependency.
    from .initial_regular_plan2_master_runtime import (
        InitialRegularPlan2MasterRuntimeBlocked,
    )

    if not isinstance(error, InitialRegularPlan2MasterRuntimeBlocked):
        return None
    return tuple(
        _issue(
            blocker.code,
            "provider.runtime_provider",
            blocker.detail,
        )
        for blocker in error.blockers
    )


def _gimmick_runtime_blocked_issues(
    error: Exception,
) -> tuple[InitialRegularInnerStageIssue, ...] | None:
    """Project only the typed Master-gimmick failure at its provider edge."""

    # The optional gimmick module imports this adapter's provision envelope.
    # Resolve its exception lazily so importing the core adapter stays acyclic.
    from .initial_regular_plan2_gimmick_runtime import (
        InitialRegularPlan2GimmickRuntimeBlocked,
    )

    if not isinstance(error, InitialRegularPlan2GimmickRuntimeBlocked):
        return None
    return tuple(
        _issue(
            blocker.code,
            "provider.gimmick_provider",
            blocker.detail,
        )
        for blocker in error.blockers
    )


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2InnerAdapter:
    """Callable common-protocol adapter with injected authoritative inputs."""

    catalog_provider: Plan2CatalogProvider | None = None
    binding_provider: Plan2BindingProvider | None = None
    runtime_provider: Plan2RuntimeProvider | None = None
    gimmick_provider: Plan2GimmickProvider | None = None
    attribute_cap_provider: Plan2AttributeCapProvider | None = None
    post_runtime_refs_provider: Plan2PostRuntimeRefsProvider | None = None
    audition_resolution_provider: Plan2AuditionResolutionProvider | None = None
    authority: Plan2NativeStageAuthority = Plan2NativeStageAuthority.AUTHORITATIVE
    lesson_reward_requested: bool = True
    lesson_failure_reward_requested: bool | None = None
    draw_count: int = 3
    hand_limit: int = 5
    plays_remaining: int = 1
    max_actions: int = 100
    weights: Plan2NativeEvaluationWeights = Plan2NativeEvaluationWeights()
    limits: Plan2NativeExpectimaxLimits = Plan2NativeExpectimaxLimits()
    planner: StagePlanner | None = field(default=None, repr=False, compare=False)
    transitioner: StageTransitioner | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        try:
            authority = Plan2NativeStageAuthority(self.authority)
        except (TypeError, ValueError) as error:
            raise ValueError("unsupported Plan2 stage authority") from error
        object.__setattr__(self, "authority", authority)
        for name in (
            "catalog_provider",
            "binding_provider",
            "runtime_provider",
            "gimmick_provider",
            "attribute_cap_provider",
            "post_runtime_refs_provider",
            "audition_resolution_provider",
            "planner",
            "transitioner",
        ):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise TypeError(f"{name} must be callable or None")
        for name, minimum in (
            ("draw_count", 0),
            ("hand_limit", 1),
            ("plays_remaining", 0),
            ("max_actions", 1),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if not isinstance(self.lesson_reward_requested, bool):
            raise TypeError("lesson_reward_requested must be boolean")
        if (
            self.lesson_failure_reward_requested is not None
            and not isinstance(self.lesson_failure_reward_requested, bool)
        ):
            raise TypeError(
                "lesson_failure_reward_requested must be boolean or None"
            )
        if not isinstance(self.weights, Plan2NativeEvaluationWeights):
            raise TypeError("weights must be Plan2NativeEvaluationWeights")
        if not isinstance(self.limits, Plan2NativeExpectimaxLimits):
            raise TypeError("limits must be Plan2NativeExpectimaxLimits")

    def _mode_issues(
        self,
        request: InitialRegularInnerStageRequest,
        facts: Plan2NativeStageFacts,
    ) -> tuple[InitialRegularInnerStageIssue, ...]:
        if request.plan_type not in PLAN2_NATIVE_STAGE_PLAN_TYPES:
            return (
                _issue(
                    "plan2-plan-type-mismatch",
                    "request.plan_type",
                    request.plan_type,
                ),
            )
        if request.stage_kind is InitialRegularInnerStageKind.AUDITION:
            return ()
        from .plan2_exam_mode import resolve_plan2_exam_mode

        try:
            mode = resolve_plan2_exam_mode(
                facts.exam_type,
                facts.step_type_value,
            )
        except Exception as error:
            return (
                _issue(
                    "plan2-mode-invalid",
                    "request.step_type_value",
                    f"{type(error).__name__}:{error}",
                ),
            )
        expected_attribute = _ATTRIBUTE_BY_PARAMETER.get(mode.parameter_type)
        issues: list[InitialRegularInnerStageIssue] = []
        if request.attribute != expected_attribute:
            issues.append(
                _issue(
                    "plan2-lesson-attribute-mismatch",
                    "request.attribute",
                    f"expected={expected_attribute}:actual={request.attribute}",
                )
            )
        if request.is_sp is not mode.is_sp:
            issues.append(
                _issue(
                    "plan2-lesson-sp-mismatch",
                    "request.is_sp",
                    f"expected={mode.is_sp}:actual={request.is_sp}",
                )
            )
        return tuple(issues)

    def __call__(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> InitialRegularInnerStageOutcome | tuple[InitialRegularInnerStageIssue, ...]:
        if not isinstance(request, InitialRegularInnerStageRequest):
            raise TypeError("request must be InitialRegularInnerStageRequest")
        request_issues = validate_initial_regular_inner_stage_request(request)
        if request_issues:
            return request_issues
        for name in (
            "catalog_provider",
            "binding_provider",
            "runtime_provider",
            "gimmick_provider",
        ):
            if getattr(self, name) is None:
                return _provider_missing(name)
        assert self.catalog_provider is not None
        assert self.binding_provider is not None
        assert self.runtime_provider is not None
        assert self.gimmick_provider is not None

        try:
            gimmicks = self.gimmick_provider(request)
        except Exception as error:
            gimmick_runtime_issues = _gimmick_runtime_blocked_issues(error)
            if gimmick_runtime_issues is not None:
                return gimmick_runtime_issues
            return _provider_failed("gimmick_provider", error)
        if not isinstance(gimmicks, InitialRegularPlan2GimmickProvision):
            return (
                _issue(
                    "plan2-provider-result-invalid",
                    "provider.gimmick_provider",
                ),
            )
        if request.gimmick_group_id and not gimmicks.facts:
            return (
                _issue(
                    "plan2-gimmick-facts-unresolved",
                    "provider.gimmick_provider",
                    request.gimmick_group_id,
                ),
            )
        try:
            facts = plan2_stage_facts_from_inner_request(
                request,
                gimmicks,
                draw_count=self.draw_count,
                hand_limit=self.hand_limit,
                plays_remaining=self.plays_remaining,
            )
        except (TypeError, ValueError) as error:
            return (
                _issue(
                    "plan2-stage-facts-invalid",
                    "request",
                    f"{type(error).__name__}:{error}",
                ),
            )
        mode_issues = self._mode_issues(request, facts)
        if mode_issues:
            return mode_issues

        try:
            catalog = self.catalog_provider(request)
        except Exception as error:
            return _provider_failed("catalog_provider", error)
        if not isinstance(catalog, Plan2NativeProgramCatalog):
            return (
                _issue(
                    "plan2-provider-result-invalid",
                    "provider.catalog_provider",
                ),
            )
        try:
            binding = self.binding_provider(request)
        except Exception as error:
            return _provider_failed("binding_provider", error)
        if binding is not None and not isinstance(
            binding, NativeOrderedZoneEvidenceBinding
        ):
            return (
                _issue(
                    "plan2-provider-result-invalid",
                    "provider.binding_provider",
                ),
            )
        try:
            runtime = self.runtime_provider(request)
        except Exception as error:
            master_runtime_issues = _master_runtime_blocked_issues(error)
            if master_runtime_issues is not None:
                return master_runtime_issues
            return _provider_failed("runtime_provider", error)
        if not isinstance(runtime, Plan2NativeStageRuntime):
            return (
                _issue(
                    "plan2-provider-result-invalid",
                    "provider.runtime_provider",
                ),
            )
        assert request.rng_before is not None
        chance = Plan2NativeStageChance(
            random_state=request.rng_before,
            binding=binding,
            authority=self.authority,
        )
        bootstrap = bootstrap_plan2_native_stage(
            facts,
            request.outer_state,
            chance=chance,
            catalog=catalog,
            runtime=runtime,
            gimmick_hooks=gimmicks.hook_map,
        )
        if not bootstrap.simulation_ready:
            return tuple(
                _issue(
                    f"plan2-bootstrap:{blocker.code}",
                    "plan2.bootstrap",
                    blocker.detail,
                )
                for blocker in bootstrap.blockers
            )

        run_kwargs: dict[str, object] = {
            "max_actions": self.max_actions,
            "weights": self.weights,
            "limits": self.limits,
        }
        if self.planner is not None:
            run_kwargs["planner"] = self.planner
        if self.transitioner is not None:
            run_kwargs["transitioner"] = self.transitioner
        run = run_plan2_native_stage_to_terminal(
            bootstrap,
            catalog,
            **run_kwargs,  # type: ignore[arg-type]
        )
        if not run.completed or run.final_state is None:
            blockers = run.blockers or ()
            if blockers:
                return tuple(
                    _issue(
                        f"plan2-run:{blocker.code}",
                        "plan2.runner",
                        blocker.detail,
                    )
                    for blocker in blockers
                )
            return (
                _issue(
                    "plan2-run-incomplete",
                    "plan2.runner",
                    run.stop_reason,
                ),
            )

        final = run.final_state
        context = InitialRegularPlan2TerminalContext(
            request=request,
            facts=facts,
            bootstrap=bootstrap,
            run=run,
        )
        post_refs: InitialRegularPlan2PostRuntimeRefs | None = None
        if self.post_runtime_refs_provider is not None:
            try:
                post_refs = self.post_runtime_refs_provider(context)
            except Exception as error:
                return _provider_failed("post_runtime_refs_provider", error)
            if not isinstance(post_refs, InitialRegularPlan2PostRuntimeRefs):
                return (
                    _issue(
                        "plan2-provider-result-invalid",
                        "provider.post_runtime_refs_provider",
                    ),
                )
        score = final.scalar.score
        cleared = final.outcome is Plan2ExamOutcome.CLEAR
        rank: int | None = None
        if request.stage_kind is InitialRegularInnerStageKind.LESSON:
            if not cleared and self.lesson_failure_reward_requested is None:
                return (
                    _issue(
                        "plan2-lesson-failure-outcome-unresolved",
                        "provider.lesson_failure_reward_requested",
                        (
                            f"score={score}:clear_border={facts.clear_border};"
                            "server response reward branch is required"
                        ),
                    ),
                )
            if self.attribute_cap_provider is None:
                return _provider_missing("attribute_cap_provider")
            try:
                attribute_cap = self.attribute_cap_provider(request)
            except Exception as error:
                return _provider_failed("attribute_cap_provider", error)
            if (
                isinstance(attribute_cap, bool)
                or not isinstance(attribute_cap, int)
                or attribute_cap < 0
            ):
                return (
                    _issue(
                        "plan2-attribute-cap-unresolved",
                        "provider.attribute_cap_provider",
                    ),
                )
            assert request.attribute is not None
            current = getattr(request.outer_state.attributes, request.attribute)
            if attribute_cap < current:
                return (
                    _issue(
                        "plan2-attribute-cap-invalid",
                        "provider.attribute_cap_provider",
                        f"current={current}:cap={attribute_cap}",
                    ),
                )
            values = request.outer_state.attributes.to_dict()
            values[request.attribute] = min(current + score, attribute_cap)
            attributes = AttributeValues(**values)
            reward_requested = (
                self.lesson_reward_requested
                if cleared
                else bool(self.lesson_failure_reward_requested)
            )
            terminal = False
        else:
            if facts.clear_border < 1:
                return (
                    _issue(
                        "plan2-audition-clear-boundary-unresolved",
                        "request.clear_border",
                    ),
                )
            attributes = None
            terminal = request.stage_type == FINAL
            if self.audition_resolution_provider is None:
                return _provider_missing("audition_resolution_provider")
            try:
                audition = self.audition_resolution_provider(context)
            except Exception as error:
                return _provider_failed("audition_resolution_provider", error)
            if not isinstance(audition, InitialRegularPlan2AuditionResolution):
                return (
                    _issue(
                        "plan2-provider-result-invalid",
                        "provider.audition_resolution_provider",
                    ),
                )
            cleared = audition.rank <= facts.clear_border
            if audition.reward_requested and not cleared:
                return (
                    _issue(
                        "plan2-audition-reward-on-failure",
                        "provider.audition_resolution_provider",
                    ),
                )
            rank = audition.rank
            reward_requested = audition.reward_requested

        patch = ProduceStatePatch(
            stamina=final.scalar.stamina,
            attributes=attributes,
            item_session_refs=(
                None if post_refs is None else post_refs.item_session_refs
            ),
            drink_session_refs=(
                None if post_refs is None else post_refs.drink_session_refs
            ),
            passive_session_refs=(
                None if post_refs is None else post_refs.passive_session_refs
            ),
        )
        trace = (
            f"adapter:{PLAN2_INITIAL_REGULAR_INNER_ADAPTER_ID}",
            f"authority:{self.authority.value}",
            *(f"action:{value}" for value in run.action_path),
            f"terminal-score:{score}",
            f"lesson-cleared:{str(cleared).lower()}"
            if request.stage_kind is InitialRegularInnerStageKind.LESSON
            else f"audition-rank:{rank}",
            (
                "post-runtime-refs:preserved"
                if post_refs is None
                else "post-runtime-refs:provider"
            ),
        )
        outcome = InitialRegularInnerStageOutcome(
            request_id=request.external_request_id,
            branch=request.branch,
            patch=patch,
            cleared=cleared,
            terminal=terminal,
            reward_requested=reward_requested,
            rng_after=final.zones.random_state,
            score=score,
            rank=rank,
            trace=trace,
        )
        outcome_issues = validate_initial_regular_inner_stage_outcome(
            request,
            outcome,
        )
        return outcome_issues or outcome


def build_initial_regular_plan2_inner_adapter(
    **kwargs: object,
) -> InitialRegularPlan2InnerAdapter:
    """Small factory convenient for registry construction."""

    return InitialRegularPlan2InnerAdapter(**kwargs)  # type: ignore[arg-type]


InitialRegularPlan2InnerAdapterResult: TypeAlias = (
    InitialRegularInnerStageOutcome
    | tuple[InitialRegularInnerStageIssue, ...]
)


__all__ = [
    "PLAN2_INITIAL_REGULAR_INNER_ADAPTER_ID",
    "InitialRegularPlan2AuditionResolution",
    "InitialRegularPlan2GimmickProvision",
    "InitialRegularPlan2InnerAdapter",
    "InitialRegularPlan2InnerAdapterResult",
    "InitialRegularPlan2PostRuntimeRefs",
    "InitialRegularPlan2TerminalContext",
    "Plan2AttributeCapProvider",
    "Plan2AuditionResolutionProvider",
    "Plan2BindingProvider",
    "Plan2CatalogProvider",
    "Plan2GimmickProvider",
    "Plan2PostRuntimeRefsProvider",
    "Plan2RuntimeProvider",
    "build_initial_regular_plan2_inner_adapter",
    "plan2_stage_facts_from_inner_request",
]
