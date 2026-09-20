"""Read-only outer-action advice for N.I.A. Pro and Master.

The serialized Produce play log is authoritative for current values and the
current week.  A completed overview/training read supplies only the choices
that are visible now.  This module joins those observations to N.I.A.-specific
Master rules; it never captures a window and never carries a click payload.

N.I.A. Pro has exact ``ProduceStepSelfLesson`` rows in the current Master.
N.I.A. Master does not.  Master lesson advice therefore stays unavailable
unless the same phase/tier was observed in the current play log or the caller
supplies an explicit, named policy profile.  Initial's 13-week route, 1000
parameter cap, and percentage lesson-cost estimate are never reused.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
import re
from typing import Any, Callable

import yaml

from .audition_rules import FINAL, MID1, MID2
from .nia_outer_exam_save_adapter import NiaOuterExamSaveComposition
from .nia_outer_resource_model import (
    NiaOutingOptionEffect,
    NiaTwoWeekRecoveryContext,
    compare_nia_two_week_recovery,
    load_nia_outing_option_effect,
    project_nia_lesson_stamina_cost,
)
from .nia_outer_prior import NiaOuterPrior
from .nia_static_adapter import (
    DEFAULT_MASTER_DIR,
    NIA_PRODUCE_IDS,
    adapt_nia_runtime_schedule,
)
from .nia_outer_policy_adapter import rank_nia_outer_lesson_policy_v1
from .outer_policy_core import OuterPolicyWeights
from .overview_actions import (
    ACTIVITY,
    CLASS,
    DANCE_LESSON,
    REST,
    VOCAL_LESSON,
    VISUAL_LESSON,
)
from .produce_outer_local_save import (
    DEFAULT_PC_GAME_ROOT,
    ProduceOuterLocalSaveSnapshot,
    read_current_produce_outer_local_save,
)
from .route_calendar import (
    BUSINESS,
    CARE_PACKAGE,
    CONSULTATION,
    OUTING,
    SELF_LESSON,
    SPECIAL_GUIDANCE,
    RouteCalendar,
    load_route_calendar,
)
from .run_shadow import RunShadowState


SCHEMA_NAME = "gkms.nia-outer-advice.v1"
POLICY_SCHEMA_NAME = "gkms.nia-outer-policy.v1"
STATUS_READY = "ready"
STATUS_UNAVAILABLE = "unavailable"
UNAVAILABLE_ACTION = "unavailable"

STAGE_BEFORE_MID1 = "before_mid1"
STAGE_MID1 = "mid1"
STAGE_BETWEEN_MID1_MID2 = "between_mid1_mid2"
STAGE_MID2 = "mid2"
STAGE_BETWEEN_MID2_FINAL = "between_mid2_final"
STAGE_FINAL = "final"

_MASTER_STAGE_TO_PUBLIC = {
    MID1: STAGE_MID1,
    MID2: STAGE_MID2,
    FINAL: STAGE_FINAL,
}
_LESSON_ACTION_TO_ATTRIBUTE: Mapping[str, tuple[str, str]] = {
    VOCAL_LESSON: ("Vo", "vocal"),
    DANCE_LESSON: ("Da", "dance"),
    VISUAL_LESSON: ("Vi", "visual"),
}
_ATTRIBUTE_TO_ACTION = {
    attribute: action
    for action, (attribute, _field) in _LESSON_ACTION_TO_ATTRIBUTE.items()
}
_KNOWN_NON_LESSON_ACTIONS = frozenset(
    {
        ACTIVITY,
        BUSINESS,
        CARE_PACKAGE,
        CLASS,
        OUTING,
        REST,
        SPECIAL_GUIDANCE,
    }
)
_SHOP_ACTIONS = frozenset({CONSULTATION, "shop"})
# Productive actions that do not enter a lesson immediately.  When one of
# these is visible, taking it first preserves the current opportunity and lets
# the next LocalSave provide fresh stamina authority before Rest is considered
# again.  OUTING is deliberately excluded: its recovery/tradeoffs require an
# exact suggestion row and must continue through _outing_recovery_advice.
_REST_DEFERRING_ACTIONS = frozenset(
    {
        ACTIVITY,
        BUSINESS,
        CARE_PACKAGE,
        SPECIAL_GUIDANCE,
    }
)
_KNOWN_ACTIONS = frozenset(
    {
        *_LESSON_ACTION_TO_ATTRIBUTE,
        *_KNOWN_NON_LESSON_ACTIONS,
        *_SHOP_ACTIONS,
        SELF_LESSON,
    }
)
_SELF_LESSON_ID = re.compile(
    r"^self_lesson-(produce_00[45])-(0[123])-(normal|sp)$"
)


class NiaOuterRouteAdapterError(ValueError):
    """A precise, user-visible gap in N.I.A. route identity/evidence."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class NiaOuterAdvisorIssue:
    code: str
    detail: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NiaOuterLessonProfile:
    """One explicit N.I.A. phase/tier outer-lesson estimate."""

    produce_id: str
    phase: int
    is_sp: bool
    hp_cost: int
    nominal_gain: int
    source: str


@dataclass(frozen=True, slots=True)
class NiaOuterPolicy:
    """Caller-owned fallback policy; empty by default, so no values are guessed."""

    name: str = "strict-master-only"
    lesson_profiles: tuple[NiaOuterLessonProfile, ...] = ()
    non_lesson_priority: tuple[str, ...] = ()
    reserve_lesson_count: int = 1
    allow_unprojected_lesson_results: bool = False
    unprojected_lesson_min_stamina_permille: int = 400
    allow_shop_exit: bool = False


# Production live routing follows Maa's established N.I.A. preference order
# when the current screen exposes more than one non-lesson action.  Lesson
# values remain Master/PlayLog-derived; this policy supplies no guessed lesson
# profile and therefore does not widen N.I.A. Master beyond available facts.
DEFAULT_NIA_LIVE_POLICY = NiaOuterPolicy(
    name="maa-visible-actions-v2-opportunity-aware",
    non_lesson_priority=(
        BUSINESS,
        ACTIVITY,
        OUTING,
        SPECIAL_GUIDANCE,
        CARE_PACKAGE,
        CLASS,
        CONSULTATION,
    ),
    reserve_lesson_count=0,
    allow_unprojected_lesson_results=True,
    unprojected_lesson_min_stamina_permille=250,
    allow_shop_exit=True,
)


@dataclass(frozen=True, slots=True)
class NiaOuterModeRules:
    produce_id: str
    difficulty: str
    total_steps: int
    attribute_cap: int
    rest_recovery_permille: int
    produce_setting_id: str


@dataclass(frozen=True, slots=True)
class NiaOuterRouteContext:
    """Verified mode/week boundary used by the decision layer."""

    produce_id: str
    difficulty: str
    total_steps: int
    week: int
    stage: str
    phase: int | None
    next_audition_stage: str
    next_audition_week: int
    attribute_cap: int
    rest_recovery_permille: int
    produce_setting_id: str
    audition_boundaries: tuple[tuple[str, int], ...]
    scheduled_step_types: tuple[str, ...]
    route_source: str


@dataclass(frozen=True, slots=True)
class NiaOuterAdvice:
    """Stable click-free result for one N.I.A. outer decision."""

    status: str
    action: str
    reason: str
    issues: tuple[NiaOuterAdvisorIssue, ...]
    produce_id: str | None
    difficulty: str | None
    total_steps: int | None
    week: int | None
    stage: str | None
    next_audition_stage: str | None
    next_audition_week: int | None
    visible_actions: tuple[str, ...]
    eligible_actions: tuple[str, ...]
    predicted_hp: int | None
    attribute: str | None
    current_attribute: int | None
    predicted_attribute: int | None
    attribute_cap: int | None
    nominal_attribute_gain: int | None
    effective_attribute_gain: int | None
    overflow_attribute_gain: int | None
    is_sp: bool | None
    estimated_lesson_hp_cost: int | None
    reserved_hp_for_next_lesson: int | None
    gain_estimate_source: str | None
    route_source: str | None
    base_lesson_hp_cost: int | None = None
    lesson_item_stamina_cost: int = 0
    lesson_item_effect_ids: tuple[str, ...] = ()
    outing_suggestion_id: str | None = None
    outing_recovery_permille: int | None = None
    outing_tradeoffs: tuple[str, ...] = ()
    two_week_comparison_complete: bool = False
    forced_rest_next_week: bool | None = None
    forced_rest_penalty: int = 0
    schema: str = SCHEMA_NAME

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _LiveOption:
    action: str
    is_sp: bool


@dataclass(frozen=True, slots=True)
class _LessonEstimate:
    hp_cost: int
    nominal_gain: int
    source: str
    base_hp_cost: int | None = None
    item_stamina_cost: int = 0
    item_effect_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _LessonEvaluation:
    option: _LiveOption
    attribute: str
    field: str
    current: int
    estimate: _LessonEstimate
    effective_gain: int
    overflow_gain: int
    predicted_attribute: int
    predicted_hp: int
    reserve: int

    @property
    def safe(self) -> bool:
        return self.predicted_hp >= self.reserve


class _DecisionGap(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    return value


def _optional_mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@lru_cache(maxsize=16)
def _master_rows(path: Path) -> tuple[Mapping[str, Any], ...]:
    if not path.is_file():
        raise FileNotFoundError(f"Master file not found: {path}")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list):
        raise ValueError(f"{path.name} must contain a list")
    return tuple(row for row in payload if isinstance(row, Mapping))


def _one_row(
    rows: Sequence[Mapping[str, Any]],
    *,
    label: str,
    predicate: Callable[[Mapping[str, Any]], bool],
) -> Mapping[str, Any]:
    matches = [row for row in rows if predicate(row)]
    if len(matches) != 1:
        raise NiaOuterRouteAdapterError(
            "master-row-unavailable",
            f"{label} must resolve exactly once; found {len(matches)}",
        )
    return matches[0]


def _required_int(
    row: Mapping[str, Any], key: str, *, minimum: int = 0
) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise NiaOuterRouteAdapterError(
            "master-field-invalid", f"{key} must be an integer >= {minimum}"
        )
    return value


