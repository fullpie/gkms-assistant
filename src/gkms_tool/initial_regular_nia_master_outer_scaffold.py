"""Schedule-driven executable prefix for N.I.A. Master ``produce-005``.

The caller supplies an ordered prefix of Android ``ProduceSchedule`` rows
and one typed response per executable week.  Only response adapters already
proven mode-safe are dispatched: exact caller-response Business, Customize
explicit skip, FanPresent, EventBusiness, Interval, and Refresh.  The real 26-week calendar and its
W9/W17/W26 audition milestones remain intact.

This module never turns a prefix into a full-run claim.  SelfLesson,
auditions, Customize card selection, missing/wrong responses and
unmapped step types stop with typed issues while preserving every completed
trace.  Runtime rows and responses are caller authority and are not claimed
to be an authentic LocalSave.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Mapping, TypeAlias

from .audition_rules import FINAL, MID1, MID2
from .initial_regular_inner_protocol import InitialRegularInnerStageIssue
from .initial_regular_nia_business_scenario import InitialRegularNiaBusinessScenario
from .initial_regular_nia_customize_scenario import InitialRegularNiaCustomizeScenario
from .initial_regular_nia_event_business_scenario import (
    InitialRegularNiaEventBusinessScenario,
)
from .initial_regular_nia_interval_scenario import InitialRegularNiaIntervalScenario
from .initial_regular_nia_master_customize_checkpoint import (
    NIA_MASTER_FKTN_IDOL_CARD_ID,
    NIA_MASTER_PRODUCE_ID,
    NIA_MASTER_TOTAL_WEEKS,
    normalize_nia_master_customize_schedule_week,
)
from .initial_regular_nia_refresh_scenario import InitialRegularNiaRefreshScenario
from .initial_regular_nia_self_lesson_scenario import (
    InitialRegularNiaSelfLessonScenario,
)
from .master_db import DEFAULT_DATABASE
from .nia_outer_action_runtime import (
    BUSINESS,
    CARE_PACKAGE,
    EVENT_BUSINESS,
    INTERVAL,
    REFRESH,
    SELF_LESSON,
    SPECIAL_GUIDANCE,
    NiaCarePackageScenario,
    NiaOuterActionRuntimeError,
    NiaResolvedWeeklyAction,
    resolve_nia_care_package,
    resolve_nia_business,
    resolve_nia_event_business,
    resolve_nia_interval,
    resolve_nia_refresh,
    resolve_nia_special_guidance_skip,
)
from .nia_static_adapter import DEFAULT_MASTER_DIR, NiaRuntimeScheduleStep
from .nia_static_inventory import NiaStaticInventory, build_nia_static_inventory
from .produce_rollout import (
    ExternalRequest,
    Lifecycle,
    ProduceAction,
    ProduceRolloutKernel,
    ProduceRolloutState,
    RolloutPhase,
    RolloutStop,
)
from .route_calendar import load_route_calendar


NIA_MASTER_OUTER_SCAFFOLD_LABEL = "nia-master-fktn-plan3-schedule-prefix"
NIA_MASTER_OUTER_SCAFFOLD_AUTHORITY = (
    "PC Master produce-005 26-week mode, p_setting-5 and W9/W17/W26 "
    "milestones; caller-owned ordered ProduceSchedule rows, typed response "
    "payloads, post-states, deck GUIDs and server results; executable prefix "
    "only, not an authentic N.I.A. LocalSave or a full-run claim"
)

_AUDITIONS = frozenset((MID1, MID2, FINAL))
_SELF_LESSONS = frozenset(
    {
        "ProduceStepType_SelfLessonVocalNormal",
        "ProduceStepType_SelfLessonVocalSp",
        "ProduceStepType_SelfLessonDanceNormal",
        "ProduceStepType_SelfLessonDanceSp",
        "ProduceStepType_SelfLessonVisualNormal",
        "ProduceStepType_SelfLessonVisualSp",
    }
)
_STEP_ACTION = {
    "ProduceStepType_Business": BUSINESS,
    "ProduceStepType_Customize": SPECIAL_GUIDANCE,
    "ProduceStepType_FanPresent": CARE_PACKAGE,
    "ProduceStepType_EventBusiness": EVENT_BUSINESS,
    "ProduceStepType_Interval": INTERVAL,
    "ProduceStepType_Refresh": REFRESH,
}

NiaMasterReusableResponse: TypeAlias = (
    InitialRegularNiaBusinessScenario
    | InitialRegularNiaCustomizeScenario
    | NiaCarePackageScenario
    | InitialRegularNiaEventBusinessScenario
    | InitialRegularNiaIntervalScenario
    | InitialRegularNiaRefreshScenario
)
NiaMasterBlockedResponse: TypeAlias = InitialRegularNiaSelfLessonScenario
NiaMasterOuterResponse: TypeAlias = (
    NiaMasterReusableResponse | NiaMasterBlockedResponse | None
)


def _issue(code: str, field_name: str, detail: str = "") -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field_name, detail)


@dataclass(frozen=True, slots=True)
class NiaMasterOuterWeekScenario:
    schedule: NiaRuntimeScheduleStep
    response: NiaMasterOuterResponse

    def __post_init__(self) -> None:
        if not isinstance(self.schedule, NiaRuntimeScheduleStep):
            raise TypeError("schedule must be NiaRuntimeScheduleStep")

    @classmethod
    def from_runtime_record(
        cls,
        record: Mapping[str, object],
        response: NiaMasterOuterResponse,
    ) -> "NiaMasterOuterWeekScenario":
        return cls(normalize_nia_master_customize_schedule_week(record), response)


@dataclass(frozen=True, slots=True)
class NiaMasterOuterScaffoldScenario:
    initial_state: ProduceRolloutState
    weeks: tuple[NiaMasterOuterWeekScenario, ...]
    schedule_authority_ref: str
    scenario_label: str = NIA_MASTER_OUTER_SCAFFOLD_LABEL

    def __post_init__(self) -> None:
        if not isinstance(self.initial_state, ProduceRolloutState):
            raise TypeError("initial_state must be ProduceRolloutState")
        weeks = tuple(self.weeks)
        if not all(isinstance(value, NiaMasterOuterWeekScenario) for value in weeks):
            raise TypeError("weeks must contain NiaMasterOuterWeekScenario")
        numbers = tuple(value.schedule.number for value in weeks)
        expected = tuple(range(self.initial_state.week, self.initial_state.week + len(weeks)))
        if numbers != expected:
            raise ValueError(
                f"ProduceSchedule prefix must be contiguous:expected={expected}:actual={numbers}"
            )
        if not self.schedule_authority_ref or not self.scenario_label:
            raise ValueError("scenario authority/label is required")
        object.__setattr__(self, "weeks", weeks)

    @property
    def authentic_nia_local_save(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class NiaMasterOuterWeekTrace:
    week: int
    selected_step_type: str
    action_id: str
    request: ExternalRequest
    resolved_action: NiaResolvedWeeklyAction
    state_before: ProduceRolloutState
    waiting_state: ProduceRolloutState
    state_after: ProduceRolloutState

    def __post_init__(self) -> None:
        if self.week != self.state_before.week or self.state_after.week != self.week + 1:
            raise ValueError("trace must advance exactly one week")
        if self.waiting_state.pending != self.request:
            raise ValueError("trace request is not pending")
        if self.action_id != self.resolved_action.action_id:
            raise ValueError("trace action identity disagrees")
        if self.resolved_action.outcome.reward_requested:
            raise ValueError("reusable Master prefix actions cannot request a reward")


@dataclass(frozen=True, slots=True)
class NiaMasterOuterScaffoldReport:
    scenario_label: str
    schedule_authority_ref: str
    initial_state: ProduceRolloutState
    final_state: ProduceRolloutState
    traces: tuple[NiaMasterOuterWeekTrace, ...]
    issues: tuple[InitialRegularInnerStageIssue, ...] = ()
    stop: RolloutStop = field(default=RolloutStop.POLICY, init=False)
    authentic_nia_local_save: bool = field(default=False, init=False)
    full_route_complete: bool = field(default=False, init=False)
    authority_description: str = field(
        default=NIA_MASTER_OUTER_SCAFFOLD_AUTHORITY,
        init=False,
    )

    def __post_init__(self) -> None:
        traces = tuple(self.traces)
        issues = tuple(self.issues)
        if not self.scenario_label or not self.schedule_authority_ref:
            raise ValueError("report identity is incomplete")
        if not all(isinstance(value, NiaMasterOuterWeekTrace) for value in traces):
            raise TypeError("report traces must be typed")
        if not all(isinstance(value, InitialRegularInnerStageIssue) for value in issues):
            raise TypeError("report issues must be typed")
        if traces and traces[0].state_before != self.initial_state:
            raise ValueError("first trace does not start at initial state")
        if traces and traces[-1].state_after != self.final_state and not issues:
            raise ValueError("successful prefix final state disagrees with trace")
        object.__setattr__(self, "traces", traces)
        object.__setattr__(self, "issues", issues)

    @property
    def partial_success(self) -> bool:
        return bool(self.traces)

    @property
    def blocked(self) -> bool:
        return bool(self.issues)


@lru_cache(maxsize=4)
def _inventory(database: Path, master_dir: Path) -> NiaStaticInventory:
    return build_nia_static_inventory(
        NIA_MASTER_FKTN_IDOL_CARD_ID,
        produce_id=NIA_MASTER_PRODUCE_ID,
        database=database,
        master_dir=master_dir,
    )


def _report(
    scenario: NiaMasterOuterScaffoldScenario,
    state: ProduceRolloutState,
    traces: list[NiaMasterOuterWeekTrace],
    *issues: InitialRegularInnerStageIssue,
) -> NiaMasterOuterScaffoldReport:
    return NiaMasterOuterScaffoldReport(
        scenario.scenario_label,
        scenario.schedule_authority_ref,
        scenario.initial_state,
        state,
        tuple(traces),
        tuple(dict.fromkeys(issues)),
    )


def _dispatch_issue(
    week: NiaMasterOuterWeekScenario,
    expected_type: type[object],
) -> InitialRegularInnerStageIssue:
    actual = "missing" if week.response is None else type(week.response).__name__
    return _issue(
        "nia-master-scaffold-response-type-mismatch",
        f"weeks[{week.schedule.number}].response",
        f"expected={expected_type.__name__}:actual={actual}",
    )


def _resolve(
    *,
    state: ProduceRolloutState,
    request: ExternalRequest,
    week: NiaMasterOuterWeekScenario,
    action_id: str,
    inventory: NiaStaticInventory,
    database: Path,
) -> NiaResolvedWeeklyAction | InitialRegularInnerStageIssue:
    response = week.response
    try:
        if action_id == BUSINESS:
            if not isinstance(response, InitialRegularNiaBusinessScenario):
                return _dispatch_issue(week, InitialRegularNiaBusinessScenario)
            # Master never has a synthetic fallback here: only the complete
            # caller response scenario can cross this p5 boundary.
            return resolve_nia_business(
                state,
                request,
                response,
                inventory,
                database=database,
            )
        if action_id == SPECIAL_GUIDANCE:
            if not isinstance(response, InitialRegularNiaCustomizeScenario):
                return _dispatch_issue(week, InitialRegularNiaCustomizeScenario)
            if response.selected_card_count != 0:
                return _issue(
                    "nia-master-scaffold-customize-selection-unsupported",
                    f"weeks[{week.schedule.number}].response.selected_card_count",
                    (
                        f"selected={response.selected_card_count}:p_setting-5-limit="
                        f"{inventory.setting.customize_produce_card_count}"
                    ),
                )
            return resolve_nia_special_guidance_skip(
                state,
                request,
                inventory,
                response_scenario=response,
            )
        if action_id == CARE_PACKAGE:
            if not isinstance(response, NiaCarePackageScenario):
                return _dispatch_issue(week, NiaCarePackageScenario)
            return resolve_nia_care_package(state, request, response)
        if action_id == EVENT_BUSINESS:
            if not isinstance(response, InitialRegularNiaEventBusinessScenario):
                return _dispatch_issue(week, InitialRegularNiaEventBusinessScenario)
            return resolve_nia_event_business(
                state,
                request,
                response,
                database=database,
            )
        if action_id == INTERVAL:
            if not isinstance(response, InitialRegularNiaIntervalScenario):
                return _dispatch_issue(week, InitialRegularNiaIntervalScenario)
            if response.schedule_refresh_stamina != week.schedule.refresh_stamina:
                return _issue(
                    "nia-master-scaffold-interval-schedule-mismatch",
                    "ProduceSchedule.RefreshStamina",
                    (
                        f"schedule={week.schedule.refresh_stamina}:"
                        f"response={response.schedule_refresh_stamina}"
                    ),
                )
            return resolve_nia_interval(state, request, response)
        if action_id == REFRESH:
            if not isinstance(response, InitialRegularNiaRefreshScenario):
                return _dispatch_issue(week, InitialRegularNiaRefreshScenario)
            if (
                response.schedule_refresh_stamina != week.schedule.refresh_stamina
                or response.response_stamina_delta != week.schedule.refresh_stamina
            ):
                return _issue(
                    "nia-master-scaffold-refresh-schedule-mismatch",
                    "ProduceSchedule.RefreshStamina",
                    (
                        f"schedule={week.schedule.refresh_stamina}:observation="
                        f"{response.schedule_refresh_stamina}:response_delta="
                        f"{response.response_stamina_delta}"
                    ),
                )
            return resolve_nia_refresh(state, request, response)
    except NiaOuterActionRuntimeError as error:
        return _issue(
            "nia-master-scaffold-response-blocked",
            f"weeks[{week.schedule.number}].response",
            str(error),
        )
    raise AssertionError(f"unhandled reusable action: {action_id}")


def run_nia_master_schedule_outer_scaffold(
    scenario: NiaMasterOuterScaffoldScenario,
    *,
    inventory: NiaStaticInventory | None = None,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaMasterOuterScaffoldReport:
    """Execute the caller's exact non-audition produce-005 prefix."""

    if not isinstance(scenario, NiaMasterOuterScaffoldScenario):
        raise TypeError("scenario must be NiaMasterOuterScaffoldScenario")
    database = Path(database).resolve()
    master_dir = Path(master_dir).resolve()
    static = inventory or _inventory(database, master_dir)
    if not isinstance(static, NiaStaticInventory):
        raise TypeError("inventory must be NiaStaticInventory")
    state = scenario.initial_state
    traces: list[NiaMasterOuterWeekTrace] = []
    initial_issues: list[InitialRegularInnerStageIssue] = []
    if (
        state.mode_id != NIA_MASTER_PRODUCE_ID
        or state.total_weeks != NIA_MASTER_TOTAL_WEEKS
        or state.character_id != "fktn"
        or static.produce_id != NIA_MASTER_PRODUCE_ID
        or static.idol_card_id != NIA_MASTER_FKTN_IDOL_CARD_ID
        or static.produce_setting_id != "p_setting-5"
        or static.setting.customize_produce_card_count != 2
    ):
        initial_issues.append(
            _issue(
                "nia-master-scaffold-identity-mismatch",
                "initial_state/inventory",
                (
                    f"state={state.mode_id}/{state.character_id}/{state.total_weeks}:"
                    f"inventory={static.produce_id}/{static.idol_card_id}/"
                    f"{static.produce_setting_id}/"
                    f"{static.setting.customize_produce_card_count}"
                ),
            )
        )
    if (
        state.phase is not RolloutPhase.READY_FOR_WEEK
        or state.pending is not None
        or state.lifecycle is not Lifecycle.IN_PROGRESS
    ):
        initial_issues.append(
            _issue(
                "nia-master-scaffold-state-not-ready",
                "initial_state.phase",
                state.phase.value,
            )
        )
    if not scenario.weeks:
        initial_issues.append(
            _issue("nia-master-scaffold-schedule-empty", "weeks")
        )
    if initial_issues:
        return _report(scenario, state, traces, *initial_issues)

    calendar = load_route_calendar(NIA_MASTER_PRODUCE_ID, master_dir=master_dir)
    action_by_week = {
        value.schedule.number: _STEP_ACTION.get(value.schedule.selected_step_type)
        for value in scenario.weeks
    }
    runtime_calendar = replace(
        calendar,
        weeks=tuple(
            replace(
                value,
                actions=(action_by_week[value.week],)
                if action_by_week.get(value.week) is not None and value.stage_type is None
                else (),
                exact_actions=value.week in action_by_week,
            )
            for value in calendar.weeks
        ),
        detail_scope="runtime-produce-schedule-prefix",
        source_note=(
            "caller-owned exact ProduceSchedule prefix; remaining Master weeks "
            "and all server results are unresolved"
        ),
    )
    kernel = ProduceRolloutKernel(
        runtime_calendar,
        rest_recovery_permille=static.setting.refresh_stamina_recovery_permille,
    )

    for week in scenario.weeks:
        schedule = week.schedule
        if state.week != schedule.number:
            return _report(
                scenario,
                state,
                traces,
                _issue(
                    "nia-master-scaffold-state-week-drift",
                    "ProduceSchedule.Number",
                    f"state={state.week}:schedule={schedule.number}",
                ),
            )
        master_week = calendar.week(schedule.number)
        selected = schedule.selected_step_type
        if master_week.stage_type is not None:
            code = (
                "nia-master-scaffold-audition-boundary"
                if selected == master_week.stage_type
                else "nia-master-scaffold-audition-schedule-mismatch"
            )
            return _report(
                scenario,
                state,
                traces,
                _issue(
                    code,
                    "ProduceSchedule.SelectedStepType",
                    f"week={schedule.number}:master={master_week.stage_type}:selected={selected}",
                ),
            )
        if selected in _AUDITIONS:
            return _report(
                scenario,
                state,
                traces,
                _issue(
                    "nia-master-scaffold-unexpected-audition",
                    "ProduceSchedule.SelectedStepType",
                    f"week={schedule.number}:{selected}",
                ),
            )
        if selected in _SELF_LESSONS:
            return _report(
                scenario,
                state,
                traces,
                _issue(
                    "nia-master-scaffold-self-lesson-unsupported",
                    "ProduceSchedule.SelectedStepType",
                    "produce-005 has no ProduceStepSelfLesson Master rows",
                ),
            )
        action_id = _STEP_ACTION.get(selected)
        if action_id is None:
            return _report(
                scenario,
                state,
                traces,
                _issue(
                    "nia-master-scaffold-action-unsupported",
                    "ProduceSchedule.SelectedStepType",
                    selected,
                ),
            )
        before = state
        opened = kernel.apply_action(state, ProduceAction.weekly(action_id))
        if opened.request is None:
            raise AssertionError("weekly action did not open an external request")
        resolved = _resolve(
            state=opened.state,
            request=opened.request,
            week=week,
            action_id=action_id,
            inventory=static,
            database=database,
        )
        if isinstance(resolved, InitialRegularInnerStageIssue):
            return _report(scenario, opened.state, traces, resolved)
        settled = kernel.resolve_external(opened.state, resolved.outcome)
        state = settled.state
        traces.append(
            NiaMasterOuterWeekTrace(
                schedule.number,
                selected,
                action_id,
                opened.request,
                resolved,
                before,
                opened.state,
                state,
            )
        )
    return _report(scenario, state, traces)


__all__ = [
    "NIA_MASTER_OUTER_SCAFFOLD_AUTHORITY",
    "NIA_MASTER_OUTER_SCAFFOLD_LABEL",
    "NiaMasterOuterResponse",
    "NiaMasterOuterScaffoldReport",
    "NiaMasterOuterScaffoldScenario",
    "NiaMasterOuterWeekScenario",
    "NiaMasterOuterWeekTrace",
    "NiaMasterReusableResponse",
    "run_nia_master_schedule_outer_scaffold",
]
