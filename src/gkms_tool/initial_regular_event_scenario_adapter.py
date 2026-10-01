"""Pure wire from one resolved event scenario to an outer weekly outcome.

The response ``EffectResults`` order is authoritative.  Master effect buckets
are used only as an identity multiset: this module never concatenates them to
claim a cross-bucket execution order.  Any incomplete or ambiguous join is a
typed pause.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from .initial_regular_effect_executor import (
    InitialRegularEffectExecution,
    InitialRegularEventMasterProjection,
    InitialRegularMasterEffectSpec,
    execute_initial_regular_event_effects,
)
from .initial_regular_event_effect_catalog import (
    CatalogEffect,
    InitialRegularEventEffectCatalog,
    OpaqueCatalogEffect,
)
from .initial_regular_event_scenario import (
    InitialRegularEventScenario,
    NextStepRef,
    ProduceCardSnapshot,
)
from .initial_regular_rollout import INITIAL_REGULAR_PRODUCE_ID
from .produce_rollout import (
    ChanceBranch,
    ExternalContinuation,
    ExternalKind,
    ExternalRequest,
    ProduceRolloutState,
    RolloutPhase,
    WeeklyActionOutcome,
)


EVENT_SCENARIO_ADAPTER_REF = (
    "gkms_tool.initial_regular_event_scenario_adapter"
)
PLAN2_INNER_ADAPTER_REF = "gkms_tool.initial_regular_plan2_inner_adapter"

_FKTN_CHARACTER_ID = "fktn"
_EVENT_ACTIVITY = "ProduceStepType_EventActivity"
_LESSON_STEP_TYPES = frozenset(
    {
        "ProduceStepType_LessonVocalNormal",
        "ProduceStepType_LessonDanceNormal",
        "ProduceStepType_LessonVisualNormal",
    }
)


class InitialRegularEventScenarioAdapterStatus(StrEnum):
    READY = "ready"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularEventScenarioAdapterIssue:
    """One fail-closed reason why no weekly outcome was projected."""

    code: str
    field: str
    detail: str = ""
    effect_result_index: int | None = None
    produce_effect_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("adapter issue code must be non-empty text")
        if not isinstance(self.field, str) or not self.field:
            raise ValueError("adapter issue field must be non-empty text")
        if not isinstance(self.detail, str):
            raise TypeError("adapter issue detail must be text")
        if self.effect_result_index is not None and (
            isinstance(self.effect_result_index, bool)
            or not isinstance(self.effect_result_index, int)
            or self.effect_result_index < 0
        ):
            raise ValueError("adapter issue effect-result index is invalid")
        if self.produce_effect_id is not None and (
            not isinstance(self.produce_effect_id, str)
            or not self.produce_effect_id
        ):
            raise ValueError("adapter issue produce-effect id is invalid")


@dataclass(frozen=True, slots=True)
class InitialRegularEventScenarioAdapterReady:
    outer_state: ProduceRolloutState
    request: ExternalRequest
    scenario: InitialRegularEventScenario
    catalog: InitialRegularEventEffectCatalog
    branch: ChanceBranch
    master_projection: InitialRegularEventMasterProjection
    execution: InitialRegularEffectExecution
    outcome: WeeklyActionOutcome
    status: InitialRegularEventScenarioAdapterStatus = field(
        default=InitialRegularEventScenarioAdapterStatus.READY,
        init=False,
    )
    issues: tuple[InitialRegularEventScenarioAdapterIssue, ...] = field(
        default=(),
        init=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.outer_state, ProduceRolloutState):
            raise TypeError("ready outer_state must be ProduceRolloutState")
        if not isinstance(self.request, ExternalRequest):
            raise TypeError("ready request must be ExternalRequest")
        if not isinstance(self.scenario, InitialRegularEventScenario):
            raise TypeError("ready scenario must be InitialRegularEventScenario")
        if not isinstance(self.catalog, InitialRegularEventEffectCatalog):
            raise TypeError("ready catalog must be InitialRegularEventEffectCatalog")
        if not isinstance(self.branch, ChanceBranch):
            raise TypeError("ready branch must be ChanceBranch")
        if not isinstance(
            self.master_projection, InitialRegularEventMasterProjection
        ):
            raise TypeError("ready master_projection has the wrong type")
        if not isinstance(self.execution, InitialRegularEffectExecution):
            raise TypeError("ready execution has the wrong type")
        if not self.execution.executable:
            raise ValueError("ready execution must be executable")
        if not isinstance(self.outcome, WeeklyActionOutcome):
            raise TypeError("ready outcome must be WeeklyActionOutcome")
        if self.outcome.branch is not self.branch:
            raise ValueError("ready outcome must retain the caller branch object")
        if self.outcome.reward_requested:
            raise ValueError("scenario-resolved event cannot request another reward")

    @property
    def available(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class InitialRegularEventScenarioAdapterPause:
    outer_state: ProduceRolloutState
    request: ExternalRequest
    scenario: InitialRegularEventScenario
    catalog: InitialRegularEventEffectCatalog
    branch: ChanceBranch
    issues: tuple[InitialRegularEventScenarioAdapterIssue, ...]
    master_projection: InitialRegularEventMasterProjection | None = None
    execution: InitialRegularEffectExecution | None = None
    status: InitialRegularEventScenarioAdapterStatus = field(
        default=InitialRegularEventScenarioAdapterStatus.PAUSED,
        init=False,
    )
    outcome: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        values = tuple(self.issues)
        if not values or not all(
            isinstance(value, InitialRegularEventScenarioAdapterIssue)
            for value in values
        ):
            raise ValueError("adapter pause must contain typed issues")
        object.__setattr__(self, "issues", values)
        if self.master_projection is not None and not isinstance(
            self.master_projection, InitialRegularEventMasterProjection
        ):
            raise TypeError("pause master_projection has the wrong type")
        if self.execution is not None and not isinstance(
            self.execution, InitialRegularEffectExecution
        ):
            raise TypeError("pause execution has the wrong type")

    @property
    def available(self) -> bool:
        return False


InitialRegularEventScenarioAdapterResult: TypeAlias = (
    InitialRegularEventScenarioAdapterReady
    | InitialRegularEventScenarioAdapterPause
)


@dataclass(frozen=True, slots=True)
class _CatalogMember:
    bucket: str
    bucket_index: int
    effect: CatalogEffect

    @property
    def field(self) -> str:
        return f"catalog.{self.bucket}[{self.bucket_index}]"

    @property
    def identity(self) -> tuple[str, str]:
        return self.effect.effect_id, self.effect.effect_type


def _issue(
    code: str,
    field_name: str,
    detail: str = "",
    *,
    index: int | None = None,
    effect_id: str | None = None,
) -> InitialRegularEventScenarioAdapterIssue:
    return InitialRegularEventScenarioAdapterIssue(
        code=code,
        field=field_name,
        detail=detail,
        effect_result_index=index,
        produce_effect_id=effect_id,
    )


def _pause(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularEventScenario,
    catalog: InitialRegularEventEffectCatalog,
    branch: ChanceBranch,
    issues: tuple[InitialRegularEventScenarioAdapterIssue, ...],
    *,
    projection: InitialRegularEventMasterProjection | None = None,
    execution: InitialRegularEffectExecution | None = None,
) -> InitialRegularEventScenarioAdapterPause:
    return InitialRegularEventScenarioAdapterPause(
        outer_state=state,
        request=request,
        scenario=scenario,
        catalog=catalog,
        branch=branch,
        issues=issues,
        master_projection=projection,
        execution=execution,
    )


def _catalog_members(
    catalog: InitialRegularEventEffectCatalog,
) -> tuple[_CatalogMember, ...]:
    members: list[_CatalogMember] = []
    # These loops create an identity pool, not an execution sequence.  Bucket
    # and bucket index remain attached to every member for diagnostics.
    for bucket in (
        "detail_effects",
        "base_effects",
        "success_or_fail_effects",
    ):
        for index, effect in enumerate(getattr(catalog, bucket)):
            members.append(_CatalogMember(bucket, index, effect))
    return tuple(members)


def _opaque_issues(
    members: tuple[_CatalogMember, ...],
) -> tuple[InitialRegularEventScenarioAdapterIssue, ...]:
    return tuple(
        _issue(
            "catalog-effect-opaque",
            member.field,
            member.effect.blocker.message,
            effect_id=member.effect.effect_id,
        )
        for member in members
        if isinstance(member.effect, OpaqueCatalogEffect)
    )


def _join_response_order(
    scenario: InitialRegularEventScenario,
    members: tuple[_CatalogMember, ...],
) -> tuple[
    tuple[InitialRegularMasterEffectSpec, ...] | None,
    tuple[InitialRegularEventScenarioAdapterIssue, ...],
]:
    by_identity: dict[tuple[str, str], list[_CatalogMember]] = {}
    by_id: dict[str, list[_CatalogMember]] = {}
    for member in members:
        by_identity.setdefault(member.identity, []).append(member)
        by_id.setdefault(member.effect.effect_id, []).append(member)

    ambiguous = tuple(
        _issue(
            "effect-join-ambiguous",
            "catalog.effect_identity",
            "identity occurs in multiple Master buckets/positions: "
            + ",".join(member.field for member in candidates),
            effect_id=identity[0],
        )
        for identity, candidates in by_identity.items()
        if len(candidates) > 1
    )
    if ambiguous:
        return None, ambiguous

    used: set[_CatalogMember] = set()
    specs: list[InitialRegularMasterEffectSpec] = []
    issues: list[InitialRegularEventScenarioAdapterIssue] = []
    for index, trace in enumerate(scenario.effect_results):
        if not trace.produce_effect_id:
            issues.append(
                _issue(
                    "effect-join-identity-missing",
                    "scenario.effect_results.produce_effect_id",
                    "response effect has no Master identity",
                    index=index,
                )
            )
            continue
        identity = (trace.produce_effect_id, trace.effect_type)
        candidates = by_identity.get(identity, ())
        if not candidates:
            same_id = by_id.get(trace.produce_effect_id, ())
            if same_id:
                issues.append(
                    _issue(
                        "effect-join-type-mismatch",
                        "scenario.effect_results.effect_type",
                        "response type does not equal the exact Master row type",
                        index=index,
                        effect_id=trace.produce_effect_id,
                    )
                )
            else:
                issues.append(
                    _issue(
                        "effect-join-missing-catalog",
                        "scenario.effect_results.produce_effect_id",
                        "response effect has no member in the selected Master buckets",
                        index=index,
                        effect_id=trace.produce_effect_id,
                    )
                )
            continue
        member = candidates[0]
        if member in used:
            issues.append(
                _issue(
                    "effect-join-extra-response",
                    "scenario.effect_results",
                    "response multiplicity exceeds the selected Master multiset",
                    index=index,
                    effect_id=trace.produce_effect_id,
                )
            )
            continue
        used.add(member)
        effect = member.effect
        specs.append(
            InitialRegularMasterEffectSpec(
                produce_effect_id=effect.effect_id,
                effect_type=effect.effect_type,
                effect_value_min=effect.effect_value_min,
                effect_value_max=effect.effect_value_max,
            )
        )

    for member in members:
        if member not in used:
            issues.append(
                _issue(
                    "effect-join-extra-catalog",
                    member.field,
                    "selected Master effect is absent from the response multiset",
                    effect_id=member.effect.effect_id,
                )
            )
    if issues:
        return None, tuple(issues)
    return tuple(specs), ()


def _continuation(
    scenario: InitialRegularEventScenario,
    catalog: InitialRegularEventEffectCatalog,
) -> tuple[
    ExternalContinuation | None,
    tuple[InitialRegularEventScenarioAdapterIssue, ...],
]:
    direct = catalog.direct_next_step
    outcome = catalog.success_or_fail_next_step
    if direct is not None and outcome is not None:
        return None, (
            _issue(
                "continuation-conflict",
                "catalog.direct_next_step/catalog.success_or_fail_next_step",
                "selected event has two continuation authorities",
            ),
        )
    expected: NextStepRef | None = direct if direct is not None else outcome
    if scenario.next_step != expected:
        return None, (
            _issue(
                "continuation-mismatch",
                "scenario.next_step",
                f"catalog={expected!r}:scenario={scenario.next_step!r}",
            ),
        )
    if expected is None:
        return None, ()
    if expected.step_type == _EVENT_ACTIVITY:
        adapter_ref = EVENT_SCENARIO_ADAPTER_REF
        required_fields = ("event_scenario",)
    elif expected.step_type in _LESSON_STEP_TYPES:
        adapter_ref = PLAN2_INNER_ADAPTER_REF
        required_fields = ("inner_stage_request",)
    else:
        return None, (
            _issue(
                "continuation-type-unsupported",
                "scenario.next_step.step_type",
                expected.step_type,
            ),
        )
    return (
        ExternalContinuation(
            kind=ExternalKind.WEEKLY_ACTION_OUTCOME,
            action_id=expected.step_id,
            stage_type=expected.step_type,
            adapter_ref=adapter_ref,
            required_fields=required_fields,
        ),
        (),
    )


def _preflight_issues(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularEventScenario,
    catalog: InitialRegularEventEffectCatalog,
    branch: ChanceBranch,
    attribute_cap: object,
) -> tuple[InitialRegularEventScenarioAdapterIssue, ...]:
    issues: list[InitialRegularEventScenarioAdapterIssue] = []
    if state.mode_id != INITIAL_REGULAR_PRODUCE_ID:
        issues.append(
            _issue("state-mode-mismatch", "state.mode_id", state.mode_id)
        )
    if state.character_id != _FKTN_CHARACTER_ID:
        issues.append(
            _issue(
                "state-character-mismatch",
                "state.character_id",
                state.character_id,
            )
        )
    if request.kind != ExternalKind.WEEKLY_ACTION_OUTCOME:
        issues.append(_issue("request-kind-mismatch", "request.kind"))
    if state.phase != RolloutPhase.WAITING_EXTERNAL or state.pending != request:
        issues.append(_issue("outer-request-not-pending", "state.pending"))
    if request.week != state.week:
        issues.append(
            _issue(
                "request-week-mismatch",
                "request.week",
                f"state={state.week}:request={request.week}",
            )
        )
    if request.adapter_ref == EVENT_SCENARIO_ADAPTER_REF:
        if (
            request.action_id != scenario.detail_id
            or request.stage_type != _EVENT_ACTIVITY
        ):
            issues.append(
                _issue(
                    "continuation-request-identity-mismatch",
                    "request.action_id/request.stage_type",
                    f"request={request.action_id}/{request.stage_type}:"
                    f"scenario={scenario.detail_id}/{_EVENT_ACTIVITY}",
                )
            )
    elif request.adapter_ref is not None:
        issues.append(
            _issue(
                "event-request-adapter-mismatch",
                "request.adapter_ref",
                request.adapter_ref,
            )
        )
    for field_name, scenario_value, catalog_value in (
        ("detail_id", scenario.detail_id, catalog.detail_id),
        ("suggestion_id", scenario.suggestion_id, catalog.suggestion_id),
        ("suggestion_index", scenario.suggestion_index, catalog.suggestion_index),
        ("success", scenario.success, catalog.actual_success),
    ):
        if scenario_value != catalog_value:
            issues.append(
                _issue(
                    "catalog-scenario-identity-mismatch",
                    field_name,
                    f"scenario={scenario_value!r}:catalog={catalog_value!r}",
                )
            )
    if (
        isinstance(attribute_cap, bool)
        or not isinstance(attribute_cap, int)
        or attribute_cap < 1
    ):
        issues.append(
            _issue(
                "attribute-cap-invalid",
                "attribute_cap",
                repr(attribute_cap),
            )
        )
    return tuple(issues)


def adapt_initial_regular_event_scenario(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularEventScenario,
    catalog: InitialRegularEventEffectCatalog,
    *,
    branch: ChanceBranch,
    attribute_cap: int,
) -> InitialRegularEventScenarioAdapterResult:
    """Project one exact response scenario, or return a typed pause."""

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(scenario, InitialRegularEventScenario):
        raise TypeError("scenario must be InitialRegularEventScenario")
    if not isinstance(catalog, InitialRegularEventEffectCatalog):
        raise TypeError("catalog must be InitialRegularEventEffectCatalog")
    if not isinstance(branch, ChanceBranch):
        raise TypeError("branch must be ChanceBranch")

    issues = list(
        _preflight_issues(
            state,
            request,
            scenario,
            catalog,
            branch,
            attribute_cap,
        )
    )
    members = _catalog_members(catalog)
    issues.extend(_opaque_issues(members))
    specs, join_issues = _join_response_order(scenario, members)
    issues.extend(join_issues)
    continuation, continuation_issues = _continuation(scenario, catalog)
    issues.extend(continuation_issues)

    direct_card: ProduceCardSnapshot | None = None
    if catalog.direct.produce_card_id:
        direct_card = ProduceCardSnapshot(
            id=catalog.direct.produce_card_id,
            upgrade_count=catalog.direct.produce_card_upgrade_count,
        )
    elif catalog.direct.produce_card_upgrade_count:
        issues.append(
            _issue(
                "direct-card-identity-missing",
                "catalog.direct.produce_card_id",
                "upgrade count is nonzero but literal card id is empty",
            )
        )

    if issues:
        return _pause(
            state,
            request,
            scenario,
            catalog,
            branch,
            tuple(issues),
        )
    assert specs is not None
    assert isinstance(attribute_cap, int) and not isinstance(attribute_cap, bool)
    projection = InitialRegularEventMasterProjection(
        detail_id=catalog.detail_id,
        suggestion_id=catalog.suggestion_id,
        suggestion_index=catalog.suggestion_index,
        attribute_cap=attribute_cap,
        effect_specs=specs,
        direct_card=direct_card,
    )
    execution = execute_initial_regular_event_effects(
        state,
        scenario,
        projection,
    )
    if execution.blockers:
        return _pause(
            state,
            request,
            scenario,
            catalog,
            branch,
            tuple(
                _issue(
                    f"executor:{blocker.code}",
                    blocker.field,
                    blocker.message,
                    index=blocker.effect_result_index,
                    effect_id=blocker.produce_effect_id,
                )
                for blocker in execution.blockers
            ),
            projection=projection,
            execution=execution,
        )
    assert execution.patch is not None
    outcome = WeeklyActionOutcome(
        request_id=request.request_id,
        branch=branch,
        patch=execution.patch,
        reward_requested=False,
        continuation=continuation,
    )
    return InitialRegularEventScenarioAdapterReady(
        outer_state=state,
        request=request,
        scenario=scenario,
        catalog=catalog,
        branch=branch,
        master_projection=projection,
        execution=execution,
        outcome=outcome,
    )


adapt_initial_regular_event_scenario_to_weekly_outcome = (
    adapt_initial_regular_event_scenario
)
resolve_initial_regular_event_scenario = adapt_initial_regular_event_scenario


__all__ = [
    "EVENT_SCENARIO_ADAPTER_REF",
    "InitialRegularEventScenarioAdapterIssue",
    "InitialRegularEventScenarioAdapterPause",
    "InitialRegularEventScenarioAdapterReady",
    "InitialRegularEventScenarioAdapterResult",
    "InitialRegularEventScenarioAdapterStatus",
    "PLAN2_INNER_ADAPTER_REF",
    "adapt_initial_regular_event_scenario",
    "adapt_initial_regular_event_scenario_to_weekly_outcome",
    "resolve_initial_regular_event_scenario",
]
