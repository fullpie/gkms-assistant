"""Read-only outer-action advice for Initial (Regular) Produce.

The live readers already know how to recognize an overview or a Vo/Da/Vi
training choice.  This module deliberately starts *after* those reads: it
joins their structured output with the current outer local-save snapshot and
returns a decision only.  It never captures a screen and never executes the
``SuggestedClick`` embedded in a live-reader result.

The policy is intentionally small and auditable:

* prefer SP lessons whose estimated gain fits below Regular's 1000 cap;
* estimate only the direct lesson-attribute gain, using the newest comparable
  completed lesson in the same outer save when one exists;
* spend HP only when the complete proven route horizon remains funded;
* treat hard/cram lessons as a larger stamina commitment;
* recover with the Master-defined Rest amount when that budget would break;
* accept a visible Consultation tile in server-driven Pro/Master weeks;
* discard only unsupported generic shop aliases.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from math import ceil
from pathlib import Path
from typing import Any, Mapping

import yaml

from .overview_actions import (
    ACTIVITY,
    CLASS,
    CONSULTATION,
    DANCE_LESSON,
    LESSON_COST_FLOOR,
    LESSON_COST_RATIO,
    REST,
    RegularStaminaPlan,
    VOCAL_LESSON,
    VISUAL_LESSON,
    plan_regular_stamina,
)
from .produce_outer_local_save import (
    DEFAULT_PC_GAME_ROOT,
    ProduceOuterLocalSaveSnapshot,
    read_current_produce_outer_local_save,
)
from .route_calendar import (
    CLASS as ROUTE_CLASS,
    CRAM_LESSON,
    DEFAULT_MASTER_DIR,
    LESSON,
    OUTING,
    SUPPLY,
    load_route_calendar,
)
from .screen_state import OverviewState


SCHEMA_NAME = "gkms.initial-regular-outer-advice.v1"
STATUS_READY = "ready"
STATUS_UNAVAILABLE = "unavailable"
UNAVAILABLE_ACTION = "unavailable"

ATTRIBUTE_CAP = 1000
REST_RECOVERY_PERMIL = 700

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
    {ACTIVITY, CLASS, CONSULTATION, OUTING, REST}
)
_SHOP_ACTIONS = frozenset({"shop"})


@dataclass(frozen=True, slots=True)
class InitialRegularOuterPolicy:
    """Fallback attribute gains when outer history has no comparable lesson.

    Attribute caps, route length, and Rest recovery are deliberately not
    configurable policy values; they are loaded from the current Master and
    missing fields fail closed.
    """

    fallback_sp_gain: int = 100
    fallback_normal_gain: int = 70


@dataclass(frozen=True, slots=True)
class InitialRegularModeRules:
    """Initial Regular fields loaded from the current Master dump."""

    produce_id: str
    total_steps: int
    attribute_cap: int
    produce_setting_id: str
    rest_recovery_permille: int
    before_audition_recovery_permille: int
    exam_start_alert_stamina_threshold: int


@dataclass(frozen=True, slots=True)
class InitialRegularOuterAdvice:
    """Stable, click-free result for one visible outer-action choice."""

    status: str
    action: str
    reason: str
    predicted_hp: int | None
    attribute: str | None
    current_attribute: int | None
    predicted_attribute: int | None
    nominal_attribute_gain: int | None
    effective_attribute_gain: int | None
    overflow_attribute_gain: int | None
    is_sp: bool | None
    estimated_lesson_hp_cost: int | None
    reserved_hp_for_next_lesson: int | None
    gain_estimate_source: str | None
    current_week: int | None = None
    weeks_until_final: int | None = None
    required_hp_for_horizon: int | None = None
    final_hp_reserve: int | None = None
    next_recovery_week: int | None = None
    lesson_weeks_before_recovery: tuple[int, ...] = ()
    rest_recovery_permille: int | None = None
    route_budget_source: str | None = None
    schema: str = SCHEMA_NAME

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _LiveOption:
    action: str
    is_sp: bool


@dataclass(frozen=True, slots=True)
class _GainEstimate:
    value: int
    source: str


@dataclass(frozen=True, slots=True)
class _LessonEvaluation:
    option: _LiveOption
    attribute: str
    field: str
    current: int
    nominal_gain: int
    effective_gain: int
    overflow_gain: int
    predicted: int
    gain_source: str


def _unavailable(
    reason: str,
    *,
    predicted_hp: int | None = None,
    lesson_cost: int | None = None,
    reserve: int | None = None,
) -> InitialRegularOuterAdvice:
    return InitialRegularOuterAdvice(
        status=STATUS_UNAVAILABLE,
        action=UNAVAILABLE_ACTION,
        reason=reason,
        predicted_hp=predicted_hp,
        attribute=None,
        current_attribute=None,
        predicted_attribute=None,
        nominal_attribute_gain=None,
        effective_attribute_gain=None,
        overflow_attribute_gain=None,
        is_sp=None,
        estimated_lesson_hp_cost=lesson_cost,
        reserved_hp_for_next_lesson=reserve,
        gain_estimate_source=None,
    )


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


@lru_cache(maxsize=8)
def _master_rows(path: Path) -> tuple[Mapping[str, Any], ...]:
    if not path.is_file():
        raise FileNotFoundError(f"Master file not found: {path}")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list):
        raise ValueError(f"{path.name} must contain a list")
    return tuple(row for row in payload if isinstance(row, Mapping))


def _unique_master_row(
    rows: tuple[Mapping[str, Any], ...],
    *,
    row_id: str,
    label: str,
) -> Mapping[str, Any]:
    matches = tuple(row for row in rows if row.get("id") == row_id)
    if len(matches) != 1:
        raise ValueError(f"{label} must resolve exactly once; found {len(matches)}")
    return matches[0]


def _required_master_int(
    row: Mapping[str, Any],
    key: str,
    *,
    minimum: int = 0,
) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{key} must be an integer >= {minimum}")
    return value


@lru_cache(maxsize=8)
def _load_initial_regular_mode_rules_cached(
    master_dir: Path,
    produce_id: str,
) -> InitialRegularModeRules:
    produce = _unique_master_row(
        _master_rows(master_dir / "Produce.yaml"),
        row_id=produce_id,
        label=f"Produce[{produce_id}]",
    )
    setting_id = produce.get("produceSettingId")
    if not isinstance(setting_id, str) or not setting_id:
        raise ValueError("Produce.produceSettingId is unavailable")
    setting = _unique_master_row(
        _master_rows(master_dir / "ProduceSetting.yaml"),
        row_id=setting_id,
        label=f"ProduceSetting[{setting_id}]",
    )
    rules = InitialRegularModeRules(
        produce_id=produce_id,
        total_steps=_required_master_int(produce, "steps", minimum=1),
        attribute_cap=_required_master_int(
            produce, "idolCardParameterGrowthLimit", minimum=1
        ),
        produce_setting_id=setting_id,
        rest_recovery_permille=_required_master_int(
            setting, "refreshStaminaRecoveryPermil"
        ),
        before_audition_recovery_permille=_required_master_int(
            setting, "beforeAuditionRefreshStaminaRecoveryPermil"
        ),
        exam_start_alert_stamina_threshold=_required_master_int(
            setting, "examStartAlertStaminaThreshold"
        ),
    )
    for field, value in (
        ("refreshStaminaRecoveryPermil", rules.rest_recovery_permille),
        (
            "beforeAuditionRefreshStaminaRecoveryPermil",
            rules.before_audition_recovery_permille,
        ),
    ):
        if not 0 <= value <= 1000:
            raise ValueError(f"ProduceSetting.{field} must be 0..1000")
    return rules


def load_initial_regular_mode_rules(
    produce_id: str = "produce-001",
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> InitialRegularModeRules:
    """Load Initial Regular cap, route length, and recovery fields."""

    if produce_id not in {"produce-001", "produce-002", "produce-003"}:
        raise ValueError(f"unsupported Initial produce_id: {produce_id}")
    return _load_initial_regular_mode_rules_cached(
        Path(master_dir).resolve(), produce_id
    )


def _validate_policy(policy: InitialRegularOuterPolicy) -> None:
    if not isinstance(policy, InitialRegularOuterPolicy):
        raise TypeError("policy must be InitialRegularOuterPolicy")
    if policy.fallback_sp_gain < 0 or policy.fallback_normal_gain < 0:
        raise ValueError("fallback lesson gains must be non-negative")


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
    local_value = _integer(getattr(snapshot, field))
    if local_value is not None:
        return local_value
    return _integer(overview_state.get(field))


def _maximum_hp(
    snapshot: ProduceOuterLocalSaveSnapshot,
    overview_state: Mapping[str, Any],
    current_hp: int | None,
) -> int | None:
    # Prefer an explicit maximum over historical HP values.  Historical
    # before/after values are only a lower-bound fallback when neither reader
    # currently exposes max HP.
    for value in (snapshot.max_stamina, overview_state.get("max_stamina")):
        parsed = _integer(value)
        if parsed is not None and parsed > 0:
            return parsed
    candidates: list[int] = []
    for step in snapshot.completed_steps:
        for line in step.lines:
            if line.line_type_name == "max_stamina":
                candidates.extend(
                    int(value)
                    for value in (line.before, line.after)
                    if value > 0 and float(value).is_integer()
                )
            elif line.line_type_name == "stamina":
                candidates.extend(
                    int(value)
                    for value in (line.before, line.after)
                    if value > 0 and float(value).is_integer()
                )
    if current_hp is not None and current_hp > 0:
        candidates.append(current_hp)
    return max(candidates) if candidates else None


def _lesson_cost(max_hp: int) -> int:
    return min(
        max_hp,
        max(LESSON_COST_FLOOR, ceil(max_hp * LESSON_COST_RATIO)),
    )


def _effective_outer_week(
    snapshot: ProduceOuterLocalSaveSnapshot,
) -> tuple[int | None, str | None]:
    """Resolve the week represented by a between-step outer save.

    The play log writes a Week record before its Step.  Once that Step is
    complete, ``latest_week_marker`` and ``last_completed_week`` are equal
    until the next Week record is persisted, even though the overview already
    offers the following week's action.  Advance only in that exact state;
    otherwise the latest Week record remains the effective week.
    """

    latest = snapshot.latest_week_marker
    if latest is None:
        return None, "outer LocalSave has no current week marker."
    completed = snapshot.last_completed_week
    if completed is not None and completed > latest:
        return None, (
            f"outer LocalSave completed week {completed} is ahead of its "
            f"latest week marker {latest}."
        )
    return latest + 1 if completed == latest else latest, None


def _route_stamina_plan(
    snapshot: ProduceOuterLocalSaveSnapshot,
    overview_output: Mapping[str, Any] | None,
    overview_state: Mapping[str, Any],
    *,
    current_hp: int,
    max_hp: int,
    rules: InitialRegularModeRules,
    master_dir: Path,
) -> tuple[RegularStaminaPlan | None, str]:
    if overview_output is None:
        return None, "Initial Regular stamina planning needs overview route evidence."
    route = _optional_mapping(overview_output.get("route"))
    if route is None:
        return None, "Initial Regular stamina planning needs overview.route evidence."
    produce_id = route.get("produce_id")
    if produce_id != rules.produce_id:
        return None, f"Initial Regular only; live route is {produce_id!r}."
    character_id = route.get("character_id")
    if not isinstance(character_id, str) or not character_id:
        return None, "overview.route.character_id is unavailable."
    current_week = _integer(route.get("current_week"))
    total_weeks = _integer(route.get("total_weeks"))
    if current_week is None or total_weeks is None:
        return None, "overview route week/total evidence is unavailable."
    if total_weeks != rules.total_steps:
        return None, (
            f"overview route has {total_weeks} weeks but Master has "
            f"{rules.total_steps}."
        )
    effective_week, week_error = _effective_outer_week(snapshot)
    if effective_week is None:
        return None, week_error or "outer LocalSave week evidence is unavailable."
    if effective_week != current_week:
        return None, (
            f"overview week {current_week} does not match outer LocalSave "
            f"effective week {effective_week}."
        )
    try:
        calendar = load_route_calendar(
            rules.produce_id,
            character_id=character_id,
            master_dir=master_dir,
        )
        route_week = calendar.week(current_week)
    except (FileNotFoundError, KeyError, TypeError, ValueError) as error:
        return None, f"route calendar is unavailable: {type(error).__name__}: {error}"
    if calendar.total_weeks != rules.total_steps:
        return None, "route calendar and Master total weeks disagree."
    horizon = calendar.weeks[current_week - 1 : calendar.final_week]
    if not horizon or not all(week.exact_actions for week in horizon):
        return None, (
            f"the {character_id} Initial Regular route horizon is not exact."
        )
    if route.get("route_actions_exact") is not True:
        return None, "live overview did not certify exact route actions."
    raw_actions = route.get("route_actions")
    if not isinstance(raw_actions, (list, tuple)) or any(
        not isinstance(action, str) for action in raw_actions
    ):
        return None, "live overview route actions are unavailable."
    if tuple(raw_actions) != route_week.actions:
        return None, "live overview route actions disagree with the route calendar."

    def required_state_int(field: str) -> int | None:
        return _state_value(snapshot, overview_state, field)

    vocal = required_state_int("vocal")
    dance = required_state_int("dance")
    visual = required_state_int("visual")
    if vocal is None or dance is None or visual is None:
        return None, "attribute evidence is incomplete for route planning."
    produce_points = required_state_int("produce_points")
    if produce_points is None:
        produce_points = 0
    state = OverviewState(
        weeks_remaining=calendar.final_week - current_week,
        stamina=current_hp,
        max_stamina=max_hp,
        produce_points=produce_points,
        vocal=vocal,
        dance=dance,
        visual=visual,
        confidence=1.0,
        raw_text={},
        countdown_target="final",
    )
    try:
        plan = plan_regular_stamina(
            state,
            route_week,
            route_horizon=horizon,
        )
    except (TypeError, ValueError) as error:
        return None, f"route stamina plan is unavailable: {type(error).__name__}: {error}"
    if not plan.route_horizon_exact:
        return None, "route stamina horizon is not exact."
    return plan, (
        f"route-calendar:{calendar.detail_scope}+"
        f"master:ProduceSetting.yaml#{rules.produce_setting_id}"
    )


def _overview_options(
    overview_output: Mapping[str, Any] | None,
) -> tuple[_LiveOption, ...]:
    if overview_output is None:
        return ()
    raw_options = overview_output.get("options")
    if not isinstance(raw_options, (list, tuple)):
        return ()
    options: list[_LiveOption] = []
    for raw_option in raw_options:
        option = _optional_mapping(raw_option)
        if option is None:
            continue
        action = option.get("action")
        if not isinstance(action, str) or not action:
            continue
        options.append(_LiveOption(action, option.get("is_sp") is True))
    return tuple(options)


def _training_options(
    training_output: Mapping[str, Any] | None,
    overview_by_action: Mapping[str, _LiveOption],
) -> tuple[_LiveOption, ...]:
    if training_output is None:
        return ()
    state = _optional_mapping(training_output.get("state"))
    if state is None:
        return ()
    raw_options = state.get("options")
    if not isinstance(raw_options, (list, tuple)):
        return ()
    options: list[_LiveOption] = []
    for raw_option in raw_options:
        option = _optional_mapping(raw_option)
        if option is None:
            continue
        attribute = option.get("attribute")
        if not isinstance(attribute, str):
            continue
        action = _ATTRIBUTE_TO_ACTION.get(attribute)
        if action is None:
            continue
        overview_option = overview_by_action.get(action)
        is_sp = (
            overview_option.is_sp
            if overview_option is not None
            else option.get("is_sp") is True
        )
        options.append(_LiveOption(action, is_sp))
    return tuple(options)


def _available_options(
    overview_output: Mapping[str, Any] | None,
    training_output: Mapping[str, Any] | None,
) -> tuple[_LiveOption, ...]:
    overview_options = _overview_options(overview_output)
    by_action = {option.action: option for option in overview_options}
    combined = [*overview_options, *_training_options(training_output, by_action)]
    deduplicated: dict[str, _LiveOption] = {}
    for option in combined:
        if option.action in _SHOP_ACTIONS:
            continue
        if (
            option.action not in _LESSON_ACTION_TO_ATTRIBUTE
            and option.action not in _KNOWN_NON_LESSON_ACTIONS
        ):
            continue
        previous = deduplicated.get(option.action)
        deduplicated[option.action] = _LiveOption(
            option.action,
            option.is_sp or (previous.is_sp if previous is not None else False),
        )
    return tuple(deduplicated.values())


def _preferred_action(
    overview_output: Mapping[str, Any] | None,
) -> str | None:
    if overview_output is None:
        return None
    decision = _optional_mapping(overview_output.get("decision"))
    recommended = (
        None if decision is None else _optional_mapping(decision.get("recommended"))
    )
    action = None if recommended is None else recommended.get("action")
    return action if isinstance(action, str) else None


def _route_option_allowed(option: _LiveOption, route_actions: set[str]) -> bool:
    if option.action == REST:
        return True
    if option.action in _LESSON_ACTION_TO_ATTRIBUTE:
        return bool({LESSON, CRAM_LESSON}.intersection(route_actions))
    return {
        ACTIVITY: SUPPLY,
        CLASS: ROUTE_CLASS,
        OUTING: OUTING,
    }.get(option.action) in route_actions


def _preferred_attribute(
    training_output: Mapping[str, Any] | None,
) -> str | None:
    if training_output is None:
        return None
    state = _optional_mapping(training_output.get("state"))
    attribute = None if state is None else state.get("recommended_attribute")
    if attribute in _ATTRIBUTE_TO_ACTION:
        return attribute
    policy_attribute = (
        None if state is None else state.get("policy_preferred_attribute")
    )
    return policy_attribute if policy_attribute in _ATTRIBUTE_TO_ACTION else None


def _step_direct_gain(step: object, field: str) -> int | None:
    lines = getattr(step, "lines", ())
    gains = [
        line.delta
        for line in lines
        if not line.is_triggered
        and line.line_type_name == field
        and line.delta > 0
    ]
    if not gains:
        return None
    total = sum(gains)
    return int(round(total))


def _historical_gain(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    field: str,
    is_sp: bool,
) -> _GainEstimate | None:
    suffix = "_sp" if is_sp else "_normal"
    comparable: list[tuple[bool, int, int, str]] = []
    marker = f"_{field}_"
    for step in snapshot.completed_steps:
        if (
            not step.step_type_name.startswith("lesson_")
            or not step.step_type_name.endswith(suffix)
        ):
            continue
        step_field = next(
            (
                candidate_field
                for _action, (_attribute, candidate_field) in
                _LESSON_ACTION_TO_ATTRIBUTE.items()
                if f"_{candidate_field}_" in step.step_type_name
            ),
            None,
        )
        # A lesson of another attribute can calibrate this run's tier, but use
        # that lesson's primary line rather than an incidental cross-stat line.
        gain = None if step_field is None else _step_direct_gain(step, step_field)
        if gain is None:
            continue
        comparable.append(
            (marker in step.step_type_name, step.log_index, gain, step.step_type_name)
        )
    if not comparable:
        return None
    exact = [item for item in comparable if item[0]]
    _same_attribute, log_index, gain, step_name = max(
        exact or comparable,
        key=lambda item: item[1],
    )
    return _GainEstimate(gain, f"outer-save:{step_name}@log{log_index}")


def _gain_estimate(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    field: str,
    is_sp: bool,
    policy: InitialRegularOuterPolicy,
) -> _GainEstimate:
    observed = _historical_gain(snapshot, field=field, is_sp=is_sp)
    if observed is not None:
        return observed
    fallback = policy.fallback_sp_gain if is_sp else policy.fallback_normal_gain
    tier = "sp" if is_sp else "normal"
    return _GainEstimate(fallback, f"policy-fallback:{tier}")


def _evaluate_lessons(
    snapshot: ProduceOuterLocalSaveSnapshot,
    overview_state: Mapping[str, Any],
    options: tuple[_LiveOption, ...],
    policy: InitialRegularOuterPolicy,
    *,
    attribute_cap: int,
) -> tuple[_LessonEvaluation, ...]:
    evaluations: list[_LessonEvaluation] = []
    for option in options:
        identity = _LESSON_ACTION_TO_ATTRIBUTE.get(option.action)
        if identity is None:
            continue
        attribute, field = identity
        current = _state_value(snapshot, overview_state, field)
        if current is None or current < 0:
            continue
        estimate = _gain_estimate(
            snapshot,
            field=field,
            is_sp=option.is_sp,
            policy=policy,
        )
        headroom = max(0, attribute_cap - current)
        effective = min(estimate.value, headroom)
        overflow = max(0, estimate.value - effective)
        evaluations.append(
            _LessonEvaluation(
                option=option,
                attribute=attribute,
                field=field,
                current=current,
                nominal_gain=estimate.value,
                effective_gain=effective,
                overflow_gain=overflow,
                predicted=current + effective,
                gain_source=estimate.source,
            )
        )
    return tuple(evaluations)


def _select_lesson(
    evaluations: tuple[_LessonEvaluation, ...],
    preferred_attribute: str | None,
) -> _LessonEvaluation | None:
    useful = tuple(item for item in evaluations if item.effective_gain > 0)
    if not useful:
        return None
    # A cap-safe SP is the first priority.  If every SP would overflow, compare
    # actual usable gain across all lessons so a nearly capped SP cannot waste
    # a whole week merely because it carries the badge.
    cap_safe_sp = tuple(
        item for item in useful if item.option.is_sp and item.overflow_gain == 0
    )
    pool = cap_safe_sp or useful
    return max(
        pool,
        key=lambda item: (
            item.effective_gain,
            item.option.is_sp,
            item.overflow_gain == 0,
            item.attribute == preferred_attribute,
            -item.current,
            item.option.action,
        ),
    )


def _rest_advice(
    *,
    current_hp: int,
    max_hp: int,
    plan: RegularStaminaPlan,
    rules: InitialRegularModeRules,
    route_source: str,
) -> InitialRegularOuterAdvice:
    recovery = max_hp * rules.rest_recovery_permille // 1000
    predicted_hp = min(max_hp, current_hp + recovery)
    current_cost = plan.current_action_hp_cost or plan.estimated_lesson_cost
    return InitialRegularOuterAdvice(
        status=STATUS_READY,
        action=REST,
        reason=(
            f"Rest at week {plan.current_week}: current HP {current_hp} is "
            f"below the {plan.required_stamina} HP required through the next "
            f"proven recovery/final horizon. Master recovery "
            f"{rules.rest_recovery_permille}‰ predicts {current_hp}->{predicted_hp} HP."
        ),
        predicted_hp=predicted_hp,
        attribute=None,
        current_attribute=None,
        predicted_attribute=None,
        nominal_attribute_gain=0,
        effective_attribute_gain=0,
        overflow_attribute_gain=0,
        is_sp=False,
        estimated_lesson_hp_cost=current_cost,
        reserved_hp_for_next_lesson=plan.required_stamina,
        gain_estimate_source=None,
        current_week=plan.current_week,
        weeks_until_final=plan.weeks_until_final,
        required_hp_for_horizon=plan.required_stamina,
        final_hp_reserve=plan.final_reserve,
        next_recovery_week=plan.next_recovery_week,
        lesson_weeks_before_recovery=plan.lesson_weeks_before_recovery,
        rest_recovery_permille=rules.rest_recovery_permille,
        route_budget_source=route_source,
    )


def _lesson_advice(
    selected: _LessonEvaluation,
    evaluations: tuple[_LessonEvaluation, ...],
    *,
    current_hp: int,
    plan: RegularStaminaPlan,
    rules: InitialRegularModeRules,
    route_source: str,
) -> InitialRegularOuterAdvice:
    lesson_cost = plan.current_action_hp_cost or plan.estimated_lesson_cost
    reserve = max(0, plan.required_stamina - lesson_cost)
    predicted_hp = max(0, current_hp - lesson_cost)
    tier = " SP" if selected.option.is_sp else ""
    clauses = [
        f"Selected {selected.attribute}{tier}: {selected.effective_gain} of "
        f"{selected.nominal_gain} estimated direct gain is effective "
        f"({selected.current}->{selected.predicted})."
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
        alternative_tier = " SP" if alternative.option.is_sp else ""
        clauses.append(
            f"Avoids {alternative.attribute}{alternative_tier}, which would "
            f"lose {alternative.overflow_gain} gain at the "
            f"{rules.attribute_cap} cap."
        )
    clauses.append(
        f"Estimated HP {current_hp}->{predicted_hp}; the full proven horizon "
        f"requires {plan.required_stamina} before this action and {reserve} "
        "after it."
    )
    return InitialRegularOuterAdvice(
        status=STATUS_READY,
        action=selected.option.action,
        reason=" ".join(clauses),
        predicted_hp=predicted_hp,
        attribute=selected.attribute,
        current_attribute=selected.current,
        predicted_attribute=selected.predicted,
        nominal_attribute_gain=selected.nominal_gain,
        effective_attribute_gain=selected.effective_gain,
        overflow_attribute_gain=selected.overflow_gain,
        is_sp=selected.option.is_sp,
        estimated_lesson_hp_cost=lesson_cost,
        reserved_hp_for_next_lesson=reserve,
        gain_estimate_source=selected.gain_source,
        current_week=plan.current_week,
        weeks_until_final=plan.weeks_until_final,
        required_hp_for_horizon=plan.required_stamina,
        final_hp_reserve=plan.final_reserve,
        next_recovery_week=plan.next_recovery_week,
        lesson_weeks_before_recovery=plan.lesson_weeks_before_recovery,
        rest_recovery_permille=rules.rest_recovery_permille,
        route_budget_source=route_source,
    )


def _non_lesson_advice(
    option: _LiveOption,
    *,
    current_hp: int,
    predicted_hp: int,
    plan: RegularStaminaPlan,
    rules: InitialRegularModeRules,
    route_source: str,
) -> InitialRegularOuterAdvice:
    return InitialRegularOuterAdvice(
        status=STATUS_READY,
        action=option.action,
        reason=(
            f"Selected the live reader's available {option.action} action; "
            "consultation/shop options were excluded and no lesson candidate "
            "was available."
        ),
        predicted_hp=predicted_hp,
        attribute=None,
        current_attribute=None,
        predicted_attribute=None,
        nominal_attribute_gain=0,
        effective_attribute_gain=0,
        overflow_attribute_gain=0,
        is_sp=False,
        estimated_lesson_hp_cost=plan.estimated_lesson_cost,
        reserved_hp_for_next_lesson=plan.required_stamina,
        gain_estimate_source=None,
        current_week=plan.current_week,
        weeks_until_final=plan.weeks_until_final,
        required_hp_for_horizon=plan.required_stamina,
        final_hp_reserve=plan.final_reserve,
        next_recovery_week=plan.next_recovery_week,
        lesson_weeks_before_recovery=plan.lesson_weeks_before_recovery,
        rest_recovery_permille=rules.rest_recovery_permille,
        route_budget_source=route_source,
    )


def _advise_visible_initial_mode(
    snapshot: ProduceOuterLocalSaveSnapshot,
    state: Mapping[str, Any],
    options: tuple[_LiveOption, ...],
    *,
    training_output: Mapping[str, Any] | None,
    current_hp: int,
    max_hp: int,
    rules: InitialRegularModeRules,
    policy: InitialRegularOuterPolicy,
) -> InitialRegularOuterAdvice:
    """Choose one visible Pro/Master action without inventing a week table."""

    lesson_cost = _lesson_cost(max_hp)
    safety_reserve = min(max_hp, max(LESSON_COST_FLOOR, ceil(max_hp * 0.20)))
    lessons = _evaluate_lessons(
        snapshot,
        state,
        options,
        policy,
        attribute_cap=rules.attribute_cap,
    )
    rest = next((value for value in options if value.action == REST), None)
    if lessons:
        if current_hp - lesson_cost < safety_reserve and rest is not None:
            recovery = max_hp * rules.rest_recovery_permille // 1000
            return InitialRegularOuterAdvice(
                status=STATUS_READY,
                action=REST,
                reason=(
                    f"Visible-action {rules.produce_id}: stamina {current_hp}/{max_hp} "
                    "is below the next-lesson reserve, so recover before training."
                ),
                predicted_hp=min(max_hp, current_hp + recovery),
                attribute=None,
                current_attribute=None,
                predicted_attribute=None,
                nominal_attribute_gain=0,
                effective_attribute_gain=0,
                overflow_attribute_gain=0,
                is_sp=False,
                estimated_lesson_hp_cost=lesson_cost,
                reserved_hp_for_next_lesson=safety_reserve,
                gain_estimate_source=None,
                rest_recovery_permille=rules.rest_recovery_permille,
                route_budget_source="live-visible-actions+Master ProduceSetting",
            )
        selected = _select_lesson(lessons, _preferred_attribute(training_output))
        if selected is not None:
            return InitialRegularOuterAdvice(
                status=STATUS_READY,
                action=selected.option.action,
                reason=(
                    f"Visible-action {rules.produce_id}: choose "
                    f"{selected.attribute}{' SP' if selected.option.is_sp else ''} "
                    f"with {selected.effective_gain} usable headroom toward the "
                    f"{rules.attribute_cap} cap."
                ),
                predicted_hp=max(0, current_hp - lesson_cost),
                attribute=selected.attribute,
                current_attribute=selected.current,
                predicted_attribute=selected.predicted,
                nominal_attribute_gain=selected.nominal_gain,
                effective_attribute_gain=selected.effective_gain,
                overflow_attribute_gain=selected.overflow_gain,
                is_sp=selected.option.is_sp,
                estimated_lesson_hp_cost=lesson_cost,
                reserved_hp_for_next_lesson=safety_reserve,
                gain_estimate_source=selected.gain_source,
                rest_recovery_permille=rules.rest_recovery_permille,
                route_budget_source="live-visible-actions+Master attribute cap",
            )

    non_rest = tuple(value for value in options if value.action != REST)
    if non_rest:
        preferred = _preferred_action({"decision": {}})
        selected = next(
            (value for value in non_rest if value.action == preferred), non_rest[0]
        )
        class_cost = min(max_hp, LESSON_COST_FLOOR) if selected.action == CLASS else 0
        if current_hp - class_cost < safety_reserve and rest is not None:
            selected = rest
            class_cost = 0
        return InitialRegularOuterAdvice(
            status=STATUS_READY,
            action=selected.action,
            reason=f"Visible-action {rules.produce_id}: choose available {selected.action}.",
            predicted_hp=max(0, current_hp - class_cost),
            attribute=None,
            current_attribute=None,
            predicted_attribute=None,
            nominal_attribute_gain=0,
            effective_attribute_gain=0,
            overflow_attribute_gain=0,
            is_sp=False,
            estimated_lesson_hp_cost=lesson_cost,
            reserved_hp_for_next_lesson=safety_reserve,
            gain_estimate_source=None,
            rest_recovery_permille=rules.rest_recovery_permille,
            route_budget_source="live-visible-actions",
        )
    if rest is not None:
        recovery = max_hp * rules.rest_recovery_permille // 1000
        return InitialRegularOuterAdvice(
            status=STATUS_READY,
            action=REST,
            reason=f"Visible-action {rules.produce_id}: Rest is the only usable tile.",
            predicted_hp=min(max_hp, current_hp + recovery),
            attribute=None,
            current_attribute=None,
            predicted_attribute=None,
            nominal_attribute_gain=0,
            effective_attribute_gain=0,
            overflow_attribute_gain=0,
            is_sp=False,
            estimated_lesson_hp_cost=lesson_cost,
            reserved_hp_for_next_lesson=safety_reserve,
            gain_estimate_source=None,
            rest_recovery_permille=rules.rest_recovery_permille,
            route_budget_source="live-visible-actions",
        )
    return _unavailable("No usable Pro/Master action is visible.", predicted_hp=current_hp)


def advise_initial_regular_outer(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    overview_output: Mapping[str, object] | None = None,
    training_output: Mapping[str, object] | None = None,
    policy: InitialRegularOuterPolicy = InitialRegularOuterPolicy(),
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> InitialRegularOuterAdvice:
    """Return one Initial Regular outer action without capturing or clicking.

    ``overview_output`` and ``training_output`` are passed through unchanged
    from :func:`gkms_tool.live_source.read_live_overview_decision` and
    :func:`gkms_tool.live_source.read_live_training_choice`, respectively.
    Either may be omitted, but at least one must expose a usable option.
    """

    if not isinstance(snapshot, ProduceOuterLocalSaveSnapshot):
        raise TypeError("snapshot must be ProduceOuterLocalSaveSnapshot")
    if overview_output is not None:
        overview_output = _mapping(overview_output, "overview_output")
    if training_output is not None:
        training_output = _mapping(training_output, "training_output")
    _validate_policy(policy)

    options = _available_options(overview_output, training_output)
    if not options:
        return _unavailable(
            "No usable live action remains after consultation/shop filtering."
        )

    state = _overview_state(overview_output)
    current_hp = _state_value(snapshot, state, "stamina")
    max_hp = _maximum_hp(snapshot, state, current_hp)
    if current_hp is None or max_hp is None or max_hp <= 0:
        return _unavailable(
            "HP/max-HP evidence is unavailable; no stamina-sensitive action "
            "can be recommended."
        )
    directory = Path(master_dir).resolve()
    route_mapping = (
        None
        if overview_output is None
        else _optional_mapping(overview_output.get("route"))
    )
    route_produce_id = (
        None if route_mapping is None else route_mapping.get("produce_id")
    )
    if route_produce_id not in {"produce-001", "produce-002", "produce-003"}:
        return _unavailable(
            f"Initial mode route is unavailable: {route_produce_id!r}.",
            predicted_hp=current_hp,
        )
    try:
        rules = load_initial_regular_mode_rules(
            str(route_produce_id), master_dir=directory
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError) as error:
        return _unavailable(
            f"Initial Regular Master rules are unavailable: "
            f"{type(error).__name__}: {error}",
            predicted_hp=current_hp,
        )
    plan, route_source = _route_stamina_plan(
        snapshot,
        overview_output,
        state,
        current_hp=current_hp,
        max_hp=max_hp,
        rules=rules,
        master_dir=directory,
    )
    # Pro/Master schedules are account/server supplied.  The current screen
    # is enough to choose the visible lesson direction; do not require a
    # fabricated full-week calendar before making that reversible choice.
    if plan is None and rules.produce_id != "produce-001":
        return _advise_visible_initial_mode(
            snapshot,
            state,
            options,
            training_output=training_output,
            current_hp=current_hp,
            max_hp=max_hp,
            rules=rules,
            policy=policy,
        )
    if plan is None:
        return _unavailable(
            route_source,
            predicted_hp=current_hp,
            lesson_cost=_lesson_cost(max_hp),
        )
    if overview_output is None:
        return _unavailable(
            "Exact live overview evidence disappeared after route planning.",
            predicted_hp=current_hp,
            lesson_cost=plan.estimated_lesson_cost,
            reserve=plan.required_stamina,
        )
    route_mapping = _optional_mapping(overview_output.get("route"))
    if route_mapping is None:
        return _unavailable(
            "Exact live route evidence disappeared after route planning.",
            predicted_hp=current_hp,
            lesson_cost=plan.estimated_lesson_cost,
            reserve=plan.required_stamina,
        )
    raw_route_actions = route_mapping.get("route_actions")
    if not isinstance(raw_route_actions, (list, tuple)) or any(
        not isinstance(action, str) for action in raw_route_actions
    ):
        return _unavailable(
            "Exact live route actions disappeared after route planning.",
            predicted_hp=current_hp,
            lesson_cost=plan.estimated_lesson_cost,
            reserve=plan.required_stamina,
        )
    route_actions = set(raw_route_actions)
    options = tuple(
        option for option in options if _route_option_allowed(option, route_actions)
    )
    if not options:
        return _unavailable(
            "No visible option is compatible with the exact current route week.",
            predicted_hp=current_hp,
            lesson_cost=plan.estimated_lesson_cost,
            reserve=plan.required_stamina,
        )

    lessons = _evaluate_lessons(
        snapshot,
        state,
        options,
        policy,
        attribute_cap=rules.attribute_cap,
    )
    rest_visible = any(option.action == REST for option in options)

    if lessons:
        lesson_cost = plan.current_action_hp_cost
        if lesson_cost <= 0:
            return _unavailable(
                "Visible lesson options disagree with the exact current route week.",
                predicted_hp=current_hp,
                lesson_cost=plan.estimated_lesson_cost,
                reserve=plan.required_stamina,
            )
        forced_lesson = CRAM_LESSON in route_actions and not rest_visible
        if current_hp < plan.required_stamina and not forced_lesson:
            if rest_visible:
                return _rest_advice(
                    current_hp=current_hp,
                    max_hp=max_hp,
                    plan=plan,
                    rules=rules,
                    route_source=route_source,
                )
            return _unavailable(
                f"Even an SP lesson would spend HP reserved for later lessons/"
                f"final: current {current_hp}, required {plan.required_stamina}; "
                "Rest is not a visible option.",
                predicted_hp=current_hp,
                lesson_cost=lesson_cost,
                reserve=plan.required_stamina,
            )
        selected = _select_lesson(
            lessons,
            _preferred_attribute(training_output),
        )
        if selected is None:
            return _unavailable(
                f"All visible lesson attributes are already at the "
                f"{rules.attribute_cap} cap.",
                predicted_hp=current_hp,
                lesson_cost=lesson_cost,
                reserve=plan.required_stamina,
            )
        return _lesson_advice(
            selected,
            lessons,
            current_hp=current_hp,
            plan=plan,
            rules=rules,
            route_source=route_source,
        )

    non_lessons = tuple(
        option for option in options if option.action in _KNOWN_NON_LESSON_ACTIONS
    )
    if not non_lessons:
        return _unavailable("No usable live action is available.")
    remaining_requirement = max(
        0,
        plan.required_stamina - plan.current_action_hp_cost,
    )
    if current_hp < remaining_requirement and rest_visible:
        return _rest_advice(
            current_hp=current_hp,
            max_hp=max_hp,
            plan=plan,
            rules=rules,
            route_source=route_source,
        )
    non_rest = tuple(option for option in non_lessons if option.action != REST)
    if not non_rest:
        return _unavailable(
            "Rest is visible, but the proven route HP budget does not require "
            "it and no other usable action is available.",
            predicted_hp=current_hp,
        )
    preferred = _preferred_action(overview_output)
    selected_non_lesson = next(
        (option for option in non_rest if option.action == preferred),
        non_rest[0],
    )
    class_cost = min(max_hp, LESSON_COST_FLOOR)
    selected_cost = class_cost if selected_non_lesson.action == CLASS else 0
    predicted_hp = max(0, current_hp - selected_cost)
    if predicted_hp < remaining_requirement:
        if rest_visible:
            return _rest_advice(
                current_hp=current_hp,
                max_hp=max_hp,
                plan=plan,
                rules=rules,
                route_source=route_source,
            )
        return _unavailable(
            f"{selected_non_lesson.action} would leave {predicted_hp} HP below "
            f"the {remaining_requirement} HP remaining route requirement; "
            "Rest is not visible.",
            predicted_hp=current_hp,
            lesson_cost=plan.estimated_lesson_cost,
            reserve=remaining_requirement,
        )
    return _non_lesson_advice(
        selected_non_lesson,
        current_hp=current_hp,
        predicted_hp=predicted_hp,
        plan=plan,
        rules=rules,
        route_source=route_source,
    )


def read_current_initial_regular_outer_advice(
    *,
    overview_output: Mapping[str, object] | None = None,
    training_output: Mapping[str, object] | None = None,
    game_root: str | Path = DEFAULT_PC_GAME_ROOT,
    policy: InitialRegularOuterPolicy = InitialRegularOuterPolicy(),
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> InitialRegularOuterAdvice:
    """Read the current outer save, then apply the same pure advice policy.

    The caller supplies an already-produced live-reader result.  This wrapper
    reads local files only; it does not invoke either live reader and does not
    use the click payload those readers include.
    """

    snapshot = read_current_produce_outer_local_save(game_root)
    return advise_initial_regular_outer(
        snapshot,
        overview_output=overview_output,
        training_output=training_output,
        policy=policy,
        master_dir=master_dir,
    )


__all__ = [
    "ATTRIBUTE_CAP",
    "InitialRegularModeRules",
    "InitialRegularOuterAdvice",
    "InitialRegularOuterPolicy",
    "REST_RECOVERY_PERMIL",
    "SCHEMA_NAME",
    "STATUS_READY",
    "STATUS_UNAVAILABLE",
    "UNAVAILABLE_ACTION",
    "advise_initial_regular_outer",
    "load_initial_regular_mode_rules",
    "read_current_initial_regular_outer_advice",
]
