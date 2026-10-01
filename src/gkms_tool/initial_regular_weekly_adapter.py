"""Fail-closed Initial Regular weekly-action outcome adapter.

The outer rollout kernel deliberately stops before server-owned weekly
results.  This module classifies that pending request using the exact route
calendar, then resolves either a Plan 3 lesson branch or an explicitly
supplied event/activity result.  It performs no screen, save, game, or network
I/O and never derives an outcome from translated display text.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, TypeAlias

from .activity_reward import ActivityRewardState
from .audition_rules import FINAL, MID1, MID2
from .initial_regular_outer_advisor import load_initial_regular_mode_rules
from .initial_regular_rollout import INITIAL_REGULAR_PRODUCE_ID
from .master_db import DEFAULT_DATABASE
from .produce_rollout import (
    AttributeValues,
    ChanceBranch,
    ExternalKind,
    ExternalRequest,
    ProduceRolloutState,
    ProduceStatePatch,
    RolloutPhase,
    WeeklyActionOutcome,
)
from .route_calendar import (
    CLASS,
    CONSULTATION,
    CRAM_LESSON,
    DEFAULT_MASTER_DIR,
    LESSON,
    OUTING,
    SUPPLY,
    load_route_calendar,
)
from .school_event import SchoolEventResolution, resolve_school_event


if TYPE_CHECKING:
    from .plan3_exam_start import Plan3ExamStartInputs
    from .plan3_rollout_inner_adapter import (
        Plan3InnerPostExamResolution,
        Plan3InnerRolloutPause,
        Plan3InnerRolloutReady,
        Plan3InnerRuntimeRefs,
        Plan3InnerSearchCallable,
    )


_LESSON_ACTIONS = frozenset({LESSON, CRAM_LESSON})
_EVENT_ACTIONS = frozenset({CLASS, SUPPLY, OUTING})
_SUPPORTED_ACTIONS = _LESSON_ACTIONS | _EVENT_ACTIONS
_AUDITION_STAGES = frozenset({MID1, MID2, FINAL})
_LESSON_VOCAL = "ProduceStepLessonType_LessonVocal"
_LESSON_DANCE = "ProduceStepLessonType_LessonDance"
_LESSON_VISUAL = "ProduceStepLessonType_LessonVisual"
INITIAL_REGULAR_EVENT_SCENARIO_ADAPTER_REF = (
    "gkms_tool.initial_regular_event_scenario_adapter"
)
INITIAL_REGULAR_PLAN2_INNER_ADAPTER_REF = (
    "gkms_tool.initial_regular_plan2_inner_adapter"
)
_EVENT_ACTIVITY_CONTINUATION_TYPE = "ProduceStepType_EventActivity"
_EVENT_LESSON_CONTINUATIONS = {
    "exam_n-002_school-event_fktn_vo": "ProduceStepType_LessonVocalNormal",
    "exam_n-002_school-event_fktn_da": "ProduceStepType_LessonDanceNormal",
    "exam_n-002_school-event_fktn_vi": "ProduceStepType_LessonVisualNormal",
}


class InitialRegularWeeklyStatus(StrEnum):
    READY = "ready"
    PAUSED = "paused"


class InitialRegularWeeklyNodeKind(StrEnum):
    LESSON = "lesson"
    EVENT = "event"


class InitialRegularLessonAttribute(StrEnum):
    VOCAL = "vocal"
    DANCE = "dance"
    VISUAL = "visual"


_ATTRIBUTE_CONTEXT = {
    InitialRegularLessonAttribute.VOCAL: (1, _LESSON_VOCAL),
    InitialRegularLessonAttribute.DANCE: (2, _LESSON_DANCE),
    InitialRegularLessonAttribute.VISUAL: (3, _LESSON_VISUAL),
}


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularWeeklyIssue:
    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.code or not self.field:
            raise ValueError("issue code and field must be non-empty")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class InitialRegularWeeklyLessonNode:
    outer_state: ProduceRolloutState
    external_request: ExternalRequest
    action_family: str
    kind: InitialRegularWeeklyNodeKind = field(
        default=InitialRegularWeeklyNodeKind.LESSON, init=False
    )
    required_fields: tuple[str, ...] = field(
        default=(
            "attribute",
            "is_sp",
            "chance_branch",
            "lesson_setting_context",
            "exam_rng",
            "card_instance_guids",
            "loadout",
            "exact_post_lesson_patch",
        ),
        init=False,
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "request_id": self.external_request.request_id,
            "week": self.external_request.week,
            "action_family": self.action_family,
            "required_fields": list(self.required_fields),
        }


@dataclass(frozen=True, slots=True)
class InitialRegularWeeklyEventNode:
    outer_state: ProduceRolloutState
    external_request: ExternalRequest
    action_family: str
    kind: InitialRegularWeeklyNodeKind = field(
        default=InitialRegularWeeklyNodeKind.EVENT, init=False
    )
    required_fields: tuple[str, ...] = field(
        default=(
            "chance_branch",
            "exact_state_patch",
            "reward_requested",
            "school_event_or_activity_reward_evidence",
        ),
        init=False,
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "request_id": self.external_request.request_id,
            "week": self.external_request.week,
            "action_family": self.action_family,
            "required_fields": list(self.required_fields),
        }


InitialRegularWeeklyNode: TypeAlias = (
    InitialRegularWeeklyLessonNode | InitialRegularWeeklyEventNode
)


@dataclass(frozen=True, slots=True)
class InitialRegularLessonResolutionInput:
    """Explicit chance and exact post-lesson facts for one lesson branch."""

    attribute: InitialRegularLessonAttribute | None
    is_sp: bool | None
    start_inputs: Plan3ExamStartInputs | None
    branch: ChanceBranch | None
    post_exam_resolution: Plan3InnerPostExamResolution | None

    def __post_init__(self) -> None:
        attribute = self.attribute
        if isinstance(attribute, str) and not isinstance(
            attribute, InitialRegularLessonAttribute
        ):
            try:
                attribute = InitialRegularLessonAttribute(attribute)
            except ValueError as error:
                raise ValueError("attribute must be vocal, dance, visual, or None") from error
            object.__setattr__(self, "attribute", attribute)
        if attribute is not None and not isinstance(
            attribute, InitialRegularLessonAttribute
        ):
            raise TypeError("attribute must be InitialRegularLessonAttribute or None")
        if self.is_sp is not None and not isinstance(self.is_sp, bool):
            raise TypeError("is_sp must be bool or None")
        if self.start_inputs is not None:
            from .plan3_exam_start import Plan3ExamStartInputs as _Plan3ExamStartInputs

            if not isinstance(self.start_inputs, _Plan3ExamStartInputs):
                raise TypeError("start_inputs must be Plan3ExamStartInputs or None")
        if self.branch is not None and not isinstance(self.branch, ChanceBranch):
            raise TypeError("branch must be ChanceBranch or None")
        if self.post_exam_resolution is not None:
            from .plan3_rollout_inner_adapter import (
                Plan3InnerPostExamResolution as _Plan3InnerPostExamResolution,
            )

            if not isinstance(
                self.post_exam_resolution, _Plan3InnerPostExamResolution
            ):
                raise TypeError(
                    "post_exam_resolution must be Plan3InnerPostExamResolution or None"
                )


@dataclass(frozen=True, slots=True)
class InitialRegularSchoolEventEvidence:
    """Visible event identity sufficient for the existing Master resolver."""

    adv_asset_id: str
    observed_stamina_costs: tuple[int, ...]
    selected_slot: int
    suggestion_id: str
    effect_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        costs = tuple(self.observed_stamina_costs)
        effects = tuple(self.effect_ids)
        if not self.adv_asset_id or not self.suggestion_id:
            raise ValueError("school-event asset and suggestion IDs are required")
        if not costs or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in costs
        ):
            raise ValueError("observed_stamina_costs must be non-negative integers")
        if (
            not isinstance(self.selected_slot, int)
            or isinstance(self.selected_slot, bool)
            or self.selected_slot < 1
        ):
            raise ValueError("selected_slot must be a positive integer")
        if any(not isinstance(value, str) or not value for value in effects):
            raise ValueError("effect_ids must contain non-empty identities")
        if len(set(effects)) != len(effects):
            raise ValueError("effect_ids must be unique")
        object.__setattr__(self, "observed_stamina_costs", costs)
        object.__setattr__(self, "effect_ids", effects)


@dataclass(frozen=True, slots=True)
class InitialRegularActivityRewardEvidence:
    """Settled activity overlay plus externally retained reward identity."""

    reward_id: str
    observation: ActivityRewardState

    def __post_init__(self) -> None:
        if not isinstance(self.reward_id, str) or not self.reward_id.strip():
            raise ValueError("reward_id must be non-empty text")
        if not isinstance(self.observation, ActivityRewardState):
            raise TypeError("observation must be ActivityRewardState")


InitialRegularEventEvidence: TypeAlias = (
    InitialRegularSchoolEventEvidence | InitialRegularActivityRewardEvidence
)


@dataclass(frozen=True, slots=True)
class InitialRegularExplicitEventResult:
    """Authoritative result supplied after a server/event choice settles."""

    branch: ChanceBranch
    patch: ProduceStatePatch
    reward_requested: bool
    evidence: InitialRegularEventEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.branch, ChanceBranch):
            raise TypeError("branch must be ChanceBranch")
        if not isinstance(self.patch, ProduceStatePatch):
            raise TypeError("patch must be ProduceStatePatch")
        if not isinstance(self.reward_requested, bool):
            raise TypeError("reward_requested must be a boolean")
        if not isinstance(
            self.evidence,
            (InitialRegularSchoolEventEvidence, InitialRegularActivityRewardEvidence),
        ):
            raise TypeError("evidence must be school-event or activity-reward evidence")


class InitialRegularEventProvider(Protocol):
    def __call__(
        self,
        state: ProduceRolloutState,
        request: ExternalRequest,
        node: InitialRegularWeeklyEventNode,
    ) -> InitialRegularExplicitEventResult: ...


@dataclass(frozen=True, slots=True)
class InitialRegularWeeklyReady:
    outer_state: ProduceRolloutState
    request: ExternalRequest
    node: InitialRegularWeeklyNode
    outcome: WeeklyActionOutcome
    lesson_result: Plan3InnerRolloutReady | None = None
    validated_event: SchoolEventResolution | ActivityRewardState | None = None
    status: InitialRegularWeeklyStatus = field(
        default=InitialRegularWeeklyStatus.READY, init=False
    )
    issues: tuple[InitialRegularWeeklyIssue, ...] = field(default=(), init=False)

    @property
    def available(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class InitialRegularWeeklyPause:
    outer_state: ProduceRolloutState
    request: ExternalRequest
    issues: tuple[InitialRegularWeeklyIssue, ...]
    node: InitialRegularWeeklyNode | None = None
    lesson_result: Plan3InnerRolloutPause | None = None
    status: InitialRegularWeeklyStatus = field(
        default=InitialRegularWeeklyStatus.PAUSED, init=False
    )
    outcome: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not self.issues:
            raise ValueError("paused result must contain at least one issue")

    @property
    def available(self) -> bool:
        return False


InitialRegularWeeklyResult: TypeAlias = (
    InitialRegularWeeklyReady | InitialRegularWeeklyPause
)


def _issue(code: str, field: str, detail: str = "") -> InitialRegularWeeklyIssue:
    return InitialRegularWeeklyIssue(code, field, detail)


def _pause(
    state: ProduceRolloutState,
    request: ExternalRequest,
    *issues: InitialRegularWeeklyIssue,
    node: InitialRegularWeeklyNode | None = None,
    lesson_result: Plan3InnerRolloutPause | None = None,
) -> InitialRegularWeeklyPause:
    return InitialRegularWeeklyPause(
        outer_state=state,
        request=request,
        issues=tuple(issues),
        node=node,
        lesson_result=lesson_result,
    )


def open_initial_regular_weekly_request(
    state: ProduceRolloutState,
    request: ExternalRequest,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> InitialRegularWeeklyNode | InitialRegularWeeklyPause:
    """Validate and classify one pending weekly request into a typed subnode."""

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    issues: list[InitialRegularWeeklyIssue] = []
    if state.mode_id != INITIAL_REGULAR_PRODUCE_ID:
        issues.append(_issue("mode-mismatch", "state.mode_id", state.mode_id))
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
    if issues:
        return _pause(state, request, *issues)
    try:
        calendar = load_route_calendar(
            state.mode_id,
            character_id=state.character_id,
            master_dir=Path(master_dir),
        )
    except (KeyError, OSError, ValueError) as error:
        return _pause(
            state,
            request,
            _issue("route-calendar-unresolved", "state.mode_id", str(error)),
        )
    if state.total_weeks != calendar.total_weeks:
        return _pause(
            state,
            request,
            _issue("route-length-mismatch", "state.total_weeks"),
        )
    route_week = calendar.week(state.week)
    if route_week.stage_type in _AUDITION_STAGES:
        return _pause(
            state,
            request,
            _issue(
                "audition-boundary-not-weekly",
                "request.action_id",
                route_week.stage_type,
            ),
        )

    # A Master NextStep continues the action already selected for this week;
    # its action_id is the exact target detail/exam identity, not another
    # calendar tile.  Only the two typed adapters emitted by the outer event
    # scenario wire may bypass ordinary route-action membership.
    if request.adapter_ref == INITIAL_REGULAR_EVENT_SCENARIO_ADAPTER_REF:
        from .initial_regular_event_effect_catalog import (
            FKTN_INITIAL_REGULAR_EVENT_DETAIL_ID_SET,
        )

        if (
            request.stage_type != _EVENT_ACTIVITY_CONTINUATION_TYPE
            or request.action_id not in FKTN_INITIAL_REGULAR_EVENT_DETAIL_ID_SET
            or "-activity-" not in request.action_id
        ):
            return _pause(
                state,
                request,
                _issue(
                    "event-continuation-identity-invalid",
                    "request.action_id",
                    f"type={request.stage_type}:id={request.action_id}",
                ),
            )
        return InitialRegularWeeklyEventNode(
            outer_state=state,
            external_request=request,
            action_family=OUTING,
        )

    if request.adapter_ref == INITIAL_REGULAR_PLAN2_INNER_ADAPTER_REF:
        expected_type = _EVENT_LESSON_CONTINUATIONS.get(request.action_id)
        if expected_type is None or request.stage_type != expected_type:
            return _pause(
                state,
                request,
                _issue(
                    "event-lesson-continuation-identity-invalid",
                    "request.action_id",
                    f"type={request.stage_type}:id={request.action_id}",
                ),
            )
        return InitialRegularWeeklyLessonNode(
            outer_state=state,
            external_request=request,
            action_family=LESSON,
        )

    if request.action_id not in route_week.actions:
        return _pause(
            state,
            request,
            _issue(
                "action-not-on-route-week",
                "request.action_id",
                f"week={state.week}:action={request.action_id}",
            ),
        )
    if request.action_id == CONSULTATION or request.action_id not in _SUPPORTED_ACTIONS:
        return _pause(
            state,
            request,
            _issue(
                "action-family-unsupported",
                "request.action_id",
                request.action_id,
            ),
        )
    node_type: Callable[..., InitialRegularWeeklyNode]
    node_type = (
        InitialRegularWeeklyLessonNode
        if request.action_id in _LESSON_ACTIONS
        else InitialRegularWeeklyEventNode
    )
    return node_type(
        outer_state=state,
        external_request=request,
        action_family=request.action_id,
    )


def _lesson_input_issues(
    value: InitialRegularLessonResolutionInput | None,
) -> tuple[InitialRegularWeeklyIssue, ...]:
    if value is None:
        return (_issue("lesson-inputs-unresolved", "lesson_inputs"),)
    issues: list[InitialRegularWeeklyIssue] = []
    if value.attribute is None:
        issues.append(_issue("lesson-attribute-unresolved", "lesson_inputs.attribute"))
    if value.is_sp is None:
        issues.append(_issue("lesson-sp-branch-unresolved", "lesson_inputs.is_sp"))
    if value.start_inputs is None:
        issues.append(
            _issue("lesson-start-inputs-unresolved", "lesson_inputs.start_inputs")
        )
    if value.branch is None:
        issues.append(_issue("lesson-chance-unresolved", "lesson_inputs.branch"))
    if value.post_exam_resolution is None:
        issues.append(
            _issue(
                "lesson-post-patch-unresolved",
                "lesson_inputs.post_exam_resolution",
            )
        )
    if value.branch is not None:
        if value.branch.was_sp is None:
            issues.append(
                _issue("lesson-branch-sp-unresolved", "lesson_inputs.branch.was_sp")
            )
        elif value.is_sp is not None and value.branch.was_sp != value.is_sp:
            issues.append(
                _issue("lesson-branch-sp-mismatch", "lesson_inputs.branch.was_sp")
            )
    if value.attribute is not None and value.start_inputs is not None:
        parameter_type, lesson_type = _ATTRIBUTE_CONTEXT[value.attribute]
        context = value.start_inputs.context
        if context.is_battle:
            issues.append(_issue("lesson-context-is-battle", "start_inputs.context"))
        if context.current_parameter_type != parameter_type:
            issues.append(
                _issue(
                    "lesson-attribute-parameter-mismatch",
                    "start_inputs.context.current_parameter_type",
                )
            )
        if context.lesson_type != lesson_type:
            issues.append(
                _issue(
                    "lesson-attribute-type-mismatch",
                    "start_inputs.context.lesson_type",
                )
            )
    return tuple(issues)


def _validate_school_event(
    state: ProduceRolloutState,
    result: InitialRegularExplicitEventResult,
    evidence: InitialRegularSchoolEventEvidence,
    *,
    database: Path,
    master_dir: Path,
) -> tuple[
    SchoolEventResolution | None, tuple[InitialRegularWeeklyIssue, ...]
]:
    if state.character_id not in evidence.adv_asset_id:
        return None, (
            _issue(
                "school-event-character-mismatch",
                "event_result.evidence.adv_asset_id",
            ),
        )
    try:
        resolved = resolve_school_event(
            evidence.adv_asset_id,
            evidence.observed_stamina_costs,
            database=Path(database),
        )
    except (KeyError, LookupError, OSError, ValueError) as error:
        return None, (
            _issue("school-event-unresolved", "event_result.evidence", str(error)),
        )
    if evidence.selected_slot > len(resolved.choices):
        return None, (
            _issue("school-event-slot-invalid", "event_result.evidence.selected_slot"),
        )
    choice = resolved.choices[evidence.selected_slot - 1]
    issues: list[InitialRegularWeeklyIssue] = []
    if choice.suggestion_id != evidence.suggestion_id:
        issues.append(
            _issue(
                "school-event-suggestion-mismatch",
                "event_result.evidence.suggestion_id",
            )
        )
    if choice.effect_ids != evidence.effect_ids:
        issues.append(
            _issue(
                "school-event-effect-ids-mismatch",
                "event_result.evidence.effect_ids",
            )
        )
    # Validate only structured fixed Master effects.  Reward selections and
    # other server-owned choices remain represented by the explicit branch.
    expected_stamina = state.stamina - choice.stamina_cost
    actual_stamina = (
        state.stamina if result.patch.stamina is None else result.patch.stamina
    )
    if expected_stamina < 0 or actual_stamina != expected_stamina:
        issues.append(_issue("school-event-stamina-mismatch", "event_result.patch"))
    expected_points = state.produce_points - choice.produce_point_cost
    actual_points = (
        state.produce_points
        if result.patch.produce_points is None
        else result.patch.produce_points
    )
    if actual_points != expected_points:
        issues.append(
            _issue("school-event-produce-points-mismatch", "event_result.patch")
        )
    if dict(choice.status_delta_min) == dict(choice.status_delta_max):
        fixed_delta = dict(choice.status_delta_min)
        try:
            mode_rules = load_initial_regular_mode_rules(master_dir=Path(master_dir))
        except (KeyError, OSError, ValueError) as error:
            issues.append(
                _issue(
                    "school-event-fixed-effect-unresolved",
                    "event_result.evidence",
                    str(error),
                )
            )
        else:
            expected_attributes = AttributeValues(
                vocal=min(
                    mode_rules.attribute_cap,
                    state.attributes.vocal + fixed_delta.get("vocal", 0),
                ),
                dance=min(
                    mode_rules.attribute_cap,
                    state.attributes.dance + fixed_delta.get("dance", 0),
                ),
                visual=min(
                    mode_rules.attribute_cap,
                    state.attributes.visual + fixed_delta.get("visual", 0),
                ),
            )
            actual_attributes = (
                state.attributes
                if result.patch.attributes is None
                else result.patch.attributes
            )
            if actual_attributes != expected_attributes:
                issues.append(
                    _issue("school-event-attributes-mismatch", "event_result.patch")
                )
    if choice.direct_card_id:
        if result.patch.deck is None:
            issues.append(
                _issue("school-event-direct-card-unbound", "event_result.patch.deck")
            )
        else:
            previous_count = sum(
                entry.count
                for entry in state.deck
                if entry.card_id == choice.direct_card_id
                and entry.upgrade == choice.direct_card_upgrade
            )
            resolved_count = sum(
                entry.count
                for entry in result.patch.deck
                if entry.card_id == choice.direct_card_id
                and entry.upgrade == choice.direct_card_upgrade
            )
            if resolved_count < previous_count + 1:
                issues.append(
                    _issue(
                        "school-event-direct-card-mismatch",
                        "event_result.patch.deck",
                    )
                )
    return resolved, tuple(issues)


def _validate_activity_reward(
    state: ProduceRolloutState,
    result: InitialRegularExplicitEventResult,
    evidence: InitialRegularActivityRewardEvidence,
) -> tuple[ActivityRewardState, tuple[InitialRegularWeeklyIssue, ...]]:
    observation = evidence.observation
    issues: list[InitialRegularWeeklyIssue] = []
    actual_stamina = (
        state.stamina if result.patch.stamina is None else result.patch.stamina
    )
    actual_max = (
        state.max_stamina
        if result.patch.max_stamina is None
        else result.patch.max_stamina
    )
    actual_points = (
        state.produce_points
        if result.patch.produce_points is None
        else result.patch.produce_points
    )
    if actual_stamina != observation.stamina:
        issues.append(_issue("activity-stamina-mismatch", "event_result.patch"))
    if actual_max != observation.max_stamina:
        issues.append(_issue("activity-max-stamina-mismatch", "event_result.patch"))
    if actual_points != observation.produce_points:
        issues.append(
            _issue("activity-produce-points-mismatch", "event_result.patch")
        )
    if (
        result.patch.item_session_refs is None
        or evidence.reward_id not in result.patch.item_session_refs
    ):
        issues.append(
            _issue("activity-reward-id-unbound", "event_result.patch.item_session_refs")
        )
    return observation, tuple(issues)


def resolve_initial_regular_weekly(
    state: ProduceRolloutState,
    request: ExternalRequest,
    *,
    lesson_inputs: InitialRegularLessonResolutionInput | None = None,
    event_result: InitialRegularExplicitEventResult | None = None,
    event_provider: InitialRegularEventProvider | None = None,
    search: Plan3InnerSearchCallable | None = None,
    runtime_refs: Plan3InnerRuntimeRefs | None = None,
    beam_width: int = 64,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> InitialRegularWeeklyResult:
    """Resolve one exact Initial Regular weekly boundary, or return a pause."""

    opened = open_initial_regular_weekly_request(
        state, request, master_dir=Path(master_dir)
    )
    if isinstance(opened, InitialRegularWeeklyPause):
        return opened
    node = opened
    if isinstance(node, InitialRegularWeeklyLessonNode):
        if event_result is not None or event_provider is not None:
            return _pause(
                state,
                request,
                _issue("lesson-event-input-conflict", "event_result/event_provider"),
                node=node,
            )
        lesson_issues = _lesson_input_issues(lesson_inputs)
        if lesson_issues:
            return _pause(state, request, *lesson_issues, node=node)
        assert lesson_inputs is not None
        assert lesson_inputs.start_inputs is not None
        assert lesson_inputs.branch is not None
        assert lesson_inputs.post_exam_resolution is not None
        synthetic_request = ExternalRequest(
            request_id=request.request_id,
            kind=ExternalKind.INNER_EXAM_OUTCOME,
            week=request.week,
            action_id=request.action_id,
            stage_type=lesson_inputs.start_inputs.context.stage_id,
            adapter_ref=request.adapter_ref,
            required_fields=request.required_fields,
        )
        synthetic_state = replace(state, pending=synthetic_request)
        # Legacy Plan3 compatibility is isolated to this explicitly selected
        # path.  Importing the outer weekly shell alone has no Plan3 runtime
        # dependency; common registry branches never execute this block.
        from .plan3_rollout_inner_adapter import (
            Plan3InnerRolloutPause as _Plan3InnerRolloutPause,
            Plan3InnerRolloutReady as _Plan3InnerRolloutReady,
            Plan3InnerStageContract,
            adapt_plan3_rollout_inner,
        )

        inner = adapt_plan3_rollout_inner(
            synthetic_state,
            synthetic_request,
            lesson_inputs.start_inputs,
            branch=lesson_inputs.branch,
            runtime_refs=runtime_refs,
            search=search,
            beam_width=beam_width,
            post_exam_resolution=lesson_inputs.post_exam_resolution,
            stage_contract=Plan3InnerStageContract(terminal_stage_types=()),
            database=Path(database),
            master_dir=Path(master_dir),
        )
        if isinstance(inner, _Plan3InnerRolloutPause):
            return _pause(
                state,
                request,
                *(
                    _issue(f"inner:{issue.code}", issue.field, issue.detail)
                    for issue in inner.issues
                ),
                node=node,
                lesson_result=inner,
            )
        assert isinstance(inner, _Plan3InnerRolloutReady)
        outcome = WeeklyActionOutcome(
            request_id=request.request_id,
            branch=lesson_inputs.branch,
            patch=inner.outcome.patch,
            reward_requested=inner.outcome.reward_requested,
        )
        return InitialRegularWeeklyReady(
            outer_state=state,
            request=request,
            node=node,
            outcome=outcome,
            lesson_result=inner,
        )

    assert isinstance(node, InitialRegularWeeklyEventNode)
    if lesson_inputs is not None:
        return _pause(
            state,
            request,
            _issue("event-lesson-input-conflict", "lesson_inputs"),
            node=node,
        )
    if event_result is not None and event_provider is not None:
        return _pause(
            state,
            request,
            _issue("event-result-provider-conflict", "event_result/event_provider"),
            node=node,
        )
    selected = event_result
    if selected is None and event_provider is not None:
        try:
            selected = event_provider(state, request, node)
        except Exception as error:
            return _pause(
                state,
                request,
                _issue(
                    "event-provider-failed",
                    "event_provider",
                    f"{type(error).__name__}:{error}",
                ),
                node=node,
            )
    if selected is None:
        return _pause(
            state,
            request,
            _issue("event-result-unresolved", "event_result/event_provider"),
            node=node,
        )
    if not isinstance(selected, InitialRegularExplicitEventResult):
        return _pause(
            state,
            request,
            _issue("event-result-invalid", "event_result/event_provider"),
            node=node,
        )
    validated: SchoolEventResolution | ActivityRewardState | None
    if isinstance(selected.evidence, InitialRegularSchoolEventEvidence):
        validated, validation_issues = _validate_school_event(
            state,
            selected,
            selected.evidence,
            database=Path(database),
            master_dir=Path(master_dir),
        )
    else:
        validated, validation_issues = _validate_activity_reward(
            state, selected, selected.evidence
        )
    if validation_issues:
        return _pause(state, request, *validation_issues, node=node)
    outcome = WeeklyActionOutcome(
        request_id=request.request_id,
        branch=selected.branch,
        patch=selected.patch,
        reward_requested=selected.reward_requested,
    )
    return InitialRegularWeeklyReady(
        outer_state=state,
        request=request,
        node=node,
        outcome=outcome,
        validated_event=validated,
    )


adapt_initial_regular_weekly_outcome = resolve_initial_regular_weekly


__all__ = [
    "INITIAL_REGULAR_EVENT_SCENARIO_ADAPTER_REF",
    "INITIAL_REGULAR_PLAN2_INNER_ADAPTER_REF",
    "InitialRegularActivityRewardEvidence",
    "InitialRegularEventEvidence",
    "InitialRegularEventProvider",
    "InitialRegularExplicitEventResult",
    "InitialRegularLessonAttribute",
    "InitialRegularLessonResolutionInput",
    "InitialRegularSchoolEventEvidence",
    "InitialRegularWeeklyEventNode",
    "InitialRegularWeeklyIssue",
    "InitialRegularWeeklyLessonNode",
    "InitialRegularWeeklyNode",
    "InitialRegularWeeklyNodeKind",
    "InitialRegularWeeklyPause",
    "InitialRegularWeeklyReady",
    "InitialRegularWeeklyResult",
    "InitialRegularWeeklyStatus",
    "adapt_initial_regular_weekly_outcome",
    "open_initial_regular_weekly_request",
    "resolve_initial_regular_weekly",
]
