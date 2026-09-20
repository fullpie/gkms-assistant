"""One schedule-driven N.I.A. Master Customize-skip checkpoint.

This is deliberately not a 26-week fixed route.  PC Master owns the
``produce-005`` mode length, audition milestones and ``p_setting-5`` values,
while the caller owns one exact Android ``ProduceSchedule`` row and the
captured/synthetic-exact Customize Start/End response scenario.  The bounded
runner proves only this transition::

    READY_FOR_WEEK -> Customize request -> exact zero-Select response
                   -> next READY_FOR_WEEK

Selecting either of the two cards allowed by ``p_setting-5`` remains a typed
pause.  No Business, SelfLesson, audition, NPC or reward facts are inferred.
The boundary never claims that caller data is an authentic LocalSave.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Mapping, TypeAlias

from .initial_regular_inner_protocol import InitialRegularInnerStageIssue
from .initial_regular_nia_customize_scenario import (
    ANDROID_CUSTOMIZE_STEP_TYPE,
    InitialRegularNiaCustomizeScenario,
    adapt_initial_regular_nia_customize_scenario,
)
from .master_db import DEFAULT_DATABASE
from .nia_outer_action_runtime import (
    NiaOuterActionRuntimeError,
    NiaResolvedWeeklyAction,
    resolve_nia_special_guidance_skip,
)
from .nia_static_adapter import (
    DEFAULT_MASTER_DIR,
    NiaRuntimeScheduleStep,
    adapt_nia_runtime_schedule,
)
from .nia_static_inventory import NiaStaticInventory, build_nia_static_inventory
from .produce_rollout import (
    ExternalRequest,
    Lifecycle,
    ProduceAction,
    ProduceRolloutKernel,
    ProduceRolloutState,
    RolloutPhase,
    RolloutStop,
    WeeklyActionOutcome,
)
from .route_calendar import SPECIAL_GUIDANCE, load_route_calendar


NIA_MASTER_PRODUCE_ID = "produce-005"
NIA_MASTER_TOTAL_WEEKS = 26
NIA_MASTER_FKTN_IDOL_CARD_ID = "i_card-fktn-3-011"
NIA_MASTER_CUSTOMIZE_CHECKPOINT_LABEL = (
    "nia-master-fktn-plan3-single-week-customize-skip"
)
NIA_MASTER_CUSTOMIZE_CHECKPOINT_AUTHORITY = (
    "PC Master Produce/ProduceSetting/calendar milestones; caller-owned exact "
    "Android ProduceSchedule week, outer state/deck/GUIDs and Customize "
    "Start/End CommonResponse; partial one-week checkpoint, not an authentic "
    "N.I.A. LocalSave or a 26-week route"
)


def _issue(code: str, field_name: str, detail: str = "") -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field_name, detail)


def normalize_nia_master_customize_schedule_week(
    record: Mapping[str, object],
) -> NiaRuntimeScheduleStep:
    """Normalize exactly one caller-owned Android ``ProduceSchedule`` row."""

    if not isinstance(record, Mapping):
        raise TypeError("ProduceSchedule week must be an object")
    adapted = adapt_nia_runtime_schedule((record,))
    if len(adapted.steps) != 1 or adapted.diagnostics:
        raise ValueError("one exact ProduceSchedule week is required")
    return adapted.steps[0]


@dataclass(frozen=True, slots=True)
class NiaMasterCustomizeCheckpointScenario:
    """Caller-owned inputs for one produce-005 Customize skip."""

    initial_state: ProduceRolloutState
    schedule_week: NiaRuntimeScheduleStep
    customize_response: InitialRegularNiaCustomizeScenario
    schedule_authority_ref: str
    scenario_label: str = NIA_MASTER_CUSTOMIZE_CHECKPOINT_LABEL

    def __post_init__(self) -> None:
        if not isinstance(self.initial_state, ProduceRolloutState):
            raise TypeError("initial_state must be ProduceRolloutState")
        if not isinstance(self.schedule_week, NiaRuntimeScheduleStep):
            raise TypeError("schedule_week must be NiaRuntimeScheduleStep")
        if not isinstance(
            self.customize_response, InitialRegularNiaCustomizeScenario
        ):
            raise TypeError(
                "customize_response must be InitialRegularNiaCustomizeScenario"
            )
        if not self.schedule_authority_ref or not self.scenario_label:
            raise ValueError("scenario authority/label must be non-empty")

    @classmethod
    def from_runtime_record(
        cls,
        *,
        initial_state: ProduceRolloutState,
        schedule_record: Mapping[str, object],
        customize_response: InitialRegularNiaCustomizeScenario,
        schedule_authority_ref: str,
        scenario_label: str = NIA_MASTER_CUSTOMIZE_CHECKPOINT_LABEL,
    ) -> "NiaMasterCustomizeCheckpointScenario":
        return cls(
            initial_state=initial_state,
            schedule_week=normalize_nia_master_customize_schedule_week(
                schedule_record
            ),
            customize_response=customize_response,
            schedule_authority_ref=schedule_authority_ref,
            scenario_label=scenario_label,
        )

    @property
    def authentic_nia_local_save(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class NiaMasterCustomizeCheckpointPause:
    """Fail-closed boundary for facts outside the executable skip slice."""

    scenario_label: str
    state: ProduceRolloutState
    schedule_week: NiaRuntimeScheduleStep
    issues: tuple[InitialRegularInnerStageIssue, ...]
    stop: RolloutStop = field(default=RolloutStop.POLICY, init=False)
    partial_success: bool = field(default=False, init=False)
    authentic_nia_local_save: bool = field(default=False, init=False)
    authority_description: str = field(
        default=NIA_MASTER_CUSTOMIZE_CHECKPOINT_AUTHORITY,
        init=False,
    )

    def __post_init__(self) -> None:
        issues = tuple(self.issues)
        if not self.scenario_label or not issues:
            raise ValueError("Customize checkpoint pause requires label/issues")
        if not all(isinstance(value, InitialRegularInnerStageIssue) for value in issues):
            raise TypeError("pause issues must be InitialRegularInnerStageIssue")
        object.__setattr__(self, "issues", issues)


@dataclass(frozen=True, slots=True)
class NiaMasterCustomizeCheckpointReport:
    """Successful one-week partial report; not a full Master run."""

    scenario_label: str
    schedule_week: NiaRuntimeScheduleStep
    schedule_authority_ref: str
    initial_state: ProduceRolloutState
    waiting_state: ProduceRolloutState
    request: ExternalRequest
    resolved_action: NiaResolvedWeeklyAction
    final_state: ProduceRolloutState
    stop: RolloutStop = field(default=RolloutStop.POLICY, init=False)
    partial_success: bool = field(default=True, init=False)
    authentic_nia_local_save: bool = field(default=False, init=False)
    authority_description: str = field(
        default=NIA_MASTER_CUSTOMIZE_CHECKPOINT_AUTHORITY,
        init=False,
    )
    issues: tuple[InitialRegularInnerStageIssue, ...] = field(
        default=(),
        init=False,
    )

    def __post_init__(self) -> None:
        if not self.scenario_label or not self.schedule_authority_ref:
            raise ValueError("Customize checkpoint report identity is incomplete")
        if self.schedule_week.number != self.initial_state.week:
            raise ValueError("report schedule week does not match initial state")
        if self.waiting_state.pending != self.request:
            raise ValueError("report request is not pending on waiting state")
        if self.resolved_action.action_id != SPECIAL_GUIDANCE:
            raise ValueError("report did not resolve Customize")
        if self.resolved_action.outcome.reward_requested:
            raise ValueError("Customize skip must not request a reward")
        if self.final_state.week != self.initial_state.week + 1:
            raise ValueError("Customize checkpoint must advance exactly one week")
        if (
            self.final_state.phase is not RolloutPhase.READY_FOR_WEEK
            or self.final_state.pending is not None
            or self.final_state.lifecycle is not Lifecycle.IN_PROGRESS
        ):
            raise ValueError("Customize checkpoint must stop at next READY_FOR_WEEK")


NiaMasterCustomizeCheckpointResult: TypeAlias = (
    NiaMasterCustomizeCheckpointReport | NiaMasterCustomizeCheckpointPause
)


@lru_cache(maxsize=4)
def _load_master_inventory(
    database: Path,
    master_dir: Path,
) -> NiaStaticInventory:
    return build_nia_static_inventory(
        NIA_MASTER_FKTN_IDOL_CARD_ID,
        produce_id=NIA_MASTER_PRODUCE_ID,
        database=database,
        master_dir=master_dir,
    )


def _preflight_issues(
    scenario: NiaMasterCustomizeCheckpointScenario,
    inventory: NiaStaticInventory,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    state = scenario.initial_state
    schedule = scenario.schedule_week
    response = scenario.customize_response
    issues: list[InitialRegularInnerStageIssue] = []
    if state.mode_id != NIA_MASTER_PRODUCE_ID:
        issues.append(
            _issue(
                "nia-master-customize-mode-mismatch",
                "initial_state.mode_id",
                state.mode_id,
            )
        )
    if state.total_weeks != NIA_MASTER_TOTAL_WEEKS:
        issues.append(
            _issue(
                "nia-master-customize-total-weeks-mismatch",
                "initial_state.total_weeks",
                str(state.total_weeks),
            )
        )
    if state.character_id != "fktn":
        issues.append(
            _issue(
                "nia-master-customize-character-mismatch",
                "initial_state.character_id",
                state.character_id,
            )
        )
    if (
        state.phase is not RolloutPhase.READY_FOR_WEEK
        or state.pending is not None
        or state.lifecycle is not Lifecycle.IN_PROGRESS
    ):
        issues.append(
            _issue(
                "nia-master-customize-state-not-ready",
                "initial_state.phase",
                state.phase.value,
            )
        )
    if schedule.number != state.week or response.week != state.week:
        issues.append(
            _issue(
                "nia-master-customize-week-mismatch",
                "schedule/response.week",
                (
                    f"state={state.week}:schedule={schedule.number}:"
                    f"response={response.week}"
                ),
            )
        )
    if schedule.selected_step_type != ANDROID_CUSTOMIZE_STEP_TYPE:
        issues.append(
            _issue(
                "nia-master-customize-step-type-mismatch",
                "ProduceSchedule.SelectedStepType",
                schedule.selected_step_type,
            )
        )
    if inventory.produce_id != NIA_MASTER_PRODUCE_ID:
        issues.append(
            _issue(
                "nia-master-customize-inventory-mode-mismatch",
                "inventory.produce_id",
                inventory.produce_id,
            )
        )
    if inventory.idol_card_id != NIA_MASTER_FKTN_IDOL_CARD_ID:
        issues.append(
            _issue(
                "nia-master-customize-idol-card-mismatch",
                "inventory.idol_card_id",
                inventory.idol_card_id,
            )
        )
    if inventory.total_weeks != NIA_MASTER_TOTAL_WEEKS:
        issues.append(
            _issue(
                "nia-master-customize-inventory-weeks-mismatch",
                "inventory.total_weeks",
                str(inventory.total_weeks),
            )
        )
    if inventory.produce_setting_id != "p_setting-5" or (
        inventory.setting.customize_produce_card_count != 2
    ):
        issues.append(
            _issue(
                "nia-master-customize-setting-mismatch",
                "inventory.setting",
                (
                    f"setting={inventory.produce_setting_id}:count="
                    f"{inventory.setting.customize_produce_card_count}"
                ),
            )
        )
    return tuple(dict.fromkeys(issues))


def _single_week_calendar(
    week: int,
    *,
    master_dir: Path,
):
    calendar = load_route_calendar(
        NIA_MASTER_PRODUCE_ID,
        master_dir=master_dir,
    )
    if calendar.week(week).stage_type is not None:
        return None
    return replace(
        calendar,
        weeks=tuple(
            replace(value, actions=(SPECIAL_GUIDANCE,), exact_actions=True)
            if value.week == week
            else value
            for value in calendar.weeks
        ),
        detail_scope="runtime-produce-schedule-single-week",
        source_note=(
            "caller-owned exact ProduceSchedule row for one Customize week; "
            "all other produce-005 weekly actions remain unresolved"
        ),
    )


def run_nia_master_customize_skip_checkpoint(
    scenario: NiaMasterCustomizeCheckpointScenario,
    *,
    inventory: NiaStaticInventory | None = None,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaMasterCustomizeCheckpointResult:
    """Execute exactly one produce-005 Customize Start/End skip."""

    if not isinstance(scenario, NiaMasterCustomizeCheckpointScenario):
        raise TypeError("scenario must be NiaMasterCustomizeCheckpointScenario")
    resolved_master_dir = Path(master_dir).resolve()
    resolved_inventory = inventory or _load_master_inventory(
        Path(database).resolve(),
        resolved_master_dir,
    )
    if not isinstance(resolved_inventory, NiaStaticInventory):
        raise TypeError("inventory must be NiaStaticInventory")

    issues = list(_preflight_issues(scenario, resolved_inventory))
    calendar = _single_week_calendar(
        scenario.initial_state.week,
        master_dir=resolved_master_dir,
    )
    if calendar is None:
        issues.append(
            _issue(
                "nia-master-customize-audition-week",
                "ProduceSchedule.Number",
                str(scenario.initial_state.week),
            )
        )
    if issues:
        return NiaMasterCustomizeCheckpointPause(
            scenario.scenario_label,
            scenario.initial_state,
            scenario.schedule_week,
            tuple(dict.fromkeys(issues)),
        )

    assert calendar is not None
    kernel = ProduceRolloutKernel(
        calendar,
        rest_recovery_permille=(
            resolved_inventory.setting.refresh_stamina_recovery_permille
        ),
    )
    opened = kernel.apply_action(
        scenario.initial_state,
        ProduceAction.weekly(SPECIAL_GUIDANCE),
    )
    request = opened.request
    if request is None:
        raise AssertionError("Customize action did not produce an external request")

    adapted = adapt_initial_regular_nia_customize_scenario(
        opened.state,
        request,
        scenario.customize_response,
        resolved_inventory,
    )
    if not isinstance(adapted, WeeklyActionOutcome):
        return NiaMasterCustomizeCheckpointPause(
            scenario.scenario_label,
            opened.state,
            scenario.schedule_week,
            tuple(adapted),
        )
    try:
        resolved_action = resolve_nia_special_guidance_skip(
            opened.state,
            request,
            resolved_inventory,
            response_scenario=scenario.customize_response,
        )
    except NiaOuterActionRuntimeError as error:
        return NiaMasterCustomizeCheckpointPause(
            scenario.scenario_label,
            opened.state,
            scenario.schedule_week,
            (
                _issue(
                    "nia-master-customize-runtime-adapter-blocked",
                    "customize_response",
                    str(error),
                ),
            ),
        )
    settled = kernel.resolve_external(opened.state, resolved_action.outcome)
    return NiaMasterCustomizeCheckpointReport(
        scenario_label=scenario.scenario_label,
        schedule_week=scenario.schedule_week,
        schedule_authority_ref=scenario.schedule_authority_ref,
        initial_state=scenario.initial_state,
        waiting_state=opened.state,
        request=request,
        resolved_action=resolved_action,
        final_state=settled.state,
    )


__all__ = [
    "NIA_MASTER_CUSTOMIZE_CHECKPOINT_AUTHORITY",
    "NIA_MASTER_CUSTOMIZE_CHECKPOINT_LABEL",
    "NIA_MASTER_FKTN_IDOL_CARD_ID",
    "NIA_MASTER_PRODUCE_ID",
    "NIA_MASTER_TOTAL_WEEKS",
    "NiaMasterCustomizeCheckpointPause",
    "NiaMasterCustomizeCheckpointReport",
    "NiaMasterCustomizeCheckpointResult",
    "NiaMasterCustomizeCheckpointScenario",
    "normalize_nia_master_customize_schedule_week",
    "run_nia_master_customize_skip_checkpoint",
]