@lru_cache(maxsize=8)
def _load_mode_rules_cached(
    produce_id: str, master_dir: Path
) -> NiaOuterModeRules:
    if produce_id not in NIA_PRODUCE_IDS:
        raise NiaOuterRouteAdapterError(
            "nia-produce-id-invalid",
            f"expected produce-004 or produce-005, received {produce_id!r}",
        )
    produce = _one_row(
        _master_rows(master_dir / "Produce.yaml"),
        label=f"Produce[{produce_id}]",
        predicate=lambda row: row.get("id") == produce_id,
    )
    setting_id = produce.get("produceSettingId")
    if not isinstance(setting_id, str) or not setting_id:
        raise NiaOuterRouteAdapterError(
            "master-field-invalid", "Produce.produceSettingId is unavailable"
        )
    setting = _one_row(
        _master_rows(master_dir / "ProduceSetting.yaml"),
        label=f"ProduceSetting[{setting_id}]",
        predicate=lambda row: row.get("id") == setting_id,
    )
    rules = NiaOuterModeRules(
        produce_id=produce_id,
        difficulty=NIA_PRODUCE_IDS[produce_id],
        total_steps=_required_int(produce, "steps", minimum=1),
        attribute_cap=_required_int(
            produce, "idolCardParameterGrowthLimit", minimum=1
        ),
        rest_recovery_permille=_required_int(
            setting, "refreshStaminaRecoveryPermil"
        ),
        produce_setting_id=setting_id,
    )
    if not 0 <= rules.rest_recovery_permille <= 1000:
        raise NiaOuterRouteAdapterError(
            "master-field-invalid",
            "ProduceSetting.refreshStaminaRecoveryPermil must be 0..1000",
        )
    calendar = load_route_calendar(produce_id, master_dir=master_dir)
    if calendar.total_weeks != rules.total_steps:
        raise NiaOuterRouteAdapterError(
            "route-master-mismatch",
            f"route has {calendar.total_weeks} weeks but Master has "
            f"{rules.total_steps}",
        )
    stage_shape = tuple(
        milestone.step_type for milestone in calendar.milestones
    )
    if stage_shape != (MID1, MID2, FINAL):
        raise NiaOuterRouteAdapterError(
            "nia-route-stage-shape-invalid",
            f"expected Mid1/Mid2/Final, received {stage_shape}",
        )
    return rules


def load_nia_outer_mode_rules(
    produce_id: str, *, master_dir: Path = DEFAULT_MASTER_DIR
) -> NiaOuterModeRules:
    """Load N.I.A.-specific caps, total steps, and Rest recovery from Master."""

    if not isinstance(produce_id, str) or not produce_id:
        raise TypeError("produce_id must be non-empty text")
    try:
        return _load_mode_rules_cached(produce_id, Path(master_dir).resolve())
    except NiaOuterRouteAdapterError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise NiaOuterRouteAdapterError(
            "nia-master-mode-unavailable",
            f"N.I.A. mode Master data is unavailable: "
            f"{type(error).__name__}: {error}",
        ) from error


def _phase(calendar: RouteCalendar, week: int) -> int | None:
    mid1, mid2, final = calendar.milestones
    if week in {mid1.week, mid2.week, final.week}:
        return None
    if week < mid1.week:
        return 1
    if week < mid2.week:
        return 2
    if week < final.week:
        return 3
    return None


def _stage(calendar: RouteCalendar, week: int) -> str:
    by_week = {
        milestone.week: _MASTER_STAGE_TO_PUBLIC[milestone.step_type]
        for milestone in calendar.milestones
    }
    if week in by_week:
        return by_week[week]
    mid1, mid2, _final = calendar.milestones
    if week < mid1.week:
        return STAGE_BEFORE_MID1
    if week < mid2.week:
        return STAGE_BETWEEN_MID1_MID2
    return STAGE_BETWEEN_MID2_FINAL


def _schedule_identity(
    records: Sequence[Mapping[str, object]], master_dir: Path
) -> str:
    numbers: list[int] = []
    for index, row in enumerate(records):
        if not isinstance(row, Mapping):
            raise NiaOuterRouteAdapterError(
                "runtime-schedule-invalid",
                f"schedule[{index}] must be an object",
            )
        number = row.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            raise NiaOuterRouteAdapterError(
                "runtime-schedule-invalid",
                f"schedule[{index}].number must be an integer",
            )
        numbers.append(number)
    ordered = tuple(sorted(numbers))
    if ordered != tuple(range(1, len(records) + 1)):
        raise NiaOuterRouteAdapterError(
            "runtime-schedule-incomplete",
            f"schedule numbers must be exactly 1..{len(records)}; received {ordered}",
        )
    matches = tuple(
        produce_id
        for produce_id in NIA_PRODUCE_IDS
        if load_nia_outer_mode_rules(
            produce_id, master_dir=master_dir
        ).total_steps
        == len(records)
    )
    if len(matches) != 1:
        raise NiaOuterRouteAdapterError(
            "runtime-schedule-mode-ambiguous",
            f"{len(records)} schedule records match {matches}",
        )
    return matches[0]


def _composition_matches_snapshot(
    composition: NiaOuterExamSaveComposition,
    snapshot: ProduceOuterLocalSaveSnapshot,
) -> bool:
    outer = composition.outer_snapshot
    return (
        outer.log_count,
        outer.latest_week_marker,
        outer.last_log_cell_type,
        outer.produce_type,
        outer.stamina,
        outer.max_stamina,
        outer.vocal,
        outer.dance,
        outer.visual,
        outer.completed_steps,
        outer.audition_select_events,
    ) == (
        snapshot.log_count,
        snapshot.latest_week_marker,
        snapshot.last_log_cell_type,
        snapshot.produce_type,
        snapshot.stamina,
        snapshot.max_stamina,
        snapshot.vocal,
        snapshot.dance,
        snapshot.visual,
        snapshot.completed_steps,
        snapshot.audition_select_events,
    )


