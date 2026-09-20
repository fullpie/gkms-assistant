"""Master-backed Initial Regular lesson branch catalog.

This module deliberately separates two different claims:

* a lesson *stage catalog* can be proven from the route and local Master; and
* an executable weekly branch also needs a compatible inner simulator and
  runtime-owned chance/loadout/result inputs.

The first complete route scope is Fujita Kotone (``fktn``) in
``produce-001``.  Its lesson-level IDs contain a ``plan2`` *tuning profile*,
but that token is not the producing idol card's plan.  Inner-simulator routing
must come from the selected ``IdolCard.planType`` row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
import re
from typing import Mapping, Sequence

import yaml

from .initial_regular_offline_search import (
    InitialRegularOfflineBranchExpansion,
    InitialRegularWeightedInnerBranch,
    InitialRegularWeightedWeeklyBranch,
)
from .initial_regular_inner_protocol import (
    build_initial_regular_lesson_stage_request,
)
from .initial_regular_outer_advisor import load_initial_regular_mode_rules
from .initial_regular_rollout import INITIAL_REGULAR_PRODUCE_ID
from .initial_regular_weekly_adapter import (
    InitialRegularLessonAttribute,
    InitialRegularLessonResolutionInput,
    InitialRegularWeeklyLessonNode,
    InitialRegularWeeklyPause,
    open_initial_regular_weekly_request,
)
from .loadout_runtime_bridge import PreparedLoadoutRuntime
from .passive_runtime import PassiveRuntimeResult, RunModifiers
from .plan3_engine import (
    DEFAULT_MASTER_DIR,
    LESSON_DANCE,
    LESSON_VISUAL,
    LESSON_VOCAL,
)
from .plan3_exam_start import (
    Plan3ExamStartChance,
    Plan3ExamStartContext,
    Plan3ExamStartInputs,
    Plan3ExamStartLoadout,
)
from .plan3_rollout_inner_adapter import Plan3InnerPostExamResolution
from .produce_rollout import ChanceBranch, ExternalRequest, ProduceRolloutState
from .produce_rollout_expectimax import OuterSearchIssue
from .route_calendar import CRAM_LESSON, LESSON, load_route_calendar


_SUPPORTED_CHARACTER_ID = "fktn"
_NORMAL_WEEK_INDEX = {1: 1, 3: 2, 9: 3, 10: 4}
_HARD_WEEK_INDEX = {5: 1, 12: 2}
_LESSON_PROFILE_FROM_LEVEL_ID = re.compile(
    r"^p_step_lesson_level-\d+-(?P<profile>plan\d+)-"
)
_IDOL_PLAN_TYPES = {
    "ProducePlanType_Plan1": "plan1",
    "ProducePlanType_Plan2": "plan2",
    "ProducePlanType_Plan3": "plan3",
}

_ATTRIBUTE_MASTER_CODE = {
    InitialRegularLessonAttribute.VOCAL: "vo",
    InitialRegularLessonAttribute.DANCE: "da",
    InitialRegularLessonAttribute.VISUAL: "vi",
}
_ATTRIBUTE_CONTEXT = {
    InitialRegularLessonAttribute.VOCAL: (1, LESSON_VOCAL, 1, 2, 3),
    InitialRegularLessonAttribute.DANCE: (2, LESSON_DANCE, 4, 5, 6),
    InitialRegularLessonAttribute.VISUAL: (3, LESSON_VISUAL, 7, 8, 9),
}


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularLessonCatalogIssue:
    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.code or not self.field:
            raise ValueError("catalog issue code and field must be non-empty")


@dataclass(frozen=True, slots=True)
class InitialRegularSpRatePermils:
    """Exact pre-runtime SP occurrence rates, in permille."""

    vocal: int
    dance: int
    visual: int

    def __post_init__(self) -> None:
        for name in ("vocal", "dance", "visual"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} SP rate must be a non-negative integer")

    def for_attribute(self, attribute: InitialRegularLessonAttribute) -> int:
        return int(getattr(self, attribute.value))


@dataclass(frozen=True, slots=True)
class InitialRegularLessonRuntimeContext:
    """Runtime values absent from the outer state and lesson Master rows."""

    lesson_limit_up_score: int
    exam_extra_turn: int
    battle_bonus_permille: tuple[int, int, int]
    gimmick_group_id: str

    def __post_init__(self) -> None:
        for name in ("lesson_limit_up_score", "exam_extra_turn"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        bonuses = tuple(self.battle_bonus_permille)
        if len(bonuses) != 3 or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in bonuses
        ):
            raise ValueError("battle_bonus_permille must contain three integers")
        object.__setattr__(self, "battle_bonus_permille", bonuses)
        if not isinstance(self.gimmick_group_id, str):
            raise ValueError("gimmick_group_id must be text")


@dataclass(frozen=True, slots=True)
class InitialRegularLessonStageFact:
    """One exact Master lesson row plus its outer-state start facts."""

    stage_id: str
    lesson_level_id: str
    lesson_profile: str
    plan_type: str
    setting_id: str
    attribute: InitialRegularLessonAttribute
    variant: str
    is_sp: bool
    is_hard: bool
    current_parameter_type: int
    lesson_type: str
    step_type_value: int
    base_turns: int
    base_clear_target: int
    base_limit_target: int
    start_stamina: int
    max_stamina: int
    current_attribute: int
    attribute_limit: int
    clear_border: int
    runtime_turns: int | None = None
    limit_border: int | None = None
    runtime_context: InitialRegularLessonRuntimeContext | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if self.variant not in {"normal", "sp", "hard"}:
            raise ValueError("lesson stage variant must be normal, sp, or hard")
        if self.is_sp != (self.variant == "sp"):
            raise ValueError("stage is_sp disagrees with variant")
        if self.is_hard != (self.variant == "hard"):
            raise ValueError("stage is_hard disagrees with variant")
        if (
            not self.stage_id
            or not self.lesson_level_id
            or not self.lesson_profile
            or not self.plan_type
        ):
            raise ValueError("stage identity, lesson profile, and plan type are required")
        if self.base_turns < 1:
            raise ValueError("base turns must be positive")
        if not 0 < self.base_clear_target < self.base_limit_target:
            raise ValueError("lesson target ordering is invalid")
        if not 0 <= self.start_stamina <= self.max_stamina:
            raise ValueError("stage stamina is outside 0..max_stamina")
        if not 0 <= self.current_attribute <= self.attribute_limit:
            raise ValueError("stage attribute is outside its limit")
        if not 0 <= self.clear_border <= self.base_clear_target:
            raise ValueError("clear border is outside the Master target")
        if (self.runtime_turns is None) != (self.limit_border is None):
            raise ValueError("runtime turns and limit border resolve together")

    @property
    def branch_key(self) -> str:
        return f"{self.attribute.value}:{self.variant}:{self.stage_id}"

    @property
    def context_ready(self) -> bool:
        return self.runtime_turns is not None and self.limit_border is not None


@dataclass(frozen=True, slots=True)
class InitialRegularWeightedLessonStage:
    probability: Fraction
    stage: InitialRegularLessonStageFact

    def __post_init__(self) -> None:
        if isinstance(self.probability, bool) or not isinstance(
            self.probability, Fraction
        ):
            raise TypeError("lesson probability must be fractions.Fraction")
        if not 0 < self.probability <= 1:
            raise ValueError("lesson probability must be in (0, 1]")
        if not isinstance(self.stage, InitialRegularLessonStageFact):
            raise TypeError("stage must be InitialRegularLessonStageFact")


@dataclass(frozen=True, slots=True)
class InitialRegularLessonAttributeCatalog:
    attribute: InitialRegularLessonAttribute
    fixed_stage: InitialRegularLessonStageFact
    sp_candidates: tuple[InitialRegularLessonStageFact, ...]
    selected_sp_stage: InitialRegularLessonStageFact | None
    sp_probability: Fraction | None
    frontier: tuple[InitialRegularWeightedLessonStage, ...]

    def __post_init__(self) -> None:
        candidates = tuple(self.sp_candidates)
        frontier = tuple(self.frontier)
        object.__setattr__(self, "sp_candidates", candidates)
        object.__setattr__(self, "frontier", frontier)
        if self.fixed_stage.attribute is not self.attribute:
            raise ValueError("fixed lesson stage attribute mismatch")
        if any(value.attribute is not self.attribute for value in candidates):
            raise ValueError("SP candidate attribute mismatch")
        if self.selected_sp_stage is not None and self.selected_sp_stage not in candidates:
            raise ValueError("selected SP stage is not a catalog candidate")
        if self.sp_probability is not None and not 0 <= self.sp_probability <= 1:
            raise ValueError("SP probability must be in [0, 1]")
        if frontier and sum(
            (value.probability for value in frontier), Fraction()
        ) != Fraction(1):
            raise ValueError("attribute lesson frontier must sum exactly to one")


@dataclass(frozen=True, slots=True)
class InitialRegularLessonBranchCatalog:
    outer_state: ProduceRolloutState
    external_request: ExternalRequest
    node: InitialRegularWeeklyLessonNode | None
    action_family: str
    attributes: tuple[InitialRegularLessonAttributeCatalog, ...]
    issues: tuple[InitialRegularLessonCatalogIssue, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", tuple(self.attributes))
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def stage_facts_ready(self) -> bool:
        return len(self.attributes) == 3

    @property
    def probability_frontiers_ready(self) -> bool:
        return self.stage_facts_ready and all(value.frontier for value in self.attributes)

    @property
    def plan_types(self) -> tuple[str, ...]:
        values: list[str] = []
        for catalog in self.attributes:
            for stage in (catalog.fixed_stage, *catalog.sp_candidates):
                if stage.plan_type not in values:
                    values.append(stage.plan_type)
        return tuple(values)

    def for_attribute(
        self, attribute: InitialRegularLessonAttribute | str
    ) -> InitialRegularLessonAttributeCatalog:
        resolved = InitialRegularLessonAttribute(attribute)
        for value in self.attributes:
            if value.attribute is resolved:
                return value
        raise KeyError(f"lesson attribute is unavailable: {resolved.value}")


@dataclass(frozen=True, slots=True)
class InitialRegularLessonBranchExecution:
    branch_key: str
    chance: Plan3ExamStartChance | None
    post_exam_resolution: Plan3InnerPostExamResolution | None

    def __post_init__(self) -> None:
        if not self.branch_key:
            raise ValueError("branch_key is required")
        if self.chance is not None and not isinstance(
            self.chance, Plan3ExamStartChance
        ):
            raise TypeError("chance must be Plan3ExamStartChance or None")
        if self.post_exam_resolution is not None and not isinstance(
            self.post_exam_resolution, Plan3InnerPostExamResolution
        ):
            raise TypeError(
                "post_exam_resolution must be Plan3InnerPostExamResolution or None"
            )


@dataclass(frozen=True, slots=True)
class InitialRegularLessonExecutionInputs:
    branches: tuple[InitialRegularLessonBranchExecution, ...]
    loadout: Plan3ExamStartLoadout | None
    start_turn_effects_known_absent: bool | None

    def __post_init__(self) -> None:
        branches = tuple(self.branches)
        object.__setattr__(self, "branches", branches)
        if not all(isinstance(value, InitialRegularLessonBranchExecution) for value in branches):
            raise TypeError("branches must contain InitialRegularLessonBranchExecution")
        keys = tuple(value.branch_key for value in branches)
        if len(set(keys)) != len(keys):
            raise ValueError("execution branch keys must be unique")
        if self.loadout is not None and not isinstance(
            self.loadout, Plan3ExamStartLoadout
        ):
            raise TypeError("loadout must be Plan3ExamStartLoadout or None")
        if self.start_turn_effects_known_absent is not None and not isinstance(
            self.start_turn_effects_known_absent, bool
        ):
            raise TypeError("start_turn_effects_known_absent must be bool or None")

    @property
    def branch_map(self) -> dict[str, InitialRegularLessonBranchExecution]:
        return {value.branch_key: value for value in self.branches}


@dataclass(frozen=True, slots=True)
class InitialRegularInnerLessonBranchExecution:
    """Plan-neutral chance root for one catalog-owned lesson branch.

    The plan adapter owns ordered-zone evidence, compiled card programs, and
    loadout runtimes.  The outer catalog owns only the branch identity and the
    native RNG value selected for this exact stage.
    """

    branch_key: str
    rng_before: int | None
    rng_token: str | None = None

    def __post_init__(self) -> None:
        if not self.branch_key:
            raise ValueError("branch_key is required")
        if self.rng_before is not None and (
            isinstance(self.rng_before, bool)
            or not isinstance(self.rng_before, int)
            or not 0 <= self.rng_before <= 0xFFFFFFFF
        ):
            raise ValueError("rng_before must be UInt32 or None")
        if self.rng_token is not None and not self.rng_token:
            raise ValueError("rng_token must be non-empty text or None")


@dataclass(frozen=True, slots=True)
class InitialRegularInnerLessonExecutionInputs:
    """Inputs required to construct common lesson requests.

    This object deliberately contains no Plan2/Plan3 runtime class.  Those
    stay captured by the registered plan adapter and are resolved by stage ID.
    """

    idol_card_id: str | None
    branches: tuple[InitialRegularInnerLessonBranchExecution, ...]

    def __post_init__(self) -> None:
        if self.idol_card_id is not None and not self.idol_card_id:
            raise ValueError("idol_card_id must be non-empty text or None")
        branches = tuple(self.branches)
        object.__setattr__(self, "branches", branches)
        if not all(
            isinstance(value, InitialRegularInnerLessonBranchExecution)
            for value in branches
        ):
            raise TypeError(
                "branches must contain InitialRegularInnerLessonBranchExecution"
            )
        keys = tuple(value.branch_key for value in branches)
        if len(set(keys)) != len(keys):
            raise ValueError("inner execution branch keys must be unique")

    @property
    def branch_map(self) -> dict[str, InitialRegularInnerLessonBranchExecution]:
        return {value.branch_key: value for value in self.branches}


def _issue(code: str, field: str, detail: str = "") -> InitialRegularLessonCatalogIssue:
    return InitialRegularLessonCatalogIssue(code, field, detail)


def _master_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    if not path.is_file():
        raise FileNotFoundError(f"Master file is missing: {path}")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list) or not all(isinstance(row, Mapping) for row in payload):
        raise ValueError(f"{path.name} must contain a list of objects")
    return tuple(payload)


def _unique_row(
    rows: Sequence[Mapping[str, object]], row_id: str, label: str
) -> Mapping[str, object]:
    selected = [row for row in rows if row.get("id") == row_id]
    if len(selected) != 1:
        raise KeyError(f"{label} must resolve exactly once: {row_id}")
    return selected[0]


def _positive_int(row: Mapping[str, object], field_name: str, row_id: str) -> int:
    value = row.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"invalid {field_name}: {row_id}")
    return value


def _setting_id(master_dir: Path) -> str:
    row = _unique_row(
        _master_rows(master_dir / "Produce.yaml"),
        INITIAL_REGULAR_PRODUCE_ID,
        "Produce",
    )
    value = row.get("examSettingId")
    if not isinstance(value, str) or not value:
        raise ValueError("produce-001 examSettingId is missing")
    _unique_row(_master_rows(master_dir / "ExamSetting.yaml"), value, "ExamSetting")
    return value


def _idol_card_plan_type(
    master_dir: Path,
    idol_card_id: str,
    *,
    character_id: str,
) -> str:
    row = _unique_row(
        _master_rows(master_dir / "IdolCard.yaml"), idol_card_id, "IdolCard"
    )
    row_character = row.get("characterId")
    if row_character != character_id:
        raise ValueError(
            f"IdolCard character mismatch: {idol_card_id}:{row_character!r}"
        )
    raw_plan = row.get("planType")
    try:
        return _IDOL_PLAN_TYPES[str(raw_plan)]
    except KeyError as error:
        raise ValueError(
            f"unsupported IdolCard planType: {idol_card_id}:{raw_plan!r}"
        ) from error


def _runtime_modifiers(
    runtime: PassiveRuntimeResult | PreparedLoadoutRuntime | RunModifiers | None,
) -> RunModifiers | None:
    if runtime is None:
        return None
    if isinstance(runtime, RunModifiers):
        return runtime
    if isinstance(runtime, PreparedLoadoutRuntime):
        runtime.runtime.require_supported()
        return runtime.runtime.modifiers
    if isinstance(runtime, PassiveRuntimeResult):
        runtime.require_supported()
        return runtime.modifiers
    raise TypeError(
        "passive_runtime must be PreparedLoadoutRuntime, PassiveRuntimeResult, "
        "RunModifiers, or None"
    )


def _sp_rate(
    base: InitialRegularSpRatePermils,
    modifiers: RunModifiers,
    attribute: InitialRegularLessonAttribute,
) -> int:
    specific = {
        InitialRegularLessonAttribute.VOCAL: (
            modifiers.lesson_vocal_sp_change_rate_permil_addition
        ),
        InitialRegularLessonAttribute.DANCE: (
            modifiers.lesson_dance_sp_change_rate_permil_addition
        ),
        InitialRegularLessonAttribute.VISUAL: (
            modifiers.lesson_visual_sp_change_rate_permil_addition
        ),
    }[attribute]
    return (
        base.for_attribute(attribute)
        + modifiers.lesson_sp_change_rate_permil_addition
        + specific
    )


def _stage_fact(
    *,
    state: ProduceRolloutState,
    lesson_row: Mapping[str, object],
    level_rows: Sequence[Mapping[str, object]],
    setting_id: str,
    plan_type: str,
    attribute: InitialRegularLessonAttribute,
    variant: str,
    attribute_limit: int,
    runtime_context: InitialRegularLessonRuntimeContext | None,
) -> InitialRegularLessonStageFact:
    stage_id = lesson_row.get("id")
    level_id = lesson_row.get("produceStepLessonLevelId")
    if not isinstance(stage_id, str) or not stage_id:
        raise ValueError("lesson stage ID is missing")
    if not isinstance(level_id, str) or not level_id:
        raise ValueError(f"lesson level ID is missing: {stage_id}")
    match = _LESSON_PROFILE_FROM_LEVEL_ID.match(level_id)
    if match is None:
        raise ValueError(f"lesson tuning profile is not encoded: {level_id}")
    level = _unique_row(level_rows, level_id, "ProduceStepLessonLevel")
    turns = _positive_int(level, "limitTurn", level_id)
    clear = _positive_int(level, "successThreshold", level_id)
    limit = _positive_int(level, "resultTargetValueLimit", level_id)
    if clear >= limit:
        raise ValueError(f"lesson targets are invalid: {level_id}")

    parameter_type, lesson_type, normal_step, sp_step, hard_step = _ATTRIBUTE_CONTEXT[
        attribute
    ]
    step_type_value = {
        "normal": normal_step,
        "sp": sp_step,
        "hard": hard_step,
    }[variant]
    current = int(getattr(state.attributes, attribute.value))
    remaining = attribute_limit - current
    if remaining < 0:
        raise ValueError("outer attribute exceeds Initial Regular Master cap")
    clear_border = min(clear, remaining)

    runtime_turns: int | None = None
    limit_border: int | None = None
    if runtime_context is not None:
        runtime_turns = turns + runtime_context.exam_extra_turn
        base_limit = limit + runtime_context.lesson_limit_up_score
        if variant != "hard":
            limit_border = min(base_limit, remaining)
        else:
            remainders = {
                candidate: 1000 - int(getattr(state.attributes, candidate.value))
                for candidate in InitialRegularLessonAttribute
            }
            remainders[attribute] -= clear_border
            adjusted_master_limit = base_limit + clear_border - clear
            three_parameter_limit = 3 * max(remainders.values()) + clear_border
            limit_border = min(adjusted_master_limit, three_parameter_limit)

    return InitialRegularLessonStageFact(
        stage_id=stage_id,
        lesson_level_id=level_id,
        lesson_profile=match.group("profile"),
        plan_type=plan_type,
        setting_id=setting_id,
        attribute=attribute,
        variant=variant,
        is_sp=variant == "sp",
        is_hard=variant == "hard",
        current_parameter_type=parameter_type,
        lesson_type=lesson_type,
        step_type_value=step_type_value,
        base_turns=turns,
        base_clear_target=clear,
        base_limit_target=limit,
        start_stamina=state.stamina,
        max_stamina=state.max_stamina,
        current_attribute=current,
        attribute_limit=attribute_limit,
        clear_border=clear_border,
        runtime_turns=runtime_turns,
        limit_border=limit_border,
        runtime_context=runtime_context,
    )


def build_initial_regular_lesson_branch_catalog(
    state: ProduceRolloutState,
    request: ExternalRequest,
    *,
    idol_card_id: str | None = None,
    base_sp_rate_permils: InitialRegularSpRatePermils | None = None,
    passive_runtime: PassiveRuntimeResult | PreparedLoadoutRuntime | RunModifiers | None = None,
    sp_modifiers_known_absent: bool | None = None,
    selected_sp_stage_ids: Mapping[InitialRegularLessonAttribute | str, str]
    | None = None,
    runtime_context: InitialRegularLessonRuntimeContext | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> InitialRegularLessonBranchCatalog:
    """Build the maximum proven fktn/produce-001 lesson catalog.

    Vo/Da/Vi are three player decisions, so each attribute receives its own
    exact probability frontier.  The function never invents weights between
    attributes.  Standard lesson SP row identity and the live base SP rate
    remain explicit typed inputs because neither is selected by static Master.
    The selected IdolCard is explicit too: fktn has cards in several plans,
    and the lesson tuning-profile ID is not plan authority.
    """

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    directory = Path(master_dir).resolve()
    opened = open_initial_regular_weekly_request(state, request, master_dir=directory)
    if isinstance(opened, InitialRegularWeeklyPause):
        return InitialRegularLessonBranchCatalog(
            state,
            request,
            None,
            request.action_id,
            (),
            tuple(
                _issue(f"weekly:{value.code}", value.field, value.detail)
                for value in opened.issues
            ),
        )
    if not isinstance(opened, InitialRegularWeeklyLessonNode):
        return InitialRegularLessonBranchCatalog(
            state,
            request,
            None,
            request.action_id,
            (),
            (_issue("lesson-request-required", "request.action_id"),),
        )

    issues: list[InitialRegularLessonCatalogIssue] = []
    if state.mode_id != INITIAL_REGULAR_PRODUCE_ID:
        issues.append(_issue("unsupported-mode", "state.mode_id", state.mode_id))
    if state.character_id != _SUPPORTED_CHARACTER_ID:
        issues.append(
            _issue("unsupported-character", "state.character_id", state.character_id)
        )
    actual_plan_type = "unknown"
    if idol_card_id is None:
        issues.append(
            _issue(
                "idol-card-id-required",
                "idol_card_id",
                "selected IdolCard is required to resolve authoritative planType",
            )
        )
    elif not isinstance(idol_card_id, str) or not idol_card_id:
        raise ValueError("idol_card_id must be non-empty text or None")
    else:
        try:
            actual_plan_type = _idol_card_plan_type(
                directory, idol_card_id, character_id=state.character_id
            )
        except KeyError as error:
            issues.append(_issue("idol-card-not-found", "idol_card_id", str(error)))
        except ValueError as error:
            issues.append(_issue("idol-card-plan-invalid", "idol_card_id", str(error)))
    calendar = load_route_calendar(
        INITIAL_REGULAR_PRODUCE_ID,
        character_id=_SUPPORTED_CHARACTER_ID,
        master_dir=directory,
    )
    route_week = calendar.week(request.week)
    mode_rules = load_initial_regular_mode_rules(master_dir=directory)
    if mode_rules.total_steps != calendar.total_weeks:
        issues.append(
            _issue(
                "mode-route-total-mismatch",
                "mode_rules.total_steps",
                f"{mode_rules.total_steps}!={calendar.total_weeks}",
            )
        )
    if not route_week.exact_actions or request.action_id not in route_week.actions:
        issues.append(_issue("route-action-unproven", "request.action_id"))
    if request.action_id == LESSON and request.week not in _NORMAL_WEEK_INDEX:
        issues.append(_issue("lesson-week-unmapped", "request.week", str(request.week)))
    if request.action_id == CRAM_LESSON and request.week not in _HARD_WEEK_INDEX:
        issues.append(_issue("cram-week-unmapped", "request.week", str(request.week)))
    if issues:
        return InitialRegularLessonBranchCatalog(
            state, request, opened, request.action_id, (), tuple(issues)
        )

    if runtime_context is None:
        issues.append(
            _issue(
                "lesson-runtime-context-required",
                "runtime_context",
                "lessonLimitUpScore/examExtraTurn/battle bonus/gimmick are runtime-owned",
            )
        )
    if sp_modifiers_known_absent is not None and not isinstance(
        sp_modifiers_known_absent, bool
    ):
        raise TypeError("sp_modifiers_known_absent must be bool or None")
    modifiers = _runtime_modifiers(passive_runtime)
    if (
        isinstance(passive_runtime, PreparedLoadoutRuntime)
        and passive_runtime.session.produce_id != state.mode_id
    ):
        issues.append(
            _issue(
                "loadout-runtime-mode-mismatch",
                "passive_runtime.session.produce_id",
                passive_runtime.session.produce_id,
            )
        )
        modifiers = None
    if passive_runtime is not None and sp_modifiers_known_absent is True:
        raise ValueError(
            "passive_runtime and sp_modifiers_known_absent=True are contradictory"
        )

    selected_ids: dict[InitialRegularLessonAttribute, str] = {}
    for raw_attribute, stage_id in (selected_sp_stage_ids or {}).items():
        attribute = InitialRegularLessonAttribute(raw_attribute)
        if not isinstance(stage_id, str) or not stage_id:
            raise ValueError("selected SP stage IDs must be non-empty text")
        selected_ids[attribute] = stage_id

    lesson_rows = _master_rows(directory / "ProduceStepLesson.yaml")
    level_rows = _master_rows(directory / "ProduceStepLessonLevel.yaml")
    setting_id = _setting_id(directory)
    result: list[InitialRegularLessonAttributeCatalog] = []

    if request.action_id == LESSON:
        if base_sp_rate_permils is None:
            issues.append(_issue("sp-base-rate-required", "base_sp_rate_permils"))
        if modifiers is None and sp_modifiers_known_absent is not True:
            issues.append(
                _issue(
                    "sp-runtime-modifiers-required",
                    "passive_runtime",
                    "supply supported passive runtime or explicitly prove modifiers absent",
                )
            )
        normal_index = _NORMAL_WEEK_INDEX[request.week]
        for attribute in InitialRegularLessonAttribute:
            code = _ATTRIBUTE_MASTER_CODE[attribute]
            normal_id = (
                f"p_step_lesson_level-001-fktn-normal-{code}-{normal_index:03d}"
            )
            fixed = _stage_fact(
                state=state,
                lesson_row=_unique_row(lesson_rows, normal_id, "ProduceStepLesson"),
                level_rows=level_rows,
                setting_id=setting_id,
                plan_type=actual_plan_type,
                attribute=attribute,
                variant="normal",
                attribute_limit=mode_rules.attribute_cap,
                runtime_context=runtime_context,
            )
            candidates = tuple(
                _stage_fact(
                    state=state,
                    lesson_row=row,
                    level_rows=level_rows,
                    setting_id=setting_id,
                    plan_type=actual_plan_type,
                    attribute=attribute,
                    variant="sp",
                    attribute_limit=mode_rules.attribute_cap,
                    runtime_context=runtime_context,
                )
                for row in sorted(
                    (
                        row
                        for row in lesson_rows
                        if isinstance(row.get("id"), str)
                        and str(row["id"]).startswith(
                            f"p_step_lesson_level-001-fktn-sp-{code}-"
                        )
                    ),
                    key=lambda row: str(row["id"]),
                )
            )
            selected_id = selected_ids.get(attribute)
            selected = next(
                (value for value in candidates if value.stage_id == selected_id), None
            )
            if selected_id is None:
                issues.append(
                    _issue(
                        "sp-stage-selection-required",
                        f"selected_sp_stage_ids.{attribute.value}",
                    )
                )
            elif selected is None:
                issues.append(
                    _issue(
                        "sp-stage-selection-invalid",
                        f"selected_sp_stage_ids.{attribute.value}",
                        selected_id,
                    )
                )

            probability: Fraction | None = None
            frontier: tuple[InitialRegularWeightedLessonStage, ...] = ()
            if base_sp_rate_permils is not None and (
                modifiers is not None or sp_modifiers_known_absent is True
            ):
                effective_modifiers = modifiers or RunModifiers()
                rate = _sp_rate(base_sp_rate_permils, effective_modifiers, attribute)
                if not 0 <= rate <= 1000:
                    issues.append(
                        _issue(
                            "sp-rate-out-of-range",
                            f"sp_probability.{attribute.value}",
                            str(rate),
                        )
                    )
                else:
                    probability = Fraction(rate, 1000)
                    if selected is not None:
                        weighted: list[InitialRegularWeightedLessonStage] = []
                        if rate < 1000:
                            weighted.append(
                                InitialRegularWeightedLessonStage(
                                    Fraction(1000 - rate, 1000), fixed
                                )
                            )
                        if rate > 0:
                            weighted.append(
                                InitialRegularWeightedLessonStage(probability, selected)
                            )
                        frontier = tuple(weighted)
            result.append(
                InitialRegularLessonAttributeCatalog(
                    attribute,
                    fixed,
                    candidates,
                    selected,
                    probability,
                    frontier,
                )
            )
    else:
        hard_index = _HARD_WEEK_INDEX[request.week]
        for attribute in InitialRegularLessonAttribute:
            code = _ATTRIBUTE_MASTER_CODE[attribute]
            hard_id = (
                f"p_step_lesson_level-001-fktn-hard-{code}-{hard_index:03d}"
            )
            stage = _stage_fact(
                state=state,
                lesson_row=_unique_row(lesson_rows, hard_id, "ProduceStepLesson"),
                level_rows=level_rows,
                setting_id=setting_id,
                plan_type=actual_plan_type,
                attribute=attribute,
                variant="hard",
                attribute_limit=mode_rules.attribute_cap,
                runtime_context=runtime_context,
            )
            result.append(
                InitialRegularLessonAttributeCatalog(
                    attribute,
                    stage,
                    (),
                    None,
                    Fraction(0),
                    (InitialRegularWeightedLessonStage(Fraction(1), stage),),
                )
            )

    return InitialRegularLessonBranchCatalog(
        state,
        request,
        opened,
        request.action_id,
        tuple(result),
        tuple(dict.fromkeys(issues)),
    )


def _execution_issue(code: str, field: str, detail: str = "") -> OuterSearchIssue:
    return OuterSearchIssue(code, field, detail)


def build_initial_regular_weighted_weekly_frontier(
    catalog: InitialRegularLessonBranchCatalog,
    attribute: InitialRegularLessonAttribute | str,
    *,
    execution: InitialRegularLessonExecutionInputs | None = None,
    inner_execution: InitialRegularInnerLessonExecutionInputs | None = None,
) -> InitialRegularOfflineBranchExpansion:
    """Convert one player-selected attribute frontier for the weekly adapter.

    ``inner_execution`` builds the plan-neutral request consumed by the
    adapter registry.  ``execution`` is retained only for the legacy Plan3
    compatibility route.  Lesson tuning profiles are never used for simulator
    routing; the selected IdolCard ``planType`` remains authoritative.
    """

    if not isinstance(catalog, InitialRegularLessonBranchCatalog):
        raise TypeError("catalog must be InitialRegularLessonBranchCatalog")
    issues: list[OuterSearchIssue] = []
    try:
        selected = catalog.for_attribute(attribute)
    except (KeyError, ValueError) as error:
        return InitialRegularOfflineBranchExpansion.paused(
            _execution_issue("lesson-attribute-unavailable", "attribute", str(error))
        )
    if not selected.frontier:
        issues.append(
            _execution_issue(
                "lesson-probability-frontier-unresolved",
                f"catalog.attributes.{selected.attribute.value}.frontier",
            )
        )
    plans = {value.stage.plan_type for value in selected.frontier}
    if not plans:
        plans = {selected.fixed_stage.plan_type}
    if execution is not None and inner_execution is not None:
        return InitialRegularOfflineBranchExpansion.paused(
            _execution_issue(
                "lesson-execution-path-conflict",
                "execution",
                "provide inner_execution or legacy execution, not both",
            )
        )

    if inner_execution is not None:
        if not isinstance(inner_execution, InitialRegularInnerLessonExecutionInputs):
            raise TypeError(
                "inner_execution must be InitialRegularInnerLessonExecutionInputs or None"
            )
        if inner_execution.idol_card_id is None:
            issues.append(
                _execution_issue(
                    "idol-card-id-required",
                    "inner_execution.idol_card_id",
                )
            )
        execution_by_key = inner_execution.branch_map
        for weighted in selected.frontier:
            stage = weighted.stage
            if not stage.context_ready:
                issues.append(
                    _execution_issue(
                        "lesson-runtime-context-required",
                        f"stage.{stage.branch_key}.runtime_context",
                    )
                )
            branch = execution_by_key.get(stage.branch_key)
            if branch is None:
                issues.append(
                    _execution_issue(
                        "inner-lesson-branch-input-required",
                        f"inner_execution.branches.{stage.branch_key}",
                    )
                )
            elif branch.rng_before is None:
                issues.append(
                    _execution_issue(
                        "exam-seed-required",
                        f"inner_execution.branches.{stage.branch_key}.rng_before",
                    )
                )
        if issues:
            return InitialRegularOfflineBranchExpansion.paused(
                *tuple(dict.fromkeys(issues))
            )

        assert inner_execution.idol_card_id is not None
        common_branches: list[InitialRegularWeightedInnerBranch] = []
        for weighted in selected.frontier:
            stage = weighted.stage
            branch_execution = execution_by_key[stage.branch_key]
            assert branch_execution.rng_before is not None
            chance_branch = ChanceBranch(
                branch_id=(
                    f"{catalog.external_request.request_id}:"
                    f"{stage.attribute.value}:{stage.variant}"
                ),
                probability=float(weighted.probability),
                rng_token=branch_execution.rng_token,
                was_sp=stage.is_sp,
            )
            common_branches.append(
                InitialRegularWeightedInnerBranch(
                    weighted.probability,
                    build_initial_regular_lesson_stage_request(
                        catalog.outer_state,
                        catalog.external_request,
                        stage,
                        branch=chance_branch,
                        idol_card_id=inner_execution.idol_card_id,
                        rng_before=branch_execution.rng_before,
                    ),
                )
            )
        return InitialRegularOfflineBranchExpansion.resolved(*common_branches)

    if "plan2" in plans:
        issues.append(
            _execution_issue(
                "inner-execution-inputs-required",
                "inner_execution",
                "Plan2 lessons dispatch through the plan-neutral adapter registry",
            )
        )
        if any(not value.stage.context_ready for value in selected.frontier):
            issues.append(
                _execution_issue(
                    "lesson-runtime-context-required",
                    "catalog.stage.runtime_context",
                )
            )
        return InitialRegularOfflineBranchExpansion.paused(
            *tuple(dict.fromkeys(issues))
        )

    unsupported = sorted(plans - {"plan3"})
    if unsupported:
        issues.append(
            _execution_issue(
                "unsupported-inner-plan-type",
                "catalog.plan_types",
                ",".join(unsupported),
            )
        )
    if execution is None:
        issues.extend(
            (
                _execution_issue("exam-chance-input-required", "execution.branches"),
                _execution_issue("exam-loadout-required", "execution.loadout"),
                _execution_issue(
                    "exam-start-effects-proof-required",
                    "execution.start_turn_effects_known_absent",
                ),
                _execution_issue(
                    "exact-post-lesson-patch-required", "execution.branches"
                ),
            )
        )
        return InitialRegularOfflineBranchExpansion.paused(*issues)

    if execution.loadout is None:
        issues.append(_execution_issue("exam-loadout-required", "execution.loadout"))
    if execution.start_turn_effects_known_absent is not True:
        issues.append(
            _execution_issue(
                "exam-start-effects-proof-required",
                "execution.start_turn_effects_known_absent",
            )
        )
    execution_by_key = execution.branch_map
    deck_count = sum(value.count for value in catalog.outer_state.deck)
    missing_guid_count = sum(
        value.count for value in catalog.outer_state.deck if not value.instance_ids
    )
    for weighted in selected.frontier:
        stage = weighted.stage
        if not stage.context_ready:
            issues.append(
                _execution_issue(
                    "lesson-runtime-context-required",
                    f"stage.{stage.branch_key}.runtime_context",
                )
            )
        branch = execution_by_key.get(stage.branch_key)
        if branch is None or branch.chance is None:
            issues.append(
                _execution_issue(
                    "exam-chance-input-required",
                    f"execution.branches.{stage.branch_key}.chance",
                )
            )
        else:
            chance = branch.chance
            if chance.random_root is None:
                issues.append(
                    _execution_issue(
                        "exam-random-root-required",
                        f"execution.branches.{stage.branch_key}.chance.random_root",
                    )
                )
            if chance.random_state is None:
                issues.append(
                    _execution_issue(
                        "exam-seed-required",
                        f"execution.branches.{stage.branch_key}.chance.random_state",
                    )
                )
            if len(chance.instance_guids) != missing_guid_count:
                issues.append(
                    _execution_issue(
                        "exam-instance-guids-required",
                        f"execution.branches.{stage.branch_key}.chance.instance_guids",
                        f"expected {missing_guid_count}",
                    )
                )
            if len(chance.fixed_deck_orders) != deck_count:
                issues.append(
                    _execution_issue(
                        "exam-shuffle-orders-required",
                        f"execution.branches.{stage.branch_key}.chance.fixed_deck_orders",
                        f"expected {deck_count}",
                    )
                )
        if branch is None or branch.post_exam_resolution is None:
            issues.append(
                _execution_issue(
                    "exact-post-lesson-patch-required",
                    f"execution.branches.{stage.branch_key}.post_exam_resolution",
                )
            )
    if issues:
        unique = tuple(dict.fromkeys(issues))
        return InitialRegularOfflineBranchExpansion.paused(*unique)

    assert execution.loadout is not None
    branches: list[InitialRegularWeightedWeeklyBranch] = []
    for weighted in selected.frontier:
        stage = weighted.stage
        branch_execution = execution_by_key[stage.branch_key]
        assert branch_execution.chance is not None
        assert branch_execution.post_exam_resolution is not None
        assert stage.runtime_context is not None
        assert stage.runtime_turns is not None
        assert stage.limit_border is not None
        context = Plan3ExamStartContext(
            stage_id=stage.stage_id,
            setting_id=stage.setting_id,
            turns=stage.runtime_turns,
            current_parameter_type=stage.current_parameter_type,
            lesson_type=stage.lesson_type,
            step_type_value=stage.step_type_value,
            is_battle=False,
            clear_border=stage.clear_border,
            limit_border=stage.limit_border,
            battle_bonus_permille=stage.runtime_context.battle_bonus_permille,
            gimmick_group_id=stage.runtime_context.gimmick_group_id,
        )
        start_inputs = Plan3ExamStartInputs(
            deck=catalog.outer_state.deck,
            context=context,
            chance=branch_execution.chance,
            attributes=catalog.outer_state.attributes,
            stamina=catalog.outer_state.stamina,
            max_stamina=catalog.outer_state.max_stamina,
            loadout=execution.loadout,
            start_turn_effects_known_absent=True,
        )
        chance_branch = ChanceBranch(
            branch_id=(
                f"{catalog.external_request.request_id}:{stage.attribute.value}:"
                f"{stage.variant}"
            ),
            probability=float(weighted.probability),
            rng_token=branch_execution.chance.random_root,
            was_sp=stage.is_sp,
        )
        branches.append(
            InitialRegularWeightedWeeklyBranch(
                weighted.probability,
                lesson_inputs=InitialRegularLessonResolutionInput(
                    attribute=stage.attribute,
                    is_sp=stage.is_sp,
                    start_inputs=start_inputs,
                    branch=chance_branch,
                    post_exam_resolution=branch_execution.post_exam_resolution,
                ),
            )
        )
    return InitialRegularOfflineBranchExpansion.resolved(*branches)


__all__ = [
    "InitialRegularInnerLessonBranchExecution",
    "InitialRegularInnerLessonExecutionInputs",
    "InitialRegularLessonAttributeCatalog",
    "InitialRegularLessonBranchCatalog",
    "InitialRegularLessonBranchExecution",
    "InitialRegularLessonCatalogIssue",
    "InitialRegularLessonExecutionInputs",
    "InitialRegularLessonRuntimeContext",
    "InitialRegularLessonStageFact",
    "InitialRegularSpRatePermils",
    "InitialRegularWeightedLessonStage",
    "build_initial_regular_lesson_branch_catalog",
    "build_initial_regular_weighted_weekly_frontier",
]
