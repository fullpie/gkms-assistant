"""Project runtime ``ProduceSchedule`` into an exact N.I.A. route calendar.

N.I.A. Master does not have a fixed player-facing weekly table in PC Master.
Android v3.2.3 serializes the generated route as repeated ``ProduceSchedule``
records.  This module keeps those runtime rows authoritative and only maps
their enum identities into the outer simulator vocabulary.  It does not
invent event results, reward rolls, card choices, or post-action values.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from .audition_rules import FINAL, MID1, MID2
from .initial_regular_nia_refresh_scenario import NIA_REFRESH_ACTION_ID
from .nia_static_adapter import (
    DEFAULT_MASTER_DIR,
    NiaRuntimeScheduleStep,
    NiaScheduleAdaptation,
    NiaStaticDiagnostic,
)
from .produce_runtime_calendar import (
    RuntimeCalendarProjection,
    project_runtime_calendar,
)
from .route_calendar import (
    BUSINESS,
    CARE_PACKAGE,
    EVENT_BUSINESS,
    FAN_PRESENT,
    INTERVAL,
    SELF_LESSON,
    SPECIAL_GUIDANCE,
    RouteCalendar,
    RouteWeek,
    load_route_calendar,
)


_AUDITION_TYPES = frozenset((MID1, MID2, FINAL))
_SELF_LESSON_TYPES = frozenset(
    (
        "ProduceStepType_SelfLessonVocalNormal",
        "ProduceStepType_SelfLessonVocalSp",
        "ProduceStepType_SelfLessonDanceNormal",
        "ProduceStepType_SelfLessonDanceSp",
        "ProduceStepType_SelfLessonVisualNormal",
        "ProduceStepType_SelfLessonVisualSp",
    )
)
_STEP_ACTION = {
    **{value: SELF_LESSON for value in _SELF_LESSON_TYPES},
    "ProduceStepType_Business": BUSINESS,
    "ProduceStepType_EventBusiness": EVENT_BUSINESS,
    "ProduceStepType_FanPresent": FAN_PRESENT,
    "ProduceStepType_Customize": SPECIAL_GUIDANCE,
    "ProduceStepType_Interval": INTERVAL,
    "ProduceStepType_Refresh": NIA_REFRESH_ACTION_ID,
}
_SCENARIO_REQUIREMENTS = {
    SELF_LESSON: (
        "selected attribute/SP identity",
        "exact stamina and parameter outcome",
    ),
    BUSINESS: (
        "server-selected detail/suggestion",
        "ordered effect results and rolled reward",
    ),
    EVENT_BUSINESS: (
        "server-selected event branch",
        (
            "caller-selected suggestion index, ProduceStepEvent Success, ordered "
            "MemoryRewardResults/EffectResults, and exact post-state"
        ),
    ),
    FAN_PRESENT: (
        "UserProduceProgressPresent",
        "receive/end response and exact post-state",
    ),
    SPECIAL_GUIDANCE: (
        "selected card customization or explicit skip",
        "exact post-state",
    ),
    INTERVAL: (
        "server-resolved UserProduceProgressInterval products",
        "ordered Start/Buy/Reroll/End responses and exact post-step state",
    ),
    NIA_REFRESH_ACTION_ID: (
        "ProduceStepRefresh before/after stamina and ordered effect results",
        "exact CommonResponse post-state and RefreshStamina delta",
    ),
}


@dataclass(frozen=True, slots=True)
class NiaRuntimeRouteIssue:
    code: str
    field: str
    detail: str
    week: int | None = None

    def __post_init__(self) -> None:
        if not self.code or not self.field or not self.detail:
            raise ValueError("runtime route issue fields must be non-empty")
        if self.week is not None and self.week < 1:
            raise ValueError("runtime route issue week must be positive")


@dataclass(frozen=True, slots=True)
class NiaRuntimeRouteWeek:
    number: int
    selected_step_type: str
    selected_action: str | None
    available_step_types: tuple[str, ...]
    available_actions: tuple[str, ...]
    step_sub_parameter_types: tuple[str, ...]
    refresh_stamina: int
    stage_type: str | None
    scenario_requirements: tuple[str, ...]
    issues: tuple[NiaRuntimeRouteIssue, ...] = ()

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("runtime route week must be positive")
        if not self.selected_step_type:
            raise ValueError("selected_step_type is required")
        if self.refresh_stamina < 0:
            raise ValueError("refresh_stamina cannot be negative")
        if self.stage_type is not None and self.stage_type not in _AUDITION_TYPES:
            raise ValueError("stage_type is not an N.I.A. audition")
        if self.stage_type is not None and self.selected_action is not None:
            raise ValueError("audition weeks cannot also select an outer action")

    @property
    def needs_runtime_outcome(self) -> bool:
        return self.stage_type is None and bool(self.scenario_requirements)

    @property
    def mapped(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class NiaRuntimeRouteProjection:
    produce_id: str
    calendar: RouteCalendar
    schedule: NiaScheduleAdaptation
    weeks: tuple[NiaRuntimeRouteWeek, ...]
    issues: tuple[NiaRuntimeRouteIssue, ...]
    authority_ref: str = (
        "Android UserProduceProgressSchedule/ProduceSchedule + PC Master milestones"
    )

    def __post_init__(self) -> None:
        if self.calendar.produce_id != self.produce_id:
            raise ValueError("calendar mode does not match projection")
        if len(self.weeks) != self.calendar.total_weeks:
            raise ValueError("runtime route must contain every week")
        if tuple(value.number for value in self.weeks) != tuple(
            range(1, self.calendar.total_weeks + 1)
        ):
            raise ValueError("runtime route weeks must be ordered and complete")

    @property
    def complete_identity(self) -> bool:
        return self.schedule.complete and not self.issues

    @property
    def runtime_outcome_weeks(self) -> tuple[int, ...]:
        return tuple(
            value.number for value in self.weeks if value.needs_runtime_outcome
        )

    @property
    def audition_weeks(self) -> tuple[tuple[int, str], ...]:
        return tuple(
            (value.number, value.stage_type)
            for value in self.weeks
            if value.stage_type is not None
        )


def _action(step_type: str) -> str | None:
    return _STEP_ACTION.get(step_type)


def _week_projection(
    step: NiaRuntimeScheduleStep,
    *,
    expected_stage: str | None,
) -> NiaRuntimeRouteWeek:
    issues: list[NiaRuntimeRouteIssue] = []
    selected_is_stage = step.selected_step_type in _AUDITION_TYPES
    if expected_stage is not None:
        if step.selected_step_type != expected_stage:
            issues.append(
                NiaRuntimeRouteIssue(
                    "runtime-stage-selection-mismatch",
                    "ProduceSchedule.SelectedStepType",
                    f"expected {expected_stage}, received {step.selected_step_type}",
                    step.number,
                )
            )
        stage_type = expected_stage
        selected_action = None
    else:
        stage_type = None
        selected_action = _action(step.selected_step_type)
        if selected_is_stage:
            issues.append(
                NiaRuntimeRouteIssue(
                    "runtime-unexpected-audition",
                    "ProduceSchedule.SelectedStepType",
                    step.selected_step_type,
                    step.number,
                )
            )
        elif selected_action is None:
            issues.append(
                NiaRuntimeRouteIssue(
                    "runtime-step-unmapped",
                    "ProduceSchedule.SelectedStepType",
                    step.selected_step_type,
                    step.number,
                )
            )

    available_step_types = tuple(dict.fromkeys(step.all_step_types))
    available_actions: list[str] = []
    for candidate in available_step_types:
        if candidate in _AUDITION_TYPES:
            continue
        mapped = _action(candidate)
        if mapped is None:
            issues.append(
                NiaRuntimeRouteIssue(
                    "runtime-candidate-unmapped",
                    "ProduceSchedule.StepTypes",
                    candidate,
                    step.number,
                )
            )
        elif mapped not in available_actions:
            available_actions.append(mapped)

    requirements = (
        ()
        if selected_action is None
        else _SCENARIO_REQUIREMENTS[selected_action]
    )
    return NiaRuntimeRouteWeek(
        number=step.number,
        selected_step_type=step.selected_step_type,
        selected_action=selected_action,
        available_step_types=available_step_types,
        available_actions=tuple(available_actions),
        step_sub_parameter_types=step.step_sub_parameter_types,
        refresh_stamina=step.refresh_stamina,
        stage_type=stage_type,
        scenario_requirements=requirements,
        issues=tuple(issues),
    )


def _nia_schedule_projection(
    runtime: RuntimeCalendarProjection,
) -> NiaScheduleAdaptation:
    """Retain the public N.I.A. schedule view over shared parsed weeks."""

    invalid = next(
        (
            issue
            for issue in runtime.issues
            if issue.code
            in {
                "runtime-row-invalid",
                "runtime-row-incomplete",
                "runtime-calendar-duplicate-weeks",
            }
        ),
        None,
    )
    if invalid is not None:
        # The former direct parser rejected malformed/duplicate records rather
        # than manufacturing a partial NiaScheduleAdaptation.  Preserve that
        # fail-closed API behaviour while sharing the actual field parsing.
        raise ValueError(invalid.detail)

    steps = tuple(
        NiaRuntimeScheduleStep(
            number=week.step_number,
            selected_step_type=week.selected_step_type,
            step_types=week.step_types,
            step_sub_parameter_types=week.step_sub_parameter_types,
            refresh_stamina=week.refresh_stamina,
        )
        for week in runtime.weeks
    )
    diagnostics: list[NiaStaticDiagnostic] = []
    incomplete = next(
        (
            issue
            for issue in runtime.issues
            if issue.code == "runtime-calendar-incomplete"
        ),
        None,
    )
    if incomplete is not None:
        diagnostics.append(
            NiaStaticDiagnostic(
                "runtime-schedule-incomplete",
                incomplete.detail,
                ("ProduceSchedule.Number",),
            )
        )

    present = {
        step_type
        for week in runtime.weeks
        for step_type in week.all_step_types
        if step_type in _AUDITION_TYPES
    }
    missing = tuple(
        stage
        for stage in (MID1, MID2, FINAL)
        if stage not in present
    )
    if missing:
        diagnostics.append(
            NiaStaticDiagnostic(
                "runtime-audition-weeks-incomplete",
                "missing schedule stages: " + ",".join(missing),
                (
                    "ProduceSchedule.SelectedStepType",
                    "ProduceSchedule.StepTypes",
                ),
            )
        )
    return NiaScheduleAdaptation(steps, tuple(diagnostics))


def project_nia_runtime_route(
    records: Sequence[Mapping[str, object]],
    *,
    produce_id: str,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaRuntimeRouteProjection:
    """Validate and project a complete runtime schedule without guessing rows."""

    runtime = project_runtime_calendar(
        records,
        produce_id=produce_id,
        source_family="nia-runtime-route",
        master_dir=Path(master_dir),
    )
    calendar = load_route_calendar(
        produce_id,
        master_dir=Path(master_dir),
    )
    schedule = _nia_schedule_projection(runtime)
    stage_by_week = {
        value.week: value.step_type for value in runtime.mode.fixed_milestones
    }
    weeks = tuple(
        _week_projection(
            step,
            expected_stage=stage_by_week.get(step.number),
        )
        for step in schedule.steps
    )
    issues = [
        NiaRuntimeRouteIssue(
            value.code,
            "ProduceSchedule",
            value.detail,
        )
        for value in schedule.diagnostics
    ]
    issues.extend(issue for week in weeks for issue in week.issues)

    runtime_calendar = replace(
        calendar,
        weeks=tuple(
            RouteWeek(
                week=value.number,
                actions=value.available_actions,
                stage_type=value.stage_type,
                exact_actions=True,
            )
            for value in weeks
        ),
        detail_scope="runtime-produce-schedule",
        source_note=(
            "Android runtime ProduceSchedule supplies exact generated weekly "
            "choices; PC Master supplies mode length and audition milestones"
        ),
    )
    return NiaRuntimeRouteProjection(
        produce_id=produce_id,
        calendar=runtime_calendar,
        schedule=schedule,
        weeks=weeks,
        issues=tuple(issues),
    )


__all__ = [
    "NiaRuntimeRouteIssue",
    "NiaRuntimeRouteProjection",
    "NiaRuntimeRouteWeek",
    "project_nia_runtime_route",
]