def adapt_nia_outer_route(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    overview_output: Mapping[str, object] | None = None,
    produce_id: str | None = None,
    runtime_schedule: Sequence[Mapping[str, object]] | None = None,
    exam_composition: NiaOuterExamSaveComposition | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaOuterRouteContext:
    """Resolve a N.I.A. Pro/Master route without guessing from broad type 2."""

    if not isinstance(snapshot, ProduceOuterLocalSaveSnapshot):
        raise TypeError("snapshot must be ProduceOuterLocalSaveSnapshot")
    if (
        snapshot.produce_type != 2
        or snapshot.produce_type_name != "next_idol_audition"
    ):
        raise NiaOuterRouteAdapterError(
            "not-nia-outer",
            "ProducePlayLog does not identify the N.I.A. produce family",
        )
    if overview_output is not None:
        overview_output = _mapping(overview_output, "overview_output")
    directory = Path(master_dir).resolve()
    evidence: dict[str, str] = {}
    if produce_id is not None:
        if not isinstance(produce_id, str) or not produce_id:
            raise TypeError("produce_id must be non-empty text when supplied")
        evidence["caller"] = produce_id
    route = (
        None
        if overview_output is None
        else _optional_mapping(overview_output.get("route"))
    )
    if route is not None and "produce_id" in route:
        route_produce_id = route.get("produce_id")
        if not isinstance(route_produce_id, str) or not route_produce_id:
            raise NiaOuterRouteAdapterError(
                "overview-route-invalid", "overview route produce_id is invalid"
            )
        evidence["overview-route"] = route_produce_id
    if exam_composition is not None:
        if not isinstance(exam_composition, NiaOuterExamSaveComposition):
            raise TypeError("exam_composition must be NiaOuterExamSaveComposition")
        if not _composition_matches_snapshot(exam_composition, snapshot):
            raise NiaOuterRouteAdapterError(
                "exam-composition-stale",
                "N.I.A. exam composition belongs to a different outer snapshot",
            )
        evidence["outer-exam-composition"] = (
            exam_composition.difficulty_key.produce_id
        )
    if runtime_schedule is not None:
        if not isinstance(runtime_schedule, Sequence) or isinstance(
            runtime_schedule, (str, bytes, bytearray)
        ):
            raise TypeError("runtime_schedule must be a sequence")
        evidence["runtime-schedule"] = _schedule_identity(
            runtime_schedule, directory
        )
    if not evidence:
        raise NiaOuterRouteAdapterError(
            "nia-produce-id-unavailable",
            "ProducePlayLog type 2 identifies only the N.I.A. family; provide "
            "overview.route.produce_id, explicit produce_id, a complete runtime "
            "ProduceSchedule, or a matching NiaOuterExamSaveComposition",
        )
    invalid = tuple(
        f"{source}={value}"
        for source, value in evidence.items()
        if value not in NIA_PRODUCE_IDS
    )
    if invalid:
        raise NiaOuterRouteAdapterError(
            "nia-produce-id-invalid", "; ".join(invalid)
        )
    identities = frozenset(evidence.values())
    if len(identities) != 1:
        raise NiaOuterRouteAdapterError(
            "nia-produce-id-conflict",
            "N.I.A. mode evidence conflicts: "
            + ", ".join(f"{key}={value}" for key, value in evidence.items()),
        )
    resolved_id = next(iter(identities))
    rules = load_nia_outer_mode_rules(resolved_id, master_dir=directory)
    try:
        calendar = load_route_calendar(resolved_id, master_dir=directory)
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise NiaOuterRouteAdapterError(
            "nia-route-calendar-unavailable",
            f"N.I.A. route calendar is unavailable: "
            f"{type(error).__name__}: {error}",
        ) from error

    latest_week = snapshot.latest_week_marker
    if latest_week is None:
        raise NiaOuterRouteAdapterError(
            "outer-week-unavailable",
            "ProducePlayLog has no authoritative week marker",
        )
    completed_week = snapshot.last_completed_week
    if completed_week is not None and completed_week > latest_week:
        raise NiaOuterRouteAdapterError(
            "outer-week-invalid",
            f"completed week {completed_week} is ahead of latest week {latest_week}",
        )
    # An exact outer+ExamSave composition proves that the latest step is the
    # currently running audition.  Its PlayLog row can already look
    # "completed" while the ExamSave is still active, so advancing here would
    # mislabel Mid1 as the following week.  Without an active ExamSave we keep
    # the normal between-week rule.
    week = (
        latest_week
        if exam_composition is not None
        else latest_week + 1 if completed_week == latest_week else latest_week
    )
    if not 1 <= week <= rules.total_steps:
        raise NiaOuterRouteAdapterError(
            "outer-week-invalid",
            f"ProducePlayLog week {week} is outside 1..{rules.total_steps}",
        )
    if route is not None:
        route_week = route.get("current_week")
        if route_week is not None:
            if _integer(route_week) is None:
                raise NiaOuterRouteAdapterError(
                    "overview-route-invalid", "overview current_week is invalid"
                )
            if route_week != week:
                raise NiaOuterRouteAdapterError(
                    "outer-overview-week-conflict",
                    f"ProducePlayLog week {week} != overview week {route_week}",
                )
        route_total = route.get("total_weeks")
        if route_total is not None and route_total != rules.total_steps:
            raise NiaOuterRouteAdapterError(
                "overview-route-total-conflict",
                f"overview total {route_total} != Master {rules.total_steps}",
            )

    scheduled: tuple[str, ...] = ()
    if runtime_schedule is not None:
        try:
            adapted = adapt_nia_runtime_schedule(
                runtime_schedule, expected_steps=rules.total_steps
            )
        except (TypeError, ValueError) as error:
            raise NiaOuterRouteAdapterError(
                "runtime-schedule-invalid",
                f"{type(error).__name__}: {error}",
            ) from error
        if adapted.diagnostics:
            diagnostic = adapted.diagnostics[0]
            raise NiaOuterRouteAdapterError(
                diagnostic.code, diagnostic.detail
            )
        expected_auditions = tuple(
            (milestone.step_type, milestone.week)
            for milestone in calendar.milestones
        )
        if adapted.audition_weeks != expected_auditions:
            raise NiaOuterRouteAdapterError(
                "runtime-schedule-audition-boundary-conflict",
                f"runtime {adapted.audition_weeks} != route {expected_auditions}",
            )
        current = adapted.steps[week - 1]
        scheduled = current.all_step_types

    next_milestone = calendar.next_milestone(week)
    if next_milestone is None:
        raise NiaOuterRouteAdapterError(
            "route-milestone-unavailable",
            f"no audition milestone exists at or after week {week}",
        )
    return NiaOuterRouteContext(
        produce_id=resolved_id,
        difficulty=rules.difficulty,
        total_steps=rules.total_steps,
        week=week,
        stage=_stage(calendar, week),
        phase=_phase(calendar, week),
        next_audition_stage=_MASTER_STAGE_TO_PUBLIC[next_milestone.step_type],
        next_audition_week=next_milestone.week,
        attribute_cap=rules.attribute_cap,
        rest_recovery_permille=rules.rest_recovery_permille,
        produce_setting_id=rules.produce_setting_id,
        audition_boundaries=tuple(
            (
                _MASTER_STAGE_TO_PUBLIC[milestone.step_type],
                milestone.week,
            )
            for milestone in calendar.milestones
        ),
        scheduled_step_types=scheduled,
        route_source="+".join((*evidence.keys(), "master-route")),
    )


def nia_outer_policy_from_mapping(data: Mapping[str, object]) -> NiaOuterPolicy:
    """Parse an explicit policy fixture without treating it as Master data."""

    root = _mapping(data, "policy")
    schema = root.get("schema", POLICY_SCHEMA_NAME)
    if schema != POLICY_SCHEMA_NAME:
        raise ValueError(f"unsupported N.I.A. outer policy schema: {schema!r}")
    name = root.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("policy.name must be non-empty text")
    reserve = root.get("reserve_lesson_count", 1)
    if isinstance(reserve, bool) or not isinstance(reserve, int) or reserve < 0:
        raise ValueError("reserve_lesson_count must be a non-negative integer")
    allow_unprojected = root.get("allow_unprojected_lesson_results", False)
    if type(allow_unprojected) is not bool:
        raise ValueError("allow_unprojected_lesson_results must be boolean")
    minimum_stamina = root.get(
        "unprojected_lesson_min_stamina_permille", 400
    )
    if (
        isinstance(minimum_stamina, bool)
        or not isinstance(minimum_stamina, int)
        or not 0 <= minimum_stamina <= 1000
    ):
        raise ValueError(
            "unprojected_lesson_min_stamina_permille must be 0..1000"
        )
    allow_shop_exit = root.get("allow_shop_exit", False)
    if type(allow_shop_exit) is not bool:
        raise ValueError("allow_shop_exit must be boolean")
    priority_raw = root.get("non_lesson_priority", [])
    allowed_priorities = set(_KNOWN_NON_LESSON_ACTIONS)
    if allow_shop_exit:
        allowed_priorities.update(_SHOP_ACTIONS)
    if not isinstance(priority_raw, list) or not all(
        isinstance(value, str) and value in allowed_priorities
        for value in priority_raw
    ):
        raise ValueError("non_lesson_priority contains an unknown action")
    raw_profiles = root.get("lesson_profiles", [])
    if not isinstance(raw_profiles, list):
        raise ValueError("lesson_profiles must be a list")
    profiles: list[NiaOuterLessonProfile] = []
    for index, raw in enumerate(raw_profiles):
        row = _mapping(raw, f"lesson_profiles[{index}]")
        profile_produce_id = row.get("produce_id")
        phase = row.get("phase")
        tier = row.get("tier")
        hp_cost = row.get("hp_cost")
        gain = row.get("nominal_gain")
        source = row.get("source", f"policy:{name}")
        if profile_produce_id not in NIA_PRODUCE_IDS:
            raise ValueError(f"lesson_profiles[{index}].produce_id is invalid")
        if phase not in {1, 2, 3}:
            raise ValueError(f"lesson_profiles[{index}].phase must be 1..3")
        if tier not in {"normal", "sp"}:
            raise ValueError(f"lesson_profiles[{index}].tier must be normal/sp")
        if (
            isinstance(hp_cost, bool)
            or not isinstance(hp_cost, int)
            or hp_cost < 0
        ):
            raise ValueError(f"lesson_profiles[{index}].hp_cost is invalid")
        if isinstance(gain, bool) or not isinstance(gain, int) or gain < 0:
            raise ValueError(f"lesson_profiles[{index}].nominal_gain is invalid")
        if not isinstance(source, str) or not source:
            raise ValueError(f"lesson_profiles[{index}].source is invalid")
        profiles.append(
            NiaOuterLessonProfile(
                produce_id=profile_produce_id,
                phase=phase,
                is_sp=tier == "sp",
                hp_cost=hp_cost,
                nominal_gain=gain,
                source=source,
            )
        )
    policy = NiaOuterPolicy(
        name=name,
        lesson_profiles=tuple(profiles),
        non_lesson_priority=tuple(priority_raw),
        reserve_lesson_count=reserve,
        allow_unprojected_lesson_results=allow_unprojected,
        unprojected_lesson_min_stamina_permille=minimum_stamina,
        allow_shop_exit=allow_shop_exit,
    )
    _validate_policy(policy)
    return policy


def load_nia_outer_policy(path: str | Path) -> NiaOuterPolicy:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return nia_outer_policy_from_mapping(_mapping(payload, "policy file"))


def _validate_policy(policy: NiaOuterPolicy) -> None:
    if not isinstance(policy, NiaOuterPolicy):
        raise TypeError("policy must be NiaOuterPolicy")
    if not isinstance(policy.name, str) or not policy.name:
        raise ValueError("policy.name must be non-empty text")
    if (
        isinstance(policy.reserve_lesson_count, bool)
        or not isinstance(policy.reserve_lesson_count, int)
        or policy.reserve_lesson_count < 0
    ):
        raise ValueError("reserve_lesson_count must be non-negative")
    if type(policy.allow_unprojected_lesson_results) is not bool:
        raise TypeError("allow_unprojected_lesson_results must be boolean")
    if type(policy.allow_shop_exit) is not bool:
        raise TypeError("allow_shop_exit must be boolean")
    if (
        isinstance(policy.unprojected_lesson_min_stamina_permille, bool)
        or not isinstance(policy.unprojected_lesson_min_stamina_permille, int)
        or not 0 <= policy.unprojected_lesson_min_stamina_permille <= 1000
    ):
        raise ValueError(
            "unprojected_lesson_min_stamina_permille must be 0..1000"
        )
    keys: set[tuple[str, int, bool]] = set()
    for profile in policy.lesson_profiles:
        if not isinstance(profile, NiaOuterLessonProfile):
            raise TypeError("lesson_profiles must contain NiaOuterLessonProfile")
        key = (profile.produce_id, profile.phase, profile.is_sp)
        if key in keys:
            raise ValueError(f"duplicate lesson profile: {key}")
        keys.add(key)
        if profile.produce_id not in NIA_PRODUCE_IDS:
            raise ValueError(f"invalid profile produce_id: {profile.produce_id}")
        if (
            isinstance(profile.phase, bool)
            or not isinstance(profile.phase, int)
            or profile.phase not in {1, 2, 3}
        ):
            raise ValueError("profile phase must be 1..3")
        if (
            isinstance(profile.hp_cost, bool)
            or not isinstance(profile.hp_cost, int)
            or profile.hp_cost < 0
            or isinstance(profile.nominal_gain, bool)
            or not isinstance(profile.nominal_gain, int)
            or profile.nominal_gain < 0
        ):
            raise ValueError("profile cost/gain must be non-negative")
        if not isinstance(profile.source, str) or not profile.source:
            raise ValueError("profile source must be non-empty text")
    if len(policy.non_lesson_priority) != len(set(policy.non_lesson_priority)):
        raise ValueError("non_lesson_priority must not contain duplicates")
    allowed_priorities = set(_KNOWN_NON_LESSON_ACTIONS)
    if policy.allow_shop_exit:
        allowed_priorities.update(_SHOP_ACTIONS)
    if any(
        action not in allowed_priorities or action == REST
        for action in policy.non_lesson_priority
    ):
        raise ValueError(
            "non_lesson_priority must contain known non-Rest actions"
        )


@lru_cache(maxsize=8)
def _master_lesson_profiles(
    produce_id: str, master_dir: Path
) -> tuple[NiaOuterLessonProfile, ...]:
    token = produce_id.replace("-", "_")
    result: list[NiaOuterLessonProfile] = []
    for row in _master_rows(master_dir / "ProduceStepSelfLesson.yaml"):
        row_id = row.get("id")
        if not isinstance(row_id, str):
            continue
        match = _SELF_LESSON_ID.fullmatch(row_id)
        if match is None or match.group(1) != token:
            continue
        hp_cost = _required_int(row, "stamina")
        gain = _required_int(row, "parameter")
        result.append(
            NiaOuterLessonProfile(
                produce_id=produce_id,
                phase=int(match.group(2)),
                is_sp=match.group(3) == "sp",
                hp_cost=hp_cost,
                nominal_gain=gain,
                source=f"master:ProduceStepSelfLesson.yaml#{row_id}",
            )
        )
    return tuple(result)


def _profile_catalog(
    route: NiaOuterRouteContext,
    policy: NiaOuterPolicy,
    master_dir: Path,
) -> Mapping[tuple[int, bool], NiaOuterLessonProfile]:
    catalog = {
        (profile.phase, profile.is_sp): profile
        for profile in _master_lesson_profiles(route.produce_id, master_dir)
    }
    for profile in policy.lesson_profiles:
        if profile.produce_id == route.produce_id:
            catalog[(profile.phase, profile.is_sp)] = profile
    return catalog


def _overview_options(
    overview_output: Mapping[str, Any] | None,
) -> tuple[_LiveOption, ...]:
    if overview_output is None:
        return ()
    raw_options = overview_output.get("options")
    if raw_options is None:
        return ()
    if not isinstance(raw_options, (list, tuple)):
        raise _DecisionGap(
            "overview-options-invalid", "overview options must be a list"
        )
    result: list[_LiveOption] = []
    for index, raw in enumerate(raw_options):
        option = _optional_mapping(raw)
        if option is None:
            raise _DecisionGap(
                "overview-options-invalid",
                f"overview options[{index}] must be an object",
            )
        action = option.get("action")
        if not isinstance(action, str) or not action:
            raise _DecisionGap(
                "overview-options-invalid",
                f"overview options[{index}].action is invalid",
            )
        result.append(_LiveOption(action, option.get("is_sp") is True))
    return tuple(result)


def _training_options(
    training_output: Mapping[str, Any] | None,
    overview_by_action: Mapping[str, _LiveOption],
) -> tuple[_LiveOption, ...]:
    if training_output is None:
        return ()
    state = _optional_mapping(training_output.get("state"))
    if "state" in training_output and state is None:
        raise _DecisionGap(
            "training-options-invalid", "training state must be an object"
        )
    raw_options = None if state is None else state.get("options")
    if raw_options is None:
        return ()
    if not isinstance(raw_options, (list, tuple)):
        raise _DecisionGap(
            "training-options-invalid", "training options must be a list"
        )
    result: list[_LiveOption] = []
    for index, raw in enumerate(raw_options):
        option = _optional_mapping(raw)
        if option is None:
            raise _DecisionGap(
                "training-options-invalid",
                f"training options[{index}] must be an object",
            )
        attribute = option.get("attribute")
        if not isinstance(attribute, str):
            raise _DecisionGap(
                "training-options-invalid",
                f"training options[{index}].attribute is invalid",
            )
        action = _ATTRIBUTE_TO_ACTION.get(attribute)
        if action is None:
            raise _DecisionGap(
                "training-options-invalid",
                f"training options[{index}] has unknown attribute {attribute!r}",
            )
        overview = overview_by_action.get(action) or overview_by_action.get(
            SELF_LESSON
        )
        result.append(
            _LiveOption(
                action,
                overview.is_sp
                if overview is not None
                else option.get("is_sp") is True,
            )
        )
    return tuple(result)


def _available_options(
    overview_output: Mapping[str, Any] | None,
    training_output: Mapping[str, Any] | None,
    *,
    include_shops: bool = False,
) -> tuple[
    tuple[_LiveOption, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    overview = _overview_options(overview_output)
    by_action = {option.action: option for option in overview}
    combined = [*overview, *_training_options(training_output, by_action)]
    visible = tuple(dict.fromkeys(option.action for option in combined))
    unknown = tuple(action for action in visible if action not in _KNOWN_ACTIONS)
    has_specific_lesson = any(
        option.action in _LESSON_ACTION_TO_ATTRIBUTE for option in combined
    )
    deduplicated: dict[str, _LiveOption] = {}
    for option in combined:
        if option.action in _SHOP_ACTIONS and not include_shops:
            continue
        if option.action == SELF_LESSON:
            continue
        if option.action not in _KNOWN_ACTIONS:
            continue
        prior = deduplicated.get(option.action)
        deduplicated[option.action] = _LiveOption(
            option.action,
            option.is_sp or (prior.is_sp if prior is not None else False),
        )
    missing_container = (
        (SELF_LESSON,)
        if SELF_LESSON in visible and not has_specific_lesson
        else ()
    )
    return tuple(deduplicated.values()), visible, unknown, missing_container


def _overview_state(
    overview_output: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if overview_output is None:
        return {}
    return _optional_mapping(overview_output.get("state")) or {}


def _state_value(
    snapshot: ProduceOuterLocalSaveSnapshot,
    overview_state: Mapping[str, Any],
    field: str,
) -> int | None:
    local = _integer(getattr(snapshot, field))
    live = _integer(overview_state.get(field))
    # ProducePlayLog is the runtime-state authority.  Overview OCR identifies
    # the page and actions, but its displayed total may include presentation
    # bonuses and must not veto an otherwise valid persisted state.
    return local if local is not None else live


def _maximum_hp(
    snapshot: ProduceOuterLocalSaveSnapshot,
    overview_state: Mapping[str, Any],
    current_hp: int | None,
) -> int | None:
    local = _integer(snapshot.max_stamina)
    live = _integer(overview_state.get("max_stamina"))
    for value in (local, live):
        if value is not None and value > 0:
            return value
    candidates: list[int] = []
    for step in snapshot.completed_steps:
        for line in step.lines:
            if line.line_type_name in {"stamina", "max_stamina"}:
                candidates.extend(
                    int(value)
                    for value in (line.before, line.after)
                    if value > 0 and float(value).is_integer()
                )
    if current_hp is not None and current_hp > 0:
        candidates.append(current_hp)
    return max(candidates) if candidates else None


def _preferred_attribute(
    training_output: Mapping[str, Any] | None,
) -> str | None:
    if training_output is None:
        return None
    state = _optional_mapping(training_output.get("state"))
    attribute = None if state is None else state.get("recommended_attribute")
    return attribute if attribute in _ATTRIBUTE_TO_ACTION else None


def _step_phase(route: NiaOuterRouteContext, week: int | None) -> int | None:
    if week is None:
        return None
    boundaries = tuple(value for _stage_name, value in route.audition_boundaries)
    if len(boundaries) != 3:
        return None
    if week in boundaries:
        return None
    if week < boundaries[0]:
        return 1
    if week < boundaries[1]:
        return 2
    if week < boundaries[2]:
        return 3
    return None


def _historical_estimate(
    snapshot: ProduceOuterLocalSaveSnapshot,
    route: NiaOuterRouteContext,
    *,
    field: str,
    is_sp: bool,
) -> _LessonEstimate | None:
    if route.phase is None:
        return None
    suffix = "_sp" if is_sp else "_normal"
    marker = f"_{field}_"
    candidates: list[tuple[bool, int, int, int, str]] = []
    for step in snapshot.completed_steps:
        if (
            not step.step_type_name.startswith("self_lesson_")
            or not step.step_type_name.endswith(suffix)
            or _step_phase(route, step.week_marker) != route.phase
        ):
            continue
        step_field = next(
            (
                candidate
                for candidate in ("vocal", "dance", "visual")
                if f"_{candidate}_" in step.step_type_name
            ),
            None,
        )
        if step_field is None:
            continue
        gains = [
            line.delta
            for line in step.lines
            if not line.is_triggered
            and line.line_type_name == step_field
            and line.delta > 0
        ]
        costs = [
            -line.delta
            for line in step.lines
            if not line.is_triggered
            and line.line_type_name == "stamina"
            and line.delta < 0
        ]
        if not gains or not costs:
            continue
        gain = int(round(sum(gains)))
        cost = int(round(sum(costs)))
        candidates.append(
            (marker in step.step_type_name, step.log_index, gain, cost, step.step_type_name)
        )
    if not candidates:
        return None
    exact = tuple(item for item in candidates if item[0])
    _same_attribute, log_index, gain, cost, name = max(
        exact or tuple(candidates), key=lambda item: item[1]
    )
    return _LessonEstimate(
        hp_cost=cost,
        nominal_gain=gain,
        source=f"outer-save:{name}@log{log_index}",
    )


def _estimate(
    snapshot: ProduceOuterLocalSaveSnapshot,
    route: NiaOuterRouteContext,
    catalog: Mapping[tuple[int, bool], NiaOuterLessonProfile],
    *,
    field: str,
    is_sp: bool,
) -> _LessonEstimate | None:
    historical = _historical_estimate(
        snapshot, route, field=field, is_sp=is_sp
    )
    if historical is not None:
        return historical
    if route.phase is None:
        return None
    profile = catalog.get((route.phase, is_sp))
    if profile is None:
        return None
    return _LessonEstimate(
        hp_cost=profile.hp_cost,
        nominal_gain=profile.nominal_gain,
        source=profile.source,
    )


def _apply_owned_item_stamina(
    snapshot: ProduceOuterLocalSaveSnapshot,
    estimate: _LessonEstimate,
    *,
    action: str,
    is_sp: bool,
    master_dir: Path,
) -> _LessonEstimate | NiaOuterAdvisorIssue:
    """Join an outer lesson estimate to exact currently-owned item effects."""

    projection = project_nia_lesson_stamina_cost(
        snapshot,
        base_cost=estimate.hp_cost,
        lesson_action=action,
        is_sp=is_sp,
        master_dir=master_dir,
    )
    if not projection.ready or projection.effective_cost is None:
        issue = projection.issues[0]
        return _issue(issue.code, issue.detail, *issue.evidence_ids)
    effect_ids = tuple(value.effect_id for value in projection.adjustments)
    source = estimate.source
    if effect_ids:
        source += "; owned-item:" + ",".join(effect_ids)
    return replace(
        estimate,
        hp_cost=projection.effective_cost,
        source=source,
        base_hp_cost=projection.base_cost,
        item_stamina_cost=projection.item_cost_delta,
        item_effect_ids=effect_ids,
    )


def _exact_outing_tradeoffs(effect: NiaOutingOptionEffect) -> tuple[str, ...]:
    """Keep every resource consequence visible without assigning a guessed value."""

    values: list[str] = []
    if effect.produce_point_cost is not None:
        values.append(f"produce-point-cost={effect.produce_point_cost}")
    if effect.added_card_ids:
        values.append("added-cards=" + ",".join(effect.added_card_ids))
    if effect.trouble_card_ids:
        values.append("trouble-cards=" + ",".join(effect.trouble_card_ids))
    if effect.drink_reward_count is not None:
        values.append(f"drink-rewards={effect.drink_reward_count}")
    if effect.card_upgrade_count is not None:
        values.append(f"card-upgrades={effect.card_upgrade_count}")
    if effect.card_delete_count is not None:
        values.append(f"card-deletes={effect.card_delete_count}")
    if effect.unmodeled_effect_ids:
        values.append("unvalued-effects=" + ",".join(effect.unmodeled_effect_ids))
    return tuple(values)


def _issue(code: str, detail: str, *evidence: str) -> NiaOuterAdvisorIssue:
    return NiaOuterAdvisorIssue(code, detail, tuple(evidence))


def _unavailable(
    issue: NiaOuterAdvisorIssue,
    *,
    route: NiaOuterRouteContext | None = None,
    visible_actions: tuple[str, ...] = (),
    eligible_actions: tuple[str, ...] = (),
    predicted_hp: int | None = None,
    lesson_cost: int | None = None,
    reserve: int | None = None,
) -> NiaOuterAdvice:
    return NiaOuterAdvice(
        status=STATUS_UNAVAILABLE,
        action=UNAVAILABLE_ACTION,
        reason=issue.detail,
        issues=(issue,),
        produce_id=None if route is None else route.produce_id,
        difficulty=None if route is None else route.difficulty,
        total_steps=None if route is None else route.total_steps,
        week=None if route is None else route.week,
        stage=None if route is None else route.stage,
        next_audition_stage=(
            None if route is None else route.next_audition_stage
        ),
        next_audition_week=None if route is None else route.next_audition_week,
        visible_actions=visible_actions,
        eligible_actions=eligible_actions,
        predicted_hp=predicted_hp,
        attribute=None,
        current_attribute=None,
        predicted_attribute=None,
        attribute_cap=None if route is None else route.attribute_cap,
        nominal_attribute_gain=None,
        effective_attribute_gain=None,
        overflow_attribute_gain=None,
        is_sp=None,
        estimated_lesson_hp_cost=lesson_cost,
        reserved_hp_for_next_lesson=reserve,
        gain_estimate_source=None,
        route_source=None if route is None else route.route_source,
    )


def _rest_advice(
    route: NiaOuterRouteContext,
    *,
    visible_actions: tuple[str, ...],
    eligible_actions: tuple[str, ...],
    current_hp: int,
    max_hp: int,
    lesson_cost: int,
    reserve: int,
    base_lesson_hp_cost: int | None = None,
    lesson_item_stamina_cost: int = 0,
    lesson_item_effect_ids: tuple[str, ...] = (),
) -> NiaOuterAdvice:
    recovery = max_hp * route.rest_recovery_permille // 1000
    predicted = min(max_hp, current_hp + recovery)
    return NiaOuterAdvice(
        status=STATUS_READY,
        action=REST,
        reason=(
            f"N.I.A. {route.difficulty} week {route.week}: a {lesson_cost} HP "
            f"lesson would leave {max(0, current_hp - lesson_cost)} HP, below "
            f"the {reserve} HP next-lesson reserve; Master "
            f"{route.rest_recovery_permille}‰ Rest recovery predicts "
            f"{current_hp}->{predicted} HP."
        ),
        issues=(),
        produce_id=route.produce_id,
        difficulty=route.difficulty,
        total_steps=route.total_steps,
        week=route.week,
        stage=route.stage,
        next_audition_stage=route.next_audition_stage,
        next_audition_week=route.next_audition_week,
        visible_actions=visible_actions,
        eligible_actions=eligible_actions,
        predicted_hp=predicted,
        attribute=None,
        current_attribute=None,
        predicted_attribute=None,
        attribute_cap=route.attribute_cap,
        nominal_attribute_gain=0,
        effective_attribute_gain=0,
        overflow_attribute_gain=0,
        is_sp=False,
        estimated_lesson_hp_cost=lesson_cost,
        reserved_hp_for_next_lesson=reserve,
        gain_estimate_source=(
            f"master:ProduceSetting.yaml#{route.produce_setting_id}:"
            "refreshStaminaRecoveryPermil"
        ),
        route_source=route.route_source,
        base_lesson_hp_cost=base_lesson_hp_cost,
        lesson_item_stamina_cost=lesson_item_stamina_cost,
        lesson_item_effect_ids=lesson_item_effect_ids,
    )


def _outing_recovery_advice(
    route: NiaOuterRouteContext,
    *,
    visible_actions: tuple[str, ...],
    eligible_actions: tuple[str, ...],
    current_hp: int,
    max_hp: int,
    required_hp: int,
    lesson_cost: int | None,
    reserve: int,
    suggestion_id: str | None,
    master_dir: Path,
) -> NiaOuterAdvice | None:
    """Use one exact outing row; never substitute Rest's 700-permille rate."""

    if OUTING not in eligible_actions:
        return None
    if suggestion_id is None:
        # The overview tile does not identify which 20/40/60% row will be
        # selected on the following page.  Without that product identity an
        # HP threshold cannot safely prefer Outing over exact Rest.
        return None
    effect = load_nia_outing_option_effect(
        suggestion_id,
        max_stamina=max_hp,
        master_dir=master_dir,
    )
    if not effect.ready or effect.stamina_recovery is None:
        issue = effect.issues[0]
        return _unavailable(
            _issue(issue.code, issue.detail, *issue.evidence_ids),
            route=route,
            visible_actions=visible_actions,
            eligible_actions=eligible_actions,
            predicted_hp=current_hp,
            lesson_cost=lesson_cost,
            reserve=reserve,
        )
    predicted = min(max_hp, current_hp + effect.stamina_recovery)
    if predicted < required_hp:
        return None
    tradeoffs = _exact_outing_tradeoffs(effect)
    return NiaOuterAdvice(
        status=STATUS_READY,
        action=OUTING,
        reason=(
            f"N.I.A. {route.difficulty} week {route.week}: exact outing "
            f"{suggestion_id} has {effect.stamina_recovery_permille}‰ recovery "
            f"({current_hp}->{predicted} HP), reaching the {required_hp} HP "
            f"requirement. Other consequences are retained, not flattened "
            f"into Rest: {'; '.join(tradeoffs) or 'none'}."
        ),
        issues=(),
        produce_id=route.produce_id,
        difficulty=route.difficulty,
        total_steps=route.total_steps,
        week=route.week,
        stage=route.stage,
        next_audition_stage=route.next_audition_stage,
        next_audition_week=route.next_audition_week,
        visible_actions=visible_actions,
        eligible_actions=eligible_actions,
        predicted_hp=predicted,
        attribute=None,
        current_attribute=None,
        predicted_attribute=None,
        attribute_cap=route.attribute_cap,
        nominal_attribute_gain=0,
        effective_attribute_gain=0,
        overflow_attribute_gain=0,
        is_sp=False,
        estimated_lesson_hp_cost=lesson_cost,
        reserved_hp_for_next_lesson=reserve,
        gain_estimate_source=(
            f"master:ProduceStepEventSuggestion.yaml#{suggestion_id}"
        ),
        route_source=route.route_source,
        outing_suggestion_id=suggestion_id,
        outing_recovery_permille=effect.stamina_recovery_permille,
        outing_tradeoffs=tradeoffs,
    )


def _lesson_advice(
    route: NiaOuterRouteContext,
    selected: _LessonEvaluation,
    evaluations: tuple[_LessonEvaluation, ...],
    *,
    visible_actions: tuple[str, ...],
    eligible_actions: tuple[str, ...],
) -> NiaOuterAdvice:
    tier = " SP" if selected.option.is_sp else " normal"
    clauses = [
        f"N.I.A. {route.difficulty} week {route.week} phase {route.phase}; "
        f"selected {selected.attribute}{tier}: {selected.effective_gain} of "
        f"{selected.estimate.nominal_gain} nominal gain is effective "
        f"({selected.current}->{selected.predicted_attribute}) under the "
        f"Master {route.attribute_cap} cap."
    ]
    avoided = sorted(
        (
            item
            for item in evaluations
            if item is not selected and item.overflow_gain > 0
        ),
        key=lambda item: (-item.overflow_gain, item.attribute),
    )


    if avoided:
        alternative = avoided[0]
        clauses.append(
            f"Avoids {alternative.attribute}, which would overflow "
            f"{alternative.overflow_gain}."
        )
    clauses.append(
        f"HP {selected.predicted_hp} after the exact/explicit "
        f"{selected.estimate.hp_cost} cost preserves {selected.reserve} HP."
    )
    return NiaOuterAdvice(
        status=STATUS_READY,
        action=selected.option.action,
        reason=" ".join(clauses),
        issues=(),
        produce_id=route.produce_id,
        difficulty=route.difficulty,
        total_steps=route.total_steps,
        week=route.week,
        stage=route.stage,
        next_audition_stage=route.next_audition_stage,
        next_audition_week=route.next_audition_week,
        visible_actions=visible_actions,
        eligible_actions=eligible_actions,
        predicted_hp=selected.predicted_hp,
        attribute=selected.attribute,
        current_attribute=selected.current,
        predicted_attribute=selected.predicted_attribute,
        attribute_cap=route.attribute_cap,
        nominal_attribute_gain=selected.estimate.nominal_gain,
        effective_attribute_gain=selected.effective_gain,
        overflow_attribute_gain=selected.overflow_gain,
        is_sp=selected.option.is_sp,
        estimated_lesson_hp_cost=selected.estimate.hp_cost,
        reserved_hp_for_next_lesson=selected.reserve,
        gain_estimate_source=selected.estimate.source,
        route_source=route.route_source,
        base_lesson_hp_cost=(
            selected.estimate.base_hp_cost
            if selected.estimate.base_hp_cost is not None
            else selected.estimate.hp_cost
        ),
        lesson_item_stamina_cost=selected.estimate.item_stamina_cost,
        lesson_item_effect_ids=selected.estimate.item_effect_ids,
    )


def _apply_two_week_recovery_choice(
    advice: NiaOuterAdvice,
    route: NiaOuterRouteContext,
    selected: _LessonEvaluation,
    *,
    context: NiaTwoWeekRecoveryContext | None,
    rest_visible: bool,
    current_hp: int,
    max_hp: int | None,
    visible_actions: tuple[str, ...],
    eligible_actions: tuple[str, ...],
) -> NiaOuterAdvice:
    """Move Rest one week earlier only from a complete two-week product set."""

    if context is None or not rest_visible or max_hp is None:
        return advice
    rest_recovery = max_hp * route.rest_recovery_permille // 1000
    comparison = compare_nia_two_week_recovery(
        current_action=selected.option.action,
        current_action_score=selected.effective_gain,
        current_post_hp=selected.predicted_hp,
        current_rest_post_hp=min(max_hp, current_hp + rest_recovery),
        context=context,
    )
    if not comparison.ready:
        # An incomplete or malformed candidate product must not silently
        # influence the one-week policy.  The caller can inspect the typed
        # comparison directly and retry after filling the missing candidates.
        return advice
    suffix = (
        f" Complete two-week comparison: forced-rest-next-week="
        f"{str(comparison.forced_rest).lower()}, explicit opportunity penalty="
        f"{comparison.forced_rest_penalty}; current route score="
        f"{comparison.current_then_next_score}, Rest-now route score="
        f"{comparison.rest_then_non_rest_score}."
    )
    metadata = {
        "two_week_comparison_complete": True,
        "forced_rest_next_week": comparison.forced_rest,
        "forced_rest_penalty": comparison.forced_rest_penalty,
    }
    if comparison.preferred_route != "rest-then-non-rest":
        return replace(advice, reason=advice.reason + suffix, **metadata)
    rest = _rest_advice(
        route,
        visible_actions=visible_actions,
        eligible_actions=eligible_actions,
        current_hp=current_hp,
        max_hp=max_hp,
        lesson_cost=selected.estimate.hp_cost,
        reserve=selected.reserve,
        base_lesson_hp_cost=selected.estimate.base_hp_cost,
        lesson_item_stamina_cost=selected.estimate.item_stamina_cost,
        lesson_item_effect_ids=selected.estimate.item_effect_ids,
    )
    return replace(
        rest,
        reason=(
            f"{rest.reason} Complete two-week evidence prefers Rest now, then "
            f"{comparison.next_action}, over {selected.option.action} now and "
            f"a forced Rest next week." + suffix
        ),
        **metadata,
    )


def _unprojected_live_lesson_advice(
    route: NiaOuterRouteContext,
    selected: _LiveOption,
    *,
    attribute: str,
    current_attribute: int,
    current_hp: int,
    visible_actions: tuple[str, ...],
    eligible_actions: tuple[str, ...],
) -> NiaOuterAdvice:
    tier = "SP" if selected.is_sp else "normal"
    return NiaOuterAdvice(
        status=STATUS_READY,
        action=selected.action,
        reason=(
            f"N.I.A. {route.difficulty} week {route.week}: selected "
            f"{attribute} {tier} by largest remaining headroom under the "
            f"{route.attribute_cap} cap. Lesson gain/HP result is deliberately "
            "not projected; the next Produce LocalSave is authoritative."
        ),
        issues=(),
        produce_id=route.produce_id,
        difficulty=route.difficulty,
        total_steps=route.total_steps,
        week=route.week,
        stage=route.stage,
        next_audition_stage=route.next_audition_stage,
        next_audition_week=route.next_audition_week,
        visible_actions=visible_actions,
        eligible_actions=eligible_actions,
        predicted_hp=None,
        attribute=attribute,
        current_attribute=current_attribute,
        predicted_attribute=None,
        attribute_cap=route.attribute_cap,
        nominal_attribute_gain=None,
        effective_attribute_gain=None,
        overflow_attribute_gain=None,
        is_sp=selected.is_sp,
        estimated_lesson_hp_cost=None,
        reserved_hp_for_next_lesson=None,
        gain_estimate_source="post-action-produce-local-save",
        route_source=route.route_source,
    )


def _unprojected_live_rest_advice(
    route: NiaOuterRouteContext,
    *,
    current_hp: int,
    max_hp: int,
    threshold_hp: int,
    visible_actions: tuple[str, ...],
    eligible_actions: tuple[str, ...],
) -> NiaOuterAdvice:
    recovery = max_hp * route.rest_recovery_permille // 1000
    predicted = min(max_hp, current_hp + recovery)
    return NiaOuterAdvice(
        status=STATUS_READY,
        action=REST,
        reason=(
            f"N.I.A. {route.difficulty} week {route.week}: current HP "
            f"{current_hp}/{max_hp} is below the live-policy threshold "
            f"{threshold_hp}; Rest is selected before another lesson. "
            "No lesson HP cost or result gain was projected."
        ),
        issues=(),
        produce_id=route.produce_id,
        difficulty=route.difficulty,
        total_steps=route.total_steps,
        week=route.week,
        stage=route.stage,
        next_audition_stage=route.next_audition_stage,
        next_audition_week=route.next_audition_week,
        visible_actions=visible_actions,
        eligible_actions=eligible_actions,
        predicted_hp=predicted,
        attribute=None,
        current_attribute=None,
        predicted_attribute=None,
        attribute_cap=route.attribute_cap,
        nominal_attribute_gain=None,
        effective_attribute_gain=None,
        overflow_attribute_gain=None,
        is_sp=False,
        estimated_lesson_hp_cost=None,
        reserved_hp_for_next_lesson=threshold_hp,
        gain_estimate_source="post-action-produce-local-save",
        route_source=route.route_source,
    )


def _non_lesson_advice(
    route: NiaOuterRouteContext,
    action: str,
    *,
    visible_actions: tuple[str, ...],
    eligible_actions: tuple[str, ...],
    current_hp: int | None,
) -> NiaOuterAdvice:
    return NiaOuterAdvice(
        status=STATUS_READY,
        action=action,
        reason=(
            f"N.I.A. {route.difficulty} week {route.week}: selected the "
            f"unambiguous/policy-ranked visible {action} action; no unsupported "
            "reward value was guessed. Consultation/shop, when enabled by the "
            "live policy, is entered only for Maa's safe exit workflow."
        ),
        issues=(),
        produce_id=route.produce_id,
        difficulty=route.difficulty,
        total_steps=route.total_steps,
        week=route.week,
        stage=route.stage,
        next_audition_stage=route.next_audition_stage,
        next_audition_week=route.next_audition_week,
        visible_actions=visible_actions,
        eligible_actions=eligible_actions,
        predicted_hp=current_hp,
        attribute=None,
        current_attribute=None,
        predicted_attribute=None,
        attribute_cap=route.attribute_cap,
        nominal_attribute_gain=0,
        effective_attribute_gain=0,
        overflow_attribute_gain=0,
        is_sp=False,
        estimated_lesson_hp_cost=None,
        reserved_hp_for_next_lesson=None,
        gain_estimate_source=None,
        route_source=route.route_source,
    )


def _productive_rest_deferral_advice(
    route: NiaOuterRouteContext,
    actions: tuple[str, ...],
    *,
    policy: NiaOuterPolicy,
    visible_actions: tuple[str, ...],
    eligible_actions: tuple[str, ...],
    current_hp: int,
    lesson_cost: int | None,
    reserve: int,
) -> NiaOuterAdvice | None:
    """Prefer a current productive action to speculative early Rest.

    This is intentionally action-semantic rather than week-based.  It does
    not guess the action's reward or future stamina; the next outer snapshot
    is re-read before another lesson decision.
    """

    candidates = tuple(
        action for action in actions if action in _REST_DEFERRING_ACTIONS
    )
    if not candidates:
        return None
    if len(candidates) == 1:
        selected = candidates[0]
    else:
        selected = next(
            (
                action
                for action in policy.non_lesson_priority
                if action in candidates
            ),
            "",
        )
        if not selected:
            return None
    advice = _non_lesson_advice(
        route,
        selected,
        visible_actions=visible_actions,
        eligible_actions=eligible_actions,
        current_hp=current_hp,
    )
    lesson_detail = (
        f" The next lesson estimate is {lesson_cost} HP with a {reserve} HP "
        "reserve."
        if lesson_cost is not None
        else f" The current stamina threshold is {reserve} HP."
    )
    return replace(
        advice,
        reason=(
            f"{advice.reason} Rest is deferred because {selected} is a "
            "recognized productive non-lesson action; stamina will be "
            f"re-read from the next LocalSave before committing to Rest."
            f"{lesson_detail}"
        ),
        estimated_lesson_hp_cost=lesson_cost,
        reserved_hp_for_next_lesson=reserve,
    )


def _current_step(snapshot: ProduceOuterLocalSaveSnapshot) -> str | None:
    # Once the latest week marker is also recorded as completed, the visible
    # overview already belongs to the following week.  The completed step can
    # still be the final PlayLog cell (no later event row is required), so its
    # position alone must not turn that next overview into an in-progress step.
    if snapshot.last_completed_week == snapshot.latest_week_marker:
        return None
    if snapshot.last_log_cell_type_name != "step" or not snapshot.completed_steps:
        return None
    latest = snapshot.completed_steps[-1]
    if latest.log_index != snapshot.log_count - 1:
        return None
    return latest.step_type_name


def _mark_outer_policy_v1(advice: NiaOuterAdvice, action: str) -> NiaOuterAdvice:
    return replace(
        advice,
        reason=(
            f"{advice.reason} Outer Policy v1 ranked {action} before the "
            "existing recovery policy, using the highest fan-vote-unlocked "
            "next-stage and highest final "
            "Master no-parameter-penalty baselines."
        ),
    )


def advise_nia_outer(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    overview_output: Mapping[str, object] | None = None,
    training_output: Mapping[str, object] | None = None,
    produce_id: str | None = None,
    idol_card_id: str | None = None,
    runtime_schedule: Sequence[Mapping[str, object]] | None = None,
    exam_composition: NiaOuterExamSaveComposition | None = None,
    policy: NiaOuterPolicy = NiaOuterPolicy(),
    learned_prior: NiaOuterPrior | None = None,
    run_shadow: RunShadowState | None = None,
    outer_policy_weights: OuterPolicyWeights | None = None,
    outing_suggestion_id: str | None = None,
    two_week_context: NiaTwoWeekRecoveryContext | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaOuterAdvice:
    """Return one evidence-bounded N.I.A. outer action without any click."""

    if not isinstance(snapshot, ProduceOuterLocalSaveSnapshot):
        raise TypeError("snapshot must be ProduceOuterLocalSaveSnapshot")
    if overview_output is not None:
        overview_output = _mapping(overview_output, "overview_output")
    if training_output is not None:
        training_output = _mapping(training_output, "training_output")
    if outing_suggestion_id is not None and (
        not isinstance(outing_suggestion_id, str) or not outing_suggestion_id
    ):
        raise ValueError("outing_suggestion_id must be non-empty when supplied")
    if two_week_context is not None and not isinstance(
        two_week_context, NiaTwoWeekRecoveryContext
    ):
        raise TypeError("two_week_context must be NiaTwoWeekRecoveryContext")
    if learned_prior is not None and not isinstance(learned_prior, NiaOuterPrior):
        raise TypeError("learned_prior must implement NiaOuterPrior")
    if run_shadow is not None and not isinstance(run_shadow, RunShadowState):
        raise TypeError("run_shadow must be RunShadowState")
    if outer_policy_weights is not None and not isinstance(
        outer_policy_weights, OuterPolicyWeights
    ):
        raise TypeError("outer_policy_weights must be OuterPolicyWeights")
    _validate_policy(policy)
    directory = Path(master_dir).resolve()
    try:
        route = adapt_nia_outer_route(
            snapshot,
            overview_output=overview_output,
            produce_id=produce_id,
            runtime_schedule=runtime_schedule,
            exam_composition=exam_composition,
            master_dir=directory,
        )
    except NiaOuterRouteAdapterError as error:
        return _unavailable(_issue(error.code, error.detail))
    except (OSError, KeyError, ValueError) as error:
        return _unavailable(
            _issue(
                "nia-master-route-unavailable",
                f"N.I.A. Master/route data could not be loaded: "
                f"{type(error).__name__}: {error}",
            )
        )

    if snapshot.lifecycle is not None and not snapshot.lifecycle.is_in_progress:
        return _unavailable(
            _issue("nia-run-inactive", "N.I.A. lifecycle is not in progress"),
            route=route,
        )
    if route.phase is None:
        return _unavailable(
            _issue(
                "audition-boundary",
                f"N.I.A. {route.difficulty} week {route.week} is the "
                f"{route.stage} audition boundary; outer actions are unavailable",
                f"Produce.yaml#{route.produce_id}",
            ),
            route=route,
        )
    active_step = _current_step(snapshot)
    if active_step is not None:
        return _unavailable(
            _issue(
                "outer-step-in-progress",
                f"ProducePlayLog already entered {active_step}; wait for the next week marker",
            ),
            route=route,
        )

    try:
        options, visible, unknown, missing_container = _available_options(
            overview_output,
            training_output,
            include_shops=policy.allow_shop_exit,
        )
    except _DecisionGap as error:
        return _unavailable(
            _issue(error.code, error.detail),
            route=route,
        )
    eligible = tuple(option.action for option in options)
    if unknown:
        return _unavailable(
            _issue(
                "unknown-visible-action",
                "unsupported visible action(s): " + ", ".join(unknown),
            ),
            route=route,
            visible_actions=visible,
            eligible_actions=eligible,
        )

    if missing_container:
        return _unavailable(
            _issue(
                "lesson-attribute-unavailable",
                "self_lesson is visible but no completed Vo/Da/Vi training options were supplied",
            ),
            route=route,
            visible_actions=visible,
            eligible_actions=eligible,
        )
    if not options:
        return _unavailable(
            _issue(
                "no-eligible-visible-action",
                "no usable action remains after consultation/shop filtering",
            ),
            route=route,
            visible_actions=visible,
        )

    state = _overview_state(overview_output)
    try:
        current_hp = _state_value(snapshot, state, "stamina")
        max_hp = _maximum_hp(snapshot, state, current_hp)
        attributes = {
            field: _state_value(snapshot, state, field)
            for field in ("vocal", "dance", "visual")
        }
    except _DecisionGap as error:
        return _unavailable(
            _issue(error.code, error.detail),
            route=route,
            visible_actions=visible,
            eligible_actions=eligible,
        )

    # Accepted outer evidence is scoped by the exact idol/archetype identity.
    # The live adviser remains usable without an idol id (for the diagnostic
    # GUI), but a learned prior must return no ranking until all identity
    # fields are available.  That makes the ordinary Master/Maa policy the
    # safe fallback instead of silently reusing another idol's route.
    profile = None
    if isinstance(idol_card_id, str) and idol_card_id:
        try:
            from .master_db import get_idol_profile

            profile = get_idol_profile(idol_card_id)
        except (OSError, TypeError, ValueError):
            profile = None
    prior_features: Mapping[str, object] = {
        "produce_id": route.produce_id,
        "idol_card_id": idol_card_id,
        "character_id": None if profile is None else profile.character_id,
        "plan_type": None if profile is None else profile.plan_type,
        "exam_effect_type": None if profile is None else profile.exam_effect_type,
        "difficulty": route.difficulty,
        "week": route.week,
        "phase": route.phase,
        "stage": route.stage,
        "next_audition_week": route.next_audition_week,
        "stamina": current_hp,
        "max_stamina": max_hp,
        "produce_points": snapshot.produce_points,
        "vocal": attributes["vocal"],
        "dance": attributes["dance"],
        "visual": attributes["visual"],
    }

    def learned_order(actions: Sequence[str]) -> tuple[str, ...]:
        legal = tuple(dict.fromkeys(actions))
        if learned_prior is None or not legal:
            return ()
        try:
            ranked = tuple(learned_prior.rank(prior_features, legal))
        except (KeyError, TypeError, ValueError):
            return ()
        legal_set = set(legal)
        return tuple(dict.fromkeys(action for action in ranked if action in legal_set))

    lessons = tuple(
        option
        for option in options
        if option.action in _LESSON_ACTION_TO_ATTRIBUTE
    )
    rest_visible = any(option.action == REST for option in options)
    non_rest = tuple(
        option.action
        for option in options
        if (
            option.action in _KNOWN_NON_LESSON_ACTIONS
            or (policy.allow_shop_exit and option.action in _SHOP_ACTIONS)
        )
        and option.action != REST
    )
    try:
        catalog = _profile_catalog(route, policy, directory)
    except (OSError, KeyError, ValueError) as error:
        return _unavailable(
            _issue(
                "nia-self-lesson-master-unavailable",
                "ProduceStepSelfLesson Master data could not be loaded: "
                f"{type(error).__name__}: {error}",
            ),
            route=route,
            visible_actions=visible,
            eligible_actions=eligible,
        )
    if lessons:
        if current_hp is None:
            return _unavailable(
                _issue(
                    "stamina-unavailable",
                    "lesson choices are visible but authoritative HP is unavailable",
                ),
                route=route,
                visible_actions=visible,
                eligible_actions=eligible,
            )
        evaluations: list[_LessonEvaluation] = []
        unprojected: list[tuple[_LiveOption, str, str, int]] = []
        for option in lessons:
            attribute, field = _LESSON_ACTION_TO_ATTRIBUTE[option.action]
            current = attributes[field]
            if current is None:
                return _unavailable(
                    _issue(
                        "attribute-unavailable",
                        f"{attribute} is unavailable in both PlayLog and overview",
                    ),
                    route=route,
                    visible_actions=visible,
                    eligible_actions=eligible,
                )
            if current < 0 or current > route.attribute_cap:
                return _unavailable(
                    _issue(
                        "attribute-outside-master-cap",
                        f"{attribute}={current} is outside 0..{route.attribute_cap}",
                    ),
                    route=route,
                    visible_actions=visible,
                    eligible_actions=eligible,
                )
            estimate = _estimate(
                snapshot,
                route,
                catalog,
                field=field,
                is_sp=option.is_sp,
            )
            if estimate is None:
                if policy.allow_unprojected_lesson_results:
                    unprojected.append((option, attribute, field, current))
                    continue
                tier = "sp" if option.is_sp else "normal"
                return _unavailable(
                    _issue(
                        "nia-self-lesson-profile-unavailable",
                        f"{route.produce_id} phase {route.phase} {tier} has no "
                        "ProduceStepSelfLesson row, same-phase PlayLog evidence, "
                        "or explicit NiaOuterPolicy profile",
                        "ProduceStepSelfLesson.yaml",
                        f"policy:{policy.name}",
                    ),
                    route=route,
                    visible_actions=visible,
                    eligible_actions=eligible,
                )
            adjusted_estimate = _apply_owned_item_stamina(
                snapshot,
                estimate,
                action=option.action,
                is_sp=option.is_sp,
                master_dir=directory,
            )
            if isinstance(adjusted_estimate, NiaOuterAdvisorIssue):
                return _unavailable(
                    adjusted_estimate,
                    route=route,
                    visible_actions=visible,
                    eligible_actions=eligible,
                    predicted_hp=current_hp,
                    lesson_cost=estimate.hp_cost,
                )
            estimate = adjusted_estimate
            headroom = max(0, route.attribute_cap - current)
            effective = min(estimate.nominal_gain, headroom)
            overflow = max(0, estimate.nominal_gain - effective)
            reserve = max(2, estimate.hp_cost * policy.reserve_lesson_count)
            evaluations.append(
                _LessonEvaluation(
                    option=option,
                    attribute=attribute,
                    field=field,
                    current=current,
                    estimate=estimate,
                    effective_gain=effective,
                    overflow_gain=overflow,
                    predicted_attribute=current + effective,
                    predicted_hp=max(0, current_hp - estimate.hp_cost),
                    reserve=reserve,
                )
            )
        if unprojected:
            # Production live mode does not need to predict the server's final
            # lesson result.  It only requires authoritative current stamina,
            # the correct attribute and the mode cap.  Prefer SP, then the
            # largest remaining headroom; observe the actual gain/HP from the
            # next Produce LocalSave.
            if len(unprojected) != len(lessons):
                # Do not compare a numeric projection with an intentionally
                # unprojected option.  Apply the same cap-oriented policy to
                # every visible lesson in this live decision.
                unprojected = [
                    (
                        option,
                        _LESSON_ACTION_TO_ATTRIBUTE[option.action][0],
                        _LESSON_ACTION_TO_ATTRIBUTE[option.action][1],
                        attributes[_LESSON_ACTION_TO_ATTRIBUTE[option.action][1]],
                    )
                    for option in lessons
                ]
            minimum_hp = (
                max_hp * policy.unprojected_lesson_min_stamina_permille // 1000
                if max_hp is not None
                else None
            )
            if (
                rest_visible
                and minimum_hp is not None
                and current_hp < minimum_hp
            ):
                deferral = _productive_rest_deferral_advice(
                    route,
                    non_rest,
                    policy=policy,
                    visible_actions=visible,
                    eligible_actions=eligible,
                    current_hp=current_hp,
                    lesson_cost=None,
                    reserve=minimum_hp,
                )
                if deferral is not None:
                    return deferral
                outing = _outing_recovery_advice(
                    route,
                    visible_actions=visible,
                    eligible_actions=eligible,
                    current_hp=current_hp,
                    max_hp=max_hp,
                    required_hp=minimum_hp,
                    lesson_cost=None,
                    reserve=minimum_hp,
                    suggestion_id=outing_suggestion_id,
                    master_dir=directory,
                )
                if outing is not None:
                    return outing
                return _unprojected_live_rest_advice(
                    route,
                    current_hp=current_hp,
                    max_hp=max_hp,
                    threshold_hp=minimum_hp,
                    visible_actions=visible,
                    eligible_actions=eligible,
                )
            learned_lessons = learned_order(
                tuple(option.action for option, _attribute, _field, _current in unprojected)
            )
            learned_lesson_score = {
                action: len(learned_lessons) - index
                for index, action in enumerate(learned_lessons)
            }
            selected_option, attribute, _field, current = max(
                unprojected,
                key=lambda item: (
                    item[0].is_sp,
                    route.attribute_cap - item[3],
                    learned_lesson_score.get(item[0].action, 0),
                    item[0].action,
                ),
            )
            return _unprojected_live_lesson_advice(
                route,
                selected_option,
                attribute=attribute,
                current_attribute=current,
                current_hp=current_hp,
                visible_actions=visible,
                eligible_actions=eligible,
            )
        evaluated = tuple(evaluations)
        base_candidates = tuple(
            (
                item.option.action,
                item.field,
                base.nominal_gain,
                item.estimate.hp_cost,
            )
            for item in evaluated
            if (base := catalog.get((route.phase, item.option.is_sp)))
            is not None
            and item.effective_gain > 0
        )
        if len(base_candidates) != len(evaluated):
            base_candidates = ()
        reserve = max(item.reserve for item in evaluated)
        outer_policy_action = rank_nia_outer_lesson_policy_v1(
            snapshot,
            route,
            base_candidates,
            rest_visible=rest_visible,
            reserve=reserve,
            profile=profile,
            shadow=run_shadow,
            prior=learned_prior,
            prior_features=prior_features,
            # No accepted data yet proves different Plan weights.  The seam
            # permits later calibration without another state machine.
            weights=outer_policy_weights or OuterPolicyWeights(),
            master_dir=directory,
        )
        if outer_policy_action is not None:
            if (
                outer_policy_action == REST
                and current_hp is not None
                and max_hp is not None
            ):
                return _mark_outer_policy_v1(
                    _rest_advice(
                        route,
                        visible_actions=visible,
                        eligible_actions=eligible,
                        current_hp=current_hp,
                        max_hp=max_hp,
                        lesson_cost=max(item.estimate.hp_cost for item in evaluated),
                        reserve=reserve,
                    ),
                    outer_policy_action,
                )
            evaluated = tuple(
                item
                for item in evaluated
                if item.option.action == outer_policy_action
            )
        useful = tuple(item for item in evaluated if item.effective_gain > 0)
        if not useful:
            return _unavailable(
                _issue(
                    "all-visible-attributes-at-cap",
                    f"all visible lesson attributes are at the N.I.A. "
                    f"{route.difficulty} cap {route.attribute_cap}",
                ),
                route=route,
                visible_actions=visible,
                eligible_actions=eligible,
                predicted_hp=current_hp,
            )
        safe = tuple(item for item in useful if item.safe)
        if not safe:
            lesson_cost = max(item.estimate.hp_cost for item in useful)
            reserve = max(item.reserve for item in useful)
            if rest_visible:
                deferral = _productive_rest_deferral_advice(
                    route,
                    non_rest,
                    policy=policy,
                    visible_actions=visible,
                    eligible_actions=eligible,
                    current_hp=current_hp,
                    lesson_cost=lesson_cost,
                    reserve=reserve,
                )
                if deferral is not None:
                    return deferral
                if max_hp is None:
                    return _unavailable(
                        _issue(
                            "max-stamina-unavailable",
                            "Rest is visible but max HP is unavailable",
                        ),
                        route=route,
                        visible_actions=visible,
                        eligible_actions=eligible,
                        predicted_hp=current_hp,
                        lesson_cost=lesson_cost,
                        reserve=reserve,
                    )
                outing = _outing_recovery_advice(
                    route,
                    visible_actions=visible,
                    eligible_actions=eligible,
                    current_hp=current_hp,
                    max_hp=max_hp,
                    required_hp=min(
                        item.estimate.hp_cost + item.reserve
                        for item in useful
                    ),
                    lesson_cost=lesson_cost,
                    reserve=reserve,
                    suggestion_id=outing_suggestion_id,
                    master_dir=directory,
                )
                if outing is not None:
                    return outing
                return _rest_advice(
                    route,
                    visible_actions=visible,
                    eligible_actions=eligible,
                    current_hp=current_hp,
                    max_hp=max_hp,
                    lesson_cost=lesson_cost,
                    reserve=reserve,
                )
            return _unavailable(
                _issue(
                    "lesson-hp-reserve-unsafe",
                    f"every lesson would break the {reserve} HP reserve and Rest is not visible",
                ),
                route=route,
                visible_actions=visible,
                eligible_actions=eligible,
                predicted_hp=current_hp,
                lesson_cost=lesson_cost,
                reserve=reserve,
            )
        cap_safe_sp = tuple(
            item
            for item in safe
            if item.option.is_sp and item.overflow_gain == 0
        )
        pool = cap_safe_sp or safe
        preferred = _preferred_attribute(training_output)
        learned_lessons = learned_order(tuple(item.option.action for item in pool))
        learned_lesson_score = {
            action: len(learned_lessons) - index
            for index, action in enumerate(learned_lessons)
        }
        selected = max(
            pool,
            key=lambda item: (
                item.effective_gain,
                item.option.is_sp,
                item.overflow_gain == 0,
                item.attribute == preferred,
                learned_lesson_score.get(item.option.action, 0),
                -item.current,
                item.option.action,
            ),
        )
        advice = _lesson_advice(
            route,
            selected,
            evaluated,
            visible_actions=visible,
            eligible_actions=eligible,
        )
        advice = _apply_two_week_recovery_choice(
            advice,
            route,
            selected,
            context=two_week_context,
            rest_visible=rest_visible,
            current_hp=current_hp,
            max_hp=max_hp,
            visible_actions=visible,
            eligible_actions=eligible,
        )
        if outer_policy_action is not None:
            return _mark_outer_policy_v1(advice, outer_policy_action)
        return advice

    phase_estimates: list[_LessonEstimate] = []
    phase_projection_issue: NiaOuterAdvisorIssue | None = None
    for (phase, is_sp), profile in catalog.items():
        if phase != route.phase:
            continue
        base_estimate = _LessonEstimate(
            hp_cost=profile.hp_cost,
            nominal_gain=profile.nominal_gain,
            source=profile.source,
        )
        for action in _LESSON_ACTION_TO_ATTRIBUTE:
            adjusted = _apply_owned_item_stamina(
                snapshot,
                base_estimate,
                action=action,
                is_sp=is_sp,
                master_dir=directory,
            )
            if isinstance(adjusted, NiaOuterAdvisorIssue):
                phase_projection_issue = adjusted
                break
            phase_estimates.append(adjusted)
        if phase_projection_issue is not None:
            break
    if rest_visible and phase_projection_issue is not None:
        return _unavailable(
            phase_projection_issue,
            route=route,
            visible_actions=visible,
            eligible_actions=eligible,
            predicted_hp=current_hp,
        )
    phase_costs = tuple(estimate.hp_cost for estimate in phase_estimates)
    if rest_visible and current_hp is None:
        return _unavailable(
            _issue(
                "stamina-unavailable",
                "Rest is visible but authoritative HP is unavailable",
            ),
            route=route,
            visible_actions=visible,
            eligible_actions=eligible,
        )
    if (
        rest_visible
        and not phase_costs
        and policy.allow_unprojected_lesson_results
    ):
        if max_hp is None:
            return _unavailable(
                _issue(
                    "max-stamina-unavailable",
                    "live Rest policy requires authoritative max HP",
                ),
                route=route,
                visible_actions=visible,
                eligible_actions=eligible,
                predicted_hp=current_hp,
            )
        threshold_hp = (
            max_hp * policy.unprojected_lesson_min_stamina_permille // 1000
        )
        if current_hp < threshold_hp:
            deferral = _productive_rest_deferral_advice(
                route,
                non_rest,
                policy=policy,
                visible_actions=visible,
                eligible_actions=eligible,
                current_hp=current_hp,
                lesson_cost=None,
                reserve=threshold_hp,
            )
            if deferral is not None:
                return deferral
            outing = _outing_recovery_advice(
                route,
                visible_actions=visible,
                eligible_actions=eligible,
                current_hp=current_hp,
                max_hp=max_hp,
                required_hp=threshold_hp,
                lesson_cost=None,
                reserve=threshold_hp,
                suggestion_id=outing_suggestion_id,
                master_dir=directory,
            )
            if outing is not None:
                return outing
            return _unprojected_live_rest_advice(
                route,
                current_hp=current_hp,
                max_hp=max_hp,
                threshold_hp=threshold_hp,
                visible_actions=visible,
                eligible_actions=eligible,
            )
        lesson_cost = 0
        reserve = threshold_hp
        needs_rest = False
    elif rest_visible and not phase_costs:
        return _unavailable(
            _issue(
                "nia-rest-threshold-unavailable",
                f"{route.produce_id} phase {route.phase} has no Master or "
                "explicit policy lesson cost with which to judge low HP",
                "ProduceStepSelfLesson.yaml",
                f"policy:{policy.name}",
            ),
            route=route,
            visible_actions=visible,
            eligible_actions=eligible,
            predicted_hp=current_hp,
        )
    elif rest_visible and current_hp is not None and phase_costs:
        representative = max(
            phase_estimates,
            key=lambda estimate: (
                estimate.hp_cost,
                estimate.item_stamina_cost,
                estimate.source,
            ),
        )
        lesson_cost = representative.hp_cost
        reserve = max(2, lesson_cost * policy.reserve_lesson_count)
        needs_rest = max(0, current_hp - lesson_cost) < reserve
    else:
        lesson_cost = 0
        reserve = 0
        needs_rest = False
    if needs_rest:
        deferral = _productive_rest_deferral_advice(
            route,
            non_rest,
            policy=policy,
            visible_actions=visible,
            eligible_actions=eligible,
            current_hp=current_hp,
            lesson_cost=lesson_cost,
            reserve=reserve,
        )
        if deferral is not None:
            return deferral
        if max_hp is None:
            return _unavailable(
                _issue(
                    "max-stamina-unavailable",
                    "low HP makes Rest relevant but max HP is unavailable",
                ),
                route=route,
                visible_actions=visible,
                eligible_actions=eligible,
            )
        outing = _outing_recovery_advice(
            route,
            visible_actions=visible,
            eligible_actions=eligible,
            current_hp=current_hp,
            max_hp=max_hp,
            required_hp=lesson_cost + reserve,
            lesson_cost=lesson_cost,
            reserve=reserve,
            suggestion_id=outing_suggestion_id,
            master_dir=directory,
        )
        if outing is not None:
            return outing
        return _rest_advice(
            route,
            visible_actions=visible,
            eligible_actions=eligible,
            current_hp=current_hp,
            max_hp=max_hp,
            lesson_cost=lesson_cost,
            reserve=reserve,
            base_lesson_hp_cost=representative.base_hp_cost,
            lesson_item_stamina_cost=representative.item_stamina_cost,
            lesson_item_effect_ids=representative.item_effect_ids,
        )
    if not non_rest:
        return _unavailable(
            _issue(
                "rest-only-without-low-hp-proof",
                "Rest is the only eligible tile but current evidence does not prove it is required",
            ),
            route=route,
            visible_actions=visible,
            eligible_actions=eligible,
            predicted_hp=current_hp,
        )
    if len(non_rest) == 1:
        selected_action = non_rest[0]
        learned_selected = False
    else:
        learned_non_rest = learned_order(non_rest)
        selected_action = learned_non_rest[0] if learned_non_rest else next(
            (action for action in policy.non_lesson_priority if action in non_rest),
            "",
        )
        learned_selected = bool(learned_non_rest)
        if not selected_action:
            return _unavailable(
                _issue(
                    "non-lesson-policy-required",
                    "multiple non-lesson actions are visible; supply an explicit "
                    "NiaOuterPolicy.non_lesson_priority instead of guessing rewards",
                ),
                route=route,
                visible_actions=visible,
                eligible_actions=eligible,
                predicted_hp=current_hp,
            )
    advice = _non_lesson_advice(
        route,
        selected_action,
        visible_actions=visible,
        eligible_actions=eligible,
        current_hp=current_hp,
    )
    if learned_selected:
        advice = replace(
            advice,
            reason=(
                f"{advice.reason} Accepted-run outer prior ranked only the "
                "currently legal non-lesson actions."
            ),
        )
    return advice


def read_current_nia_outer_advice(
    *,
    overview_output: Mapping[str, object] | None = None,
    training_output: Mapping[str, object] | None = None,
    produce_id: str | None = None,
    idol_card_id: str | None = None,
    runtime_schedule: Sequence[Mapping[str, object]] | None = None,
    policy: NiaOuterPolicy = NiaOuterPolicy(),
    learned_prior: NiaOuterPrior | None = None,
    run_shadow: RunShadowState | None = None,
    outer_policy_weights: OuterPolicyWeights | None = None,
    outing_suggestion_id: str | None = None,
    two_week_context: NiaTwoWeekRecoveryContext | None = None,
    game_root: str | Path = DEFAULT_PC_GAME_ROOT,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaOuterAdvice:
    """Read the active outer save only; no live reader or click is invoked."""

    snapshot = read_current_produce_outer_local_save(game_root)
    return advise_nia_outer(
        snapshot,
        overview_output=overview_output,
        training_output=training_output,
        produce_id=produce_id,
        idol_card_id=idol_card_id,
        runtime_schedule=runtime_schedule,
        policy=policy,
        learned_prior=learned_prior,
        run_shadow=run_shadow,
        outer_policy_weights=outer_policy_weights,
        outing_suggestion_id=outing_suggestion_id,
        two_week_context=two_week_context,
        master_dir=master_dir,
    )


__all__ = [
    "NiaOuterAdvice",
    "NiaOuterAdvisorIssue",
    "NiaOuterLessonProfile",
    "NiaOuterModeRules",
    "NiaOuterPolicy",
    "NiaOuterRouteAdapterError",
    "NiaOuterRouteContext",
    "POLICY_SCHEMA_NAME",
    "SCHEMA_NAME",
    "STAGE_BEFORE_MID1",
    "STAGE_BETWEEN_MID1_MID2",
    "STAGE_BETWEEN_MID2_FINAL",
    "STAGE_FINAL",
    "STAGE_MID1",
    "STAGE_MID2",
    "STATUS_READY",
    "STATUS_UNAVAILABLE",
    "UNAVAILABLE_ACTION",
    "adapt_nia_outer_route",
    "advise_nia_outer",
    "load_nia_outer_mode_rules",
    "load_nia_outer_policy",
    "nia_outer_policy_from_mapping",
    "read_current_nia_outer_advice",
]
