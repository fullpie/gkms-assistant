"""Bind a caller-owned N.I.A. full scenario to an observed runtime route.

The deterministic 27-week scenario is useful for exercising the simulator,
but its weekly route is not a substitute for the server-generated
``ProduceSchedule``.  This module compares those two inputs without mutating
either one and without relabelling an unsupported runtime step as a similar
looking fixed-scenario action.

Only identities that share an already implemented outer adapter are mapped:

* SelfLesson -> ``self_lesson``
* Business -> ``business``
* FanPresent -> ``care_package`` (the shared Present Start/Receive/End flow)
* Customize -> ``special_guidance``
* Interval -> ``interval`` (the exact Start/Buy/Reroll/End response adapter)
* EventBusiness -> ``event_business`` (the generic ProduceStepEvent response)
* Refresh -> ``refresh`` (the exact ProduceStepRefresh response adapter)

SelfLesson, EventBusiness, Interval, Refresh, and FanPresent can be bound to
typed response mappings outside the version-1 route JSON.  When a SelfLesson
or FanPresent mapping is
explicitly supplied, it is checked as strictly as the other three response
families; omitting that mapping retains the legacy synthetic Master-only
SelfLesson bridge or the version-1 route's embedded Present response.  Interval
and Refresh are never relabelled as the fixed scenario's ``outing`` recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .nia_full_fixed_scenario import NiaFullRouteScenario
from .initial_regular_nia_interval_scenario import (
    InitialRegularNiaIntervalScenario,
)
from .initial_regular_nia_event_business_scenario import (
    InitialRegularNiaEventBusinessScenario,
)
from .initial_regular_nia_business_scenario import (
    InitialRegularNiaBusinessScenario,
)
from .initial_regular_nia_customize_scenario import (
    InitialRegularNiaCustomizeScenario,
)
from .initial_regular_nia_refresh_scenario import (
    InitialRegularNiaRefreshScenario,
)
from .initial_regular_nia_self_lesson_scenario import (
    InitialRegularNiaSelfLessonScenario,
)
from .initial_regular_supply_scenario_adapter import InitialRegularSupplyScenario
from .nia_outer_action_runtime import (
    BUSINESS,
    CARE_PACKAGE,
    REFRESH,
    SELF_LESSON,
    SPECIAL_GUIDANCE,
)
from .nia_runtime_route import (
    NiaRuntimeRouteProjection,
    NiaRuntimeRouteWeek,
)
from .route_calendar import EVENT_BUSINESS, FAN_PRESENT, INTERVAL


_SUPPORTED_ACTION_BINDINGS = {
    SELF_LESSON: SELF_LESSON,
    BUSINESS: BUSINESS,
    FAN_PRESENT: CARE_PACKAGE,
    SPECIAL_GUIDANCE: SPECIAL_GUIDANCE,
    INTERVAL: INTERVAL,
    EVENT_BUSINESS: EVENT_BUSINESS,
    REFRESH: REFRESH,
}


@dataclass(frozen=True, slots=True)
class NiaRuntimeScenarioBindingIssue:
    code: str
    field: str
    detail: str
    week: int | None = None

    def __post_init__(self) -> None:
        if not self.code or not self.field or not self.detail:
            raise ValueError("runtime scenario binding issue is incomplete")
        if self.week is not None and self.week < 1:
            raise ValueError("runtime scenario binding week must be positive")


@dataclass(frozen=True, slots=True)
class NiaRuntimeScenarioWeekBinding:
    week: int
    runtime_step_type: str
    runtime_action: str | None
    scenario_action: str | None
    expected_scenario_action: str | None
    scenario_requirements: tuple[str, ...]
    issues: tuple[NiaRuntimeScenarioBindingIssue, ...] = ()

    def __post_init__(self) -> None:
        if self.week < 1 or not self.runtime_step_type:
            raise ValueError("runtime week binding identity is invalid")

    @property
    def executable(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class NiaRuntimeScenarioBinding:
    produce_id: str
    scenario_authority_ref: str
    route_authority_ref: str
    weeks: tuple[NiaRuntimeScenarioWeekBinding, ...]
    issues: tuple[NiaRuntimeScenarioBindingIssue, ...]

    def __post_init__(self) -> None:
        if not self.produce_id:
            raise ValueError("produce_id is required")
        if not self.scenario_authority_ref or not self.route_authority_ref:
            raise ValueError("binding authority references are required")
        if tuple(value.week for value in self.weeks) != tuple(
            range(1, len(self.weeks) + 1)
        ):
            raise ValueError("runtime scenario bindings must be complete and ordered")

    @property
    def executable(self) -> bool:
        return not self.issues

    @property
    def blocked_weeks(self) -> tuple[int, ...]:
        return tuple(value.week for value in self.weeks if not value.executable)


def _upstream_issue(
    *,
    code: str,
    field: str,
    detail: str,
    week: int | None,
) -> NiaRuntimeScenarioBindingIssue:
    return NiaRuntimeScenarioBindingIssue(
        f"route-{code}",
        field,
        detail,
        week,
    )


def _week_binding(
    runtime_week: NiaRuntimeRouteWeek,
    *,
    scenario_action: str | None,
    business_scenario: InitialRegularNiaBusinessScenario | None,
    require_business_scenario: bool,
    customize_scenario: InitialRegularNiaCustomizeScenario | None,
    require_customize_scenario: bool,
    self_lesson_scenario: InitialRegularNiaSelfLessonScenario | None,
    require_self_lesson_scenario: bool,
    fan_present_scenario: InitialRegularSupplyScenario | None,
    require_fan_present_scenario: bool,
    interval_scenario: InitialRegularNiaIntervalScenario | None,
    event_business_scenario: InitialRegularNiaEventBusinessScenario | None,
    refresh_scenario: InitialRegularNiaRefreshScenario | None,
) -> NiaRuntimeScenarioWeekBinding:
    issues = [
        _upstream_issue(
            code=value.code,
            field=value.field,
            detail=value.detail,
            week=value.week,
        )
        for value in runtime_week.issues
    ]
    expected: str | None = None
    if runtime_week.stage_type is not None:
        if scenario_action is not None:
            issues.append(
                NiaRuntimeScenarioBindingIssue(
                    "scenario-action-on-audition-week",
                    "scenario.weekly_actions",
                    f"audition {runtime_week.stage_type} cannot also run {scenario_action}",
                    runtime_week.number,
                )
            )
    elif runtime_week.selected_action is None:
        issues.append(
            NiaRuntimeScenarioBindingIssue(
                "nia-runtime-action-unmapped",
                "ProduceSchedule.SelectedStepType",
                runtime_week.selected_step_type,
                runtime_week.number,
            )
        )
    else:
        expected = _SUPPORTED_ACTION_BINDINGS.get(runtime_week.selected_action)
        if expected is None:
            issues.append(
                NiaRuntimeScenarioBindingIssue(
                    "nia-runtime-action-adapter-unavailable",
                    "ProduceSchedule.SelectedStepType",
                    runtime_week.selected_step_type,
                    runtime_week.number,
                )
            )
        elif scenario_action != expected:
            issues.append(
                NiaRuntimeScenarioBindingIssue(
                    "nia-runtime-scenario-action-mismatch",
                    "scenario.weekly_actions",
                    f"runtime requires {expected}, scenario supplies {scenario_action}",
                    runtime_week.number,
                )
            )
        elif runtime_week.selected_action == BUSINESS:
            if require_business_scenario and business_scenario is None:
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-business-response-scenario-missing",
                        "business_scenarios",
                        (
                            "requires exact Business Start/Select requests, "
                            "ordered ConsumptionResults/EffectResults, and "
                            "complete CommonResponse post-state"
                        ),
                        runtime_week.number,
                    )
                )
            elif (
                business_scenario is not None
                and business_scenario.week != runtime_week.number
            ):
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-business-response-week-mismatch",
                        "business_scenarios[week].week",
                        (
                            f"route={runtime_week.number};"
                            f"scenario={business_scenario.week}"
                        ),
                        runtime_week.number,
                    )
                )
        elif runtime_week.selected_action == SPECIAL_GUIDANCE:
            if require_customize_scenario and customize_scenario is None:
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-customize-response-scenario-missing",
                        "customize_scenarios",
                        (
                            "requires exact Customize Start/End responses, "
                            "zero Select requests, and complete post-state"
                        ),
                        runtime_week.number,
                    )
                )
            elif (
                customize_scenario is not None
                and customize_scenario.week != runtime_week.number
            ):
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-customize-response-week-mismatch",
                        "customize_scenarios[week].week",
                        (
                            f"route={runtime_week.number};"
                            f"scenario={customize_scenario.week}"
                        ),
                        runtime_week.number,
                    )
                )
        elif runtime_week.selected_action == SELF_LESSON:
            if require_self_lesson_scenario and self_lesson_scenario is None:
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-self-lesson-response-scenario-missing",
                        "self_lesson_scenarios",
                        (
                            "requires exact SelfLesson Start/End responses "
                            "and post-state from the runtime response bundle"
                        ),
                        runtime_week.number,
                    )
                )
            elif (
                self_lesson_scenario is not None
                and self_lesson_scenario.week != runtime_week.number
            ):
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-self-lesson-response-week-mismatch",
                        "self_lesson_scenarios[week].week",
                        (
                            f"route={runtime_week.number};"
                            f"scenario={self_lesson_scenario.week}"
                        ),
                        runtime_week.number,
                    )
                )
        elif runtime_week.selected_action == FAN_PRESENT:
            if require_fan_present_scenario and fan_present_scenario is None:
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-fan-present-response-scenario-missing",
                        "fan_present_scenarios",
                        (
                            "requires exact UserProduceProgressPresent, "
                            "PresentReceive/PresentEnd responses, and post-state "
                            "from the runtime response bundle"
                        ),
                        runtime_week.number,
                    )
                )
        elif runtime_week.selected_action == INTERVAL:
            if interval_scenario is None:
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-interval-response-scenario-missing",
                        "interval_scenarios",
                        (
                            "requires server-resolved UserProduceProgressInterval "
                            "products, ordered Start/Buy/Reroll/End responses, "
                            "and exact post-state"
                        ),
                        runtime_week.number,
                    )
                )
            elif interval_scenario.week != runtime_week.number:
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-interval-response-week-mismatch",
                        "interval_scenarios[week].week",
                        (
                            f"route={runtime_week.number};"
                            f"scenario={interval_scenario.week}"
                        ),
                        runtime_week.number,
                    )
                )
            elif (
                interval_scenario.schedule_refresh_stamina
                != runtime_week.refresh_stamina
            ):
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-interval-schedule-observation-mismatch",
                        "ProduceSchedule.RefreshStamina",
                        (
                            f"route={runtime_week.refresh_stamina};scenario="
                            f"{interval_scenario.schedule_refresh_stamina};"
                            "value is retained but not applied to Interval"
                        ),
                        runtime_week.number,
                    )
                )
        elif runtime_week.selected_action == EVENT_BUSINESS:
            if event_business_scenario is None:
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-event-business-response-scenario-missing",
                        "event_business_scenarios",
                        (
                            "requires caller-selected detail/suggestion, server "
                            "Success, ordered MemoryRewardResults/EffectResults, "
                            "UserProduceProgressEvent, and exact post-state"
                        ),
                        runtime_week.number,
                    )
                )
            elif event_business_scenario.week != runtime_week.number:
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-event-business-response-week-mismatch",
                        "event_business_scenarios[week].week",
                        (
                            f"route={runtime_week.number};"
                            f"scenario={event_business_scenario.week}"
                        ),
                        runtime_week.number,
                    )
                )
        elif runtime_week.selected_action == REFRESH:
            if refresh_scenario is None:
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-refresh-response-scenario-missing",
                        "refresh_scenarios",
                        (
                            "requires before/after stamina, ordered "
                            "EffectResults, exact CommonResponse post-state, "
                            "and the observed RefreshStamina delta"
                        ),
                        runtime_week.number,
                    )
                )
            elif refresh_scenario.week != runtime_week.number:
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-refresh-response-week-mismatch",
                        "refresh_scenarios[week].week",
                        (
                            f"route={runtime_week.number};"
                            f"scenario={refresh_scenario.week}"
                        ),
                        runtime_week.number,
                    )
                )
            elif (
                refresh_scenario.schedule_refresh_stamina
                != runtime_week.refresh_stamina
                or refresh_scenario.response_stamina_delta
                != runtime_week.refresh_stamina
            ):
                issues.append(
                    NiaRuntimeScenarioBindingIssue(
                        "nia-refresh-schedule-delta-mismatch",
                        "ProduceSchedule.RefreshStamina",
                        (
                            f"route={runtime_week.refresh_stamina};"
                            "scenario_observation="
                            f"{refresh_scenario.schedule_refresh_stamina};"
                            "response_delta="
                            f"{refresh_scenario.response_stamina_delta}"
                        ),
                        runtime_week.number,
                    )
                )

    return NiaRuntimeScenarioWeekBinding(
        week=runtime_week.number,
        runtime_step_type=runtime_week.selected_step_type,
        runtime_action=runtime_week.selected_action,
        scenario_action=scenario_action,
        expected_scenario_action=expected,
        scenario_requirements=runtime_week.scenario_requirements,
        issues=tuple(issues),
    )


def bind_nia_full_route_scenario_to_runtime_route(
    scenario: NiaFullRouteScenario,
    route: NiaRuntimeRouteProjection,
    *,
    self_lesson_scenarios: Mapping[
        int,
        InitialRegularNiaSelfLessonScenario,
    ]
    | None = None,
    business_scenarios: Mapping[
        int,
        InitialRegularNiaBusinessScenario,
    ]
    | None = None,
    customize_scenarios: Mapping[
        int,
        InitialRegularNiaCustomizeScenario,
    ]
    | None = None,
    interval_scenarios: Mapping[
        int,
        InitialRegularNiaIntervalScenario,
    ]
    | None = None,
    event_business_scenarios: Mapping[
        int,
        InitialRegularNiaEventBusinessScenario,
    ]
    | None = None,
    refresh_scenarios: Mapping[
        int,
        InitialRegularNiaRefreshScenario,
    ]
    | None = None,
    fan_present_scenarios: Mapping[
        int,
        InitialRegularSupplyScenario,
    ]
    | None = None,
) -> NiaRuntimeScenarioBinding:
    """Compare a full scenario with an exact runtime schedule.

    The function is deliberately validation-only.  A successful result means
    every selected runtime step has a matching implemented scenario adapter.
    Interval, EventBusiness, and Refresh require typed response scenarios for
    their weeks.  SelfLesson and FanPresent do so when their mappings are
    supplied explicitly; ``None`` preserves the existing synthetic
    Master-only SelfLesson bridge and embedded version-1 Present response.
    All mappings stay outside the version-1 route JSON.  Success never claims
    that effect results, product rows, or RNG roots came from an authentic
    LocalSave.
    """

    if not isinstance(scenario, NiaFullRouteScenario):
        raise TypeError("scenario must be NiaFullRouteScenario")
    if not isinstance(route, NiaRuntimeRouteProjection):
        raise TypeError("route must be NiaRuntimeRouteProjection")
    require_business_scenario = business_scenarios is not None
    require_customize_scenario = customize_scenarios is not None
    require_self_lesson_scenario = self_lesson_scenarios is not None
    require_fan_present_scenario = fan_present_scenarios is not None
    if business_scenarios is None:
        business_by_week: dict[int, InitialRegularNiaBusinessScenario] = {}
    else:
        if not isinstance(business_scenarios, Mapping):
            raise TypeError("business_scenarios must be a mapping")
        business_by_week = dict(business_scenarios)
        for week, value in business_by_week.items():
            if type(week) is not int or week < 1:
                raise ValueError("Business scenario keys must be positive weeks")
            if not isinstance(value, InitialRegularNiaBusinessScenario):
                raise TypeError("Business scenario values must be typed")
    if customize_scenarios is None:
        customize_by_week: dict[int, InitialRegularNiaCustomizeScenario] = {}
    else:
        if not isinstance(customize_scenarios, Mapping):
            raise TypeError("customize_scenarios must be a mapping")
        customize_by_week = dict(customize_scenarios)
        for week, value in customize_by_week.items():
            if type(week) is not int or week < 1:
                raise ValueError("Customize scenario keys must be positive weeks")
            if not isinstance(value, InitialRegularNiaCustomizeScenario):
                raise TypeError("Customize scenario values must be typed")
    if self_lesson_scenarios is None:
        self_lesson_by_week: dict[
            int,
            InitialRegularNiaSelfLessonScenario,
        ] = {}
    else:
        if not isinstance(self_lesson_scenarios, Mapping):
            raise TypeError("self_lesson_scenarios must be a mapping")
        self_lesson_by_week = dict(self_lesson_scenarios)
        for week, value in self_lesson_by_week.items():
            if type(week) is not int or week < 1:
                raise ValueError(
                    "SelfLesson scenario keys must be positive weeks"
                )
            if not isinstance(value, InitialRegularNiaSelfLessonScenario):
                raise TypeError("SelfLesson scenario values must be typed")
    if fan_present_scenarios is None:
        fan_present_by_week: dict[int, InitialRegularSupplyScenario] = {}
    else:
        if not isinstance(fan_present_scenarios, Mapping):
            raise TypeError("fan_present_scenarios must be a mapping")
        fan_present_by_week = dict(fan_present_scenarios)
        for week, value in fan_present_by_week.items():
            if type(week) is not int or week < 1:
                raise ValueError(
                    "FanPresent scenario keys must be positive weeks"
                )
            if not isinstance(value, InitialRegularSupplyScenario):
                raise TypeError("FanPresent scenario values must be typed")
    if interval_scenarios is None:
        interval_by_week: dict[int, InitialRegularNiaIntervalScenario] = {}
    else:
        if not isinstance(interval_scenarios, Mapping):
            raise TypeError("interval_scenarios must be a mapping")
        interval_by_week = dict(interval_scenarios)
        for week, value in interval_by_week.items():
            if type(week) is not int or week < 1:
                raise ValueError("interval scenario keys must be positive weeks")
            if not isinstance(value, InitialRegularNiaIntervalScenario):
                raise TypeError("interval scenario values must be typed")
    if event_business_scenarios is None:
        event_business_by_week: dict[
            int,
            InitialRegularNiaEventBusinessScenario,
        ] = {}
    else:
        if not isinstance(event_business_scenarios, Mapping):
            raise TypeError("event_business_scenarios must be a mapping")
        event_business_by_week = dict(event_business_scenarios)
        for week, value in event_business_by_week.items():
            if type(week) is not int or week < 1:
                raise ValueError(
                    "EventBusiness scenario keys must be positive weeks"
                )
            if not isinstance(value, InitialRegularNiaEventBusinessScenario):
                raise TypeError("EventBusiness scenario values must be typed")
    if refresh_scenarios is None:
        refresh_by_week: dict[int, InitialRegularNiaRefreshScenario] = {}
    else:
        if not isinstance(refresh_scenarios, Mapping):
            raise TypeError("refresh_scenarios must be a mapping")
        refresh_by_week = dict(refresh_scenarios)
        for week, value in refresh_by_week.items():
            if type(week) is not int or week < 1:
                raise ValueError("Refresh scenario keys must be positive weeks")
            if not isinstance(value, InitialRegularNiaRefreshScenario):
                raise TypeError("Refresh scenario values must be typed")

    top_level: list[NiaRuntimeScenarioBindingIssue] = []
    if route.produce_id != scenario.initial_state.mode_id:
        top_level.append(
            NiaRuntimeScenarioBindingIssue(
                "nia-runtime-scenario-produce-mismatch",
                "produce_id",
                f"runtime {route.produce_id}; scenario {scenario.initial_state.mode_id}",
            )
        )
    if route.calendar.total_weeks != scenario.initial_state.total_weeks:
        top_level.append(
            NiaRuntimeScenarioBindingIssue(
                "nia-runtime-scenario-length-mismatch",
                "ProduceSchedule",
                (
                    f"runtime {route.calendar.total_weeks}; "
                    f"scenario {scenario.initial_state.total_weeks}"
                ),
            )
        )
    top_level.extend(
        _upstream_issue(
            code=value.code,
            field=value.field,
            detail=value.detail,
            week=value.week,
        )
        for value in route.issues
        if value.week is None
    )

    scenario_by_week = dict(scenario.weekly_actions)
    weeks = tuple(
        _week_binding(
            runtime_week,
            scenario_action=scenario_by_week.get(runtime_week.number),
            business_scenario=business_by_week.get(runtime_week.number),
            require_business_scenario=require_business_scenario,
            customize_scenario=customize_by_week.get(runtime_week.number),
            require_customize_scenario=require_customize_scenario,
            self_lesson_scenario=self_lesson_by_week.get(runtime_week.number),
            require_self_lesson_scenario=require_self_lesson_scenario,
            fan_present_scenario=fan_present_by_week.get(runtime_week.number),
            require_fan_present_scenario=require_fan_present_scenario,
            interval_scenario=interval_by_week.get(runtime_week.number),
            event_business_scenario=event_business_by_week.get(
                runtime_week.number
            ),
            refresh_scenario=refresh_by_week.get(runtime_week.number),
        )
        for runtime_week in route.weeks
    )
    runtime_self_lesson_weeks = {
        value.number
        for value in route.weeks
        if value.selected_action == SELF_LESSON
    }
    runtime_customize_weeks = {
        value.number
        for value in route.weeks
        if value.selected_action == SPECIAL_GUIDANCE
    }
    for week in sorted(set(customize_by_week) - runtime_customize_weeks):
        top_level.append(
            NiaRuntimeScenarioBindingIssue(
                "nia-customize-response-scenario-extra",
                "customize_scenarios",
                "typed Customize response supplied for a non-Customize runtime week",
                week,
            )
        )
    runtime_business_weeks = {
        value.number
        for value in route.weeks
        if value.selected_action == BUSINESS
    }
    for week in sorted(set(business_by_week) - runtime_business_weeks):
        top_level.append(
            NiaRuntimeScenarioBindingIssue(
                "nia-business-response-scenario-extra",
                "business_scenarios",
                "typed Business response supplied for a non-Business runtime week",
                week,
            )
        )
    for week in sorted(set(self_lesson_by_week) - runtime_self_lesson_weeks):
        top_level.append(
            NiaRuntimeScenarioBindingIssue(
                "nia-self-lesson-response-scenario-extra",
                "self_lesson_scenarios",
                (
                    "typed SelfLesson response supplied for a "
                    "non-SelfLesson runtime week"
                ),
                week,
            )
        )
    runtime_fan_present_weeks = {
        value.number
        for value in route.weeks
        if value.selected_action == FAN_PRESENT
    }
    for week in sorted(set(fan_present_by_week) - runtime_fan_present_weeks):
        top_level.append(
            NiaRuntimeScenarioBindingIssue(
                "nia-fan-present-response-scenario-extra",
                "fan_present_scenarios",
                (
                    "typed FanPresent response supplied for a "
                    "non-FanPresent runtime week"
                ),
                week,
            )
        )
    runtime_interval_weeks = {
        value.number
        for value in route.weeks
        if value.selected_action == INTERVAL
    }
    for week in sorted(set(interval_by_week) - runtime_interval_weeks):
        top_level.append(
            NiaRuntimeScenarioBindingIssue(
                "nia-interval-response-scenario-extra",
                "interval_scenarios",
                "typed Interval response supplied for a non-Interval runtime week",
                week,
            )
        )
    runtime_event_business_weeks = {
        value.number
        for value in route.weeks
        if value.selected_action == EVENT_BUSINESS
    }
    for week in sorted(
        set(event_business_by_week) - runtime_event_business_weeks
    ):
        top_level.append(
            NiaRuntimeScenarioBindingIssue(
                "nia-event-business-response-scenario-extra",
                "event_business_scenarios",
                (
                    "typed EventBusiness response supplied for a "
                    "non-EventBusiness runtime week"
                ),
                week,
            )
        )
    runtime_refresh_weeks = {
        value.number
        for value in route.weeks
        if value.selected_action == REFRESH
    }
    for week in sorted(set(refresh_by_week) - runtime_refresh_weeks):
        top_level.append(
            NiaRuntimeScenarioBindingIssue(
                "nia-refresh-response-scenario-extra",
                "refresh_scenarios",
                "typed Refresh response supplied for a non-Refresh runtime week",
                week,
            )
        )
    issues = tuple((*top_level, *(issue for week in weeks for issue in week.issues)))
    return NiaRuntimeScenarioBinding(
        produce_id=route.produce_id,
        scenario_authority_ref=scenario.authority_ref,
        route_authority_ref=route.authority_ref,
        weeks=weeks,
        issues=issues,
    )


__all__ = [
    "NiaRuntimeScenarioBinding",
    "NiaRuntimeScenarioBindingIssue",
    "NiaRuntimeScenarioWeekBinding",
    "bind_nia_full_route_scenario_to_runtime_route",
]
