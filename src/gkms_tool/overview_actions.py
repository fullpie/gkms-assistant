"""Recognize and rank weekly actions on the First Produce overview.

Stable icon templates are reused from MaaGakumasu.  Their green pixels are
masks, so localized labels do not need to be OCRed.  The policy in this module
is intentionally a transparent Regular-mode baseline; it is not presented as
an optimal full-run solver.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil
from pathlib import Path
from typing import Iterable, Mapping

from PIL import Image

from .audition_rules import FINAL
from .route_calendar import (
    BUSINESS,
    CLASS,
    CONSULTATION,
    CRAM_LESSON,
    LESSON,
    OUTING,
    SPECIAL_GUIDANCE,
    SUPPLY,
    RouteWeek,
)
from .screen_state import OverviewState


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE_DIR = (
    PROJECT_ROOT
    / "_research"
    / "MaaGakumasu"
    / "assets"
    / "resource"
    / "base"
    / "image"
    / "produce"
)
NIA_TEMPLATE_DIR = DEFAULT_TEMPLATE_DIR / "NIA"

ACTIVITY = "activity"
REST = "rest"
VOCAL_LESSON = "vocal_lesson"
DANCE_LESSON = "dance_lesson"
VISUAL_LESSON = "visual_lesson"

# These are deliberately estimates, not claims about an exact lesson result.
# Actual card costs vary with the deck, block and passives, but the outer route
# still needs a budget before a lesson has begun.  Scaling from max stamina
# keeps the policy useful across idol cards instead of baking in one capture's
# 35/36-point bar.
LESSON_COST_RATIO = 0.25
LESSON_COST_FLOOR = 8
SAFETY_RESERVE_RATIO = 0.20
SAFETY_RESERVE_FLOOR = 6
FINAL_RESERVE_RATIO = 0.30
FINAL_RESERVE_FLOOR = 8
CRAM_LESSON_COST_MULTIPLIER = 2

ACTION_LABELS: Mapping[str, str] = {
    ACTIVITY: "活動支給",
    OUTING: "外出",
    CONSULTATION: "諮詢",
    CLASS: "授業",
    VOCAL_LESSON: "歌唱課程",
    DANCE_LESSON: "舞蹈課程",
    VISUAL_LESSON: "視覺課程",
    REST: "休息",
}

_TEMPLATE_FILES: Mapping[str, str] = {
    ACTIVITY: "event.png",
    OUTING: "go_out.png",
    CONSULTATION: "chat.png",
    CLASS: "lesson.png",
    VOCAL_LESSON: "Vo.png",
    DANCE_LESSON: "Da.png",
    VISUAL_LESSON: "Vi.png",
}

# N.I.A. uses its own fixed overview artwork.  These templates identify only
# the visible action kind and its click box; Produce LocalSave remains the
# authority for week, stamina, points and parameters.
_NIA_TEMPLATE_FILES: Mapping[str, str] = {
    ACTIVITY: "activity.png",
    CONSULTATION: "chat.png",
    DANCE_LESSON: "Da.png",
    OUTING: "go_out.png",
    SPECIAL_GUIDANCE: "guide.png",
    VISUAL_LESSON: "Vi.png",
    VOCAL_LESSON: "Vo.png",
    BUSINESS: "work.png",
}

_NIA_ACTION_LABELS: Mapping[str, str] = {
    ACTIVITY: "活動",
    CONSULTATION: "相談",
    DANCE_LESSON: "舞蹈自主課程",
    OUTING: "外出",
    SPECIAL_GUIDANCE: "特別指導",
    VISUAL_LESSON: "視覺自主課程",
    VOCAL_LESSON: "歌唱自主課程",
    BUSINESS: "工作",
    REST: "休息",
}


@dataclass(frozen=True, slots=True)
class WeeklyActionOption:
    action: str
    label: str
    canonical_box: tuple[int, int, int, int]
    match_score: float
    is_sp: bool = False

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.canonical_box
        return (left + right) // 2, (top + bottom) // 2

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WeeklyActionDecision:
    recommended: WeeklyActionOption
    reasons: tuple[str, ...]
    policy: str
    stamina_plan: "RegularStaminaPlan | None" = None

    def to_dict(self) -> dict[str, object]:
        return {
            "recommended": self.recommended.to_dict(),
            "reasons": list(self.reasons),
            "policy": self.policy,
            "stamina_plan": (
                None if self.stamina_plan is None else self.stamina_plan.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class RegularStaminaPlan:
    """Opportunity-aware stamina budget through the next recovery window.

    Future lesson rows remain visible in the report, but the required reserve
    covers the next expensive lesson rather than pretending every lesson must
    be prepaid from today's bar.  Drinks, card costs, and intermediate result
    recovery are settled by the game and become the next week's authority.
    """

    current_week: int
    weeks_until_final: int
    estimated_lesson_cost: int
    estimated_cram_lesson_cost: int
    current_action_hp_cost: int
    safety_reserve: int
    final_reserve: int
    next_week_has_recovery_option: bool
    next_recovery_week: int | None
    lesson_weeks_before_recovery: tuple[int, ...]
    lesson_week_costs_before_recovery: tuple[tuple[int, int], ...]
    required_stamina: int
    projected_stamina_after_lesson: int
    lesson_safe_for_horizon: bool
    below_horizon_budget: bool
    route_horizon_exact: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _scaled_stamina(
    max_stamina: int,
    *,
    ratio: float,
    floor: int,
) -> int:
    return min(max_stamina, max(floor, ceil(max_stamina * ratio)))


def _lesson_week(route_week: RouteWeek) -> bool:
    return bool({LESSON, CRAM_LESSON}.intersection(route_week.actions))


def _lesson_week_cost(
    route_week: RouteWeek,
    *,
    lesson_cost: int,
    cram_lesson_cost: int,
) -> int:
    if CRAM_LESSON in route_week.actions:
        return cram_lesson_cost
    if LESSON in route_week.actions:
        return lesson_cost
    return 0


def plan_regular_stamina(
    state: OverviewState,
    route_week: RouteWeek,
    *,
    route_horizon: Iterable[RouteWeek] | None = None,
) -> RegularStaminaPlan:
    """Estimate a reusable Regular stamina budget from current week to final.

    ``route_horizon`` starts with ``route_week`` and extends through the final
    performance.  A route-listed outing is treated as a recovery window.  The
    planner budgets every lesson/SP opportunity before that window; if there is
    no later window, it also carries a reserve into the final performance.
    """

    horizon = tuple(route_horizon) if route_horizon is not None else (route_week,)
    if not horizon or horizon[0] != route_week:
        raise ValueError("route horizon must start with the current route week")
    if any(
        later.week != earlier.week + 1
        for earlier, later in zip(horizon, horizon[1:])
    ):
        raise ValueError("route horizon weeks must be contiguous")
    if route_horizon is not None and horizon[-1].stage_type != FINAL:
        raise ValueError("route horizon must extend through the final performance")

    lesson_cost = _scaled_stamina(
        state.max_stamina,
        ratio=LESSON_COST_RATIO,
        floor=LESSON_COST_FLOOR,
    )
    cram_lesson_cost = min(
        state.max_stamina,
        lesson_cost * CRAM_LESSON_COST_MULTIPLIER,
    )
    safety_reserve = _scaled_stamina(
        state.max_stamina,
        ratio=SAFETY_RESERVE_RATIO,
        floor=SAFETY_RESERVE_FLOOR,
    )
    final_reserve = _scaled_stamina(
        state.max_stamina,
        ratio=FINAL_RESERVE_RATIO,
        floor=FINAL_RESERVE_FLOOR,
    )

    future = horizon[1:]
    recovery = next(
        (
            week
            for week in future
            if week.stage_type is None and OUTING in week.actions
        ),
        None,
    )
    budget_window = tuple(
        week for week in horizon if recovery is None or week.week < recovery.week
    )
    lesson_week_costs = tuple(
        (
            week.week,
            _lesson_week_cost(
                week,
                lesson_cost=lesson_cost,
                cram_lesson_cost=cram_lesson_cost,
            ),
        )
        for week in budget_window
        if _lesson_week(week)
    )
    lesson_weeks = tuple(week for week, _cost in lesson_week_costs)
    reserve = final_reserve if recovery is None else safety_reserve
    # Rest consumes an entire growth week.  Requiring the current bar to fund
    # every future lesson made Rest dominate even though each completed week
    # gives us a fresh authoritative stamina observation.  Reserve one next
    # lesson (the most expensive before recovery) and re-plan after settlement.
    required = min(
        state.max_stamina,
        reserve + max((cost for _week, cost in lesson_week_costs), default=0),
    )
    current_action_hp_cost = _lesson_week_cost(
        route_week,
        lesson_cost=lesson_cost,
        cram_lesson_cost=cram_lesson_cost,
    )
    # Non-exact schedules cannot prove the current action type.  Retain the
    # base lesson estimate for the immediate visible-options fallback, while
    # callers that require route safety reject ``route_horizon_exact=False``.
    projected = state.stamina - (current_action_hp_cost or lesson_cost)
    route_horizon_exact = bool(
        route_horizon is not None
        and all(week.exact_actions for week in horizon)
    )
    current_is_lesson = _lesson_week(route_week)
    lesson_safe = (
        projected >= safety_reserve
        if not route_horizon_exact or not current_is_lesson
        else state.stamina >= required
    )
    weeks_until_final = (
        state.weeks_remaining
        if route_horizon is None
        else max(0, horizon[-1].week - route_week.week)
    )
    return RegularStaminaPlan(
        current_week=route_week.week,
        weeks_until_final=weeks_until_final,
        estimated_lesson_cost=lesson_cost,
        estimated_cram_lesson_cost=cram_lesson_cost,
        current_action_hp_cost=current_action_hp_cost,
        safety_reserve=safety_reserve,
        final_reserve=final_reserve,
        next_week_has_recovery_option=bool(
            future
            and future[0].stage_type is None
            and OUTING in future[0].actions
        ),
        next_recovery_week=None if recovery is None else recovery.week,
        lesson_weeks_before_recovery=lesson_weeks,
        lesson_week_costs_before_recovery=lesson_week_costs,
        required_stamina=required,
        projected_stamina_after_lesson=projected,
        lesson_safe_for_horizon=lesson_safe,
        below_horizon_budget=state.stamina < required,
        route_horizon_exact=route_horizon_exact,
    )


def _coarse_then_fine_match(screen, template: Image.Image) -> tuple[int, int, float]:
    from .training_choice import _best_template_match

    width, height = template.size
    coarse_x = range(0, 720 - width + 1, 4)
    coarse_y = range(878, min(1105, 1280 - height) + 1, 4)
    left, top, _ = _best_template_match(
        screen, template, x_range=coarse_x, y_range=coarse_y
    )
    return _best_template_match(
        screen,
        template,
        x_range=range(max(0, left - 5), min(720 - width, left + 5) + 1),
        y_range=range(max(0, top - 5), min(1280 - height, top + 5) + 1),
    )


def _lesson_sp_badge_score(
    screen,
    template: Image.Image,
    lesson_box: tuple[int, int, int, int],
) -> float:
    """Match an SP badge only at one lesson tile's upper-left anchor.

    The overview has both three- and four-tile layouts.  In the four-tile
    layout the badge is mostly outside the lesson template bounds, so fixed
    screen columns miss it.  The compact relative window covers the observed
    overlap and older higher badge placement without scanning unrelated HUD
    icons or neighbouring tiles.
    """

    from .training_choice import _best_template_match

    left, top, _right, _bottom = lesson_box
    width, height = template.size
    minimum_x = max(0, left - width - 12)
    maximum_x = min(720 - width, left + 12)
    minimum_y = max(0, top - 2 * height - 12)
    maximum_y = min(1280 - height, top + 8)
    _matched_x, _matched_y, score = _best_template_match(
        screen,
        template,
        x_range=range(minimum_x, maximum_x + 1),
        y_range=range(minimum_y, maximum_y + 1),
    )
    return score


def detect_weekly_actions(
    image: Image.Image,
    *,
    template_dir: Path = DEFAULT_TEMPLATE_DIR,
    minimum_score: float = 0.93,
    minimum_sp_score: float = 0.93,
) -> tuple[WeeklyActionOption, ...]:
    from .training_choice import _best_template_match, _canonical_rgb

    screen = _canonical_rgb(image)
    candidates: list[WeeklyActionOption] = []
    directory = template_dir.resolve()
    sp_path = directory / "sp.png"
    if not sp_path.is_file():
        raise FileNotFoundError(f"Maa SP template is missing: {sp_path}")
    with Image.open(sp_path) as template:
        template.load()
        sp_template = template.copy()

    for action, filename in _TEMPLATE_FILES.items():
        path = directory / filename
        if not path.is_file():
            raise FileNotFoundError(f"Maa 週行動模板不存在：{path}")
        with Image.open(path) as template:
            template.load()
            left, top, score = _coarse_then_fine_match(screen, template)
            width, height = template.size
        if score >= minimum_score:
            box = (left, top, left + width, top + height)
            is_lesson = action in {
                VOCAL_LESSON,
                DANCE_LESSON,
                VISUAL_LESSON,
            }
            candidates.append(
                WeeklyActionOption(
                    action,
                    ACTION_LABELS[action],
                    box,
                    score,
                    is_lesson
                    and _lesson_sp_badge_score(
                        screen,
                        sp_template,
                        box,
                    )
                    >= minimum_sp_score,
                )
            )

    rest_path = directory / "rest.png"
    with Image.open(rest_path) as template:
        template.load()
        left, top, score = _best_template_match(
            screen,
            template,
            x_range=range(540, 641, 2),
            y_range=range(750, 930, 2),
        )
        width, height = template.size
    if score >= minimum_score:
        candidates.append(
            WeeklyActionOption(
                REST,
                ACTION_LABELS[REST],
                (left, top, left + width, top + height),
                score,
            )
        )

    # Similar schedule icons share an outline.  If two templates pass at the
    # same physical tile, retain the stronger identity only.
    accepted: list[WeeklyActionOption] = []
    for candidate in sorted(candidates, key=lambda option: option.match_score, reverse=True):
        cx, cy = candidate.center
        if any(
            abs(cx - other.center[0]) < 45 and abs(cy - other.center[1]) < 45
            for other in accepted
        ):
            continue
        accepted.append(candidate)
    if not accepted:
        raise ValueError("沒有辨識到可信的本週行動")
    return tuple(sorted(accepted, key=lambda option: (option.canonical_box[1], option.canonical_box[0])))


def detect_nia_weekly_actions(
    image: Image.Image,
    *,
    template_dir: Path = NIA_TEMPLATE_DIR,
    common_template_dir: Path = DEFAULT_TEMPLATE_DIR,
    minimum_score: float = 0.93,
    minimum_sp_score: float = 0.93,
) -> tuple[WeeklyActionOption, ...]:
    """Locate N.I.A. overview actions using Maa's established icon set.

    The result deliberately carries no parameter or route inference.  A match
    proves only that a particular action button is visible at the returned
    canonical box.  This keeps UI recognition separate from LocalSave state
    authority and avoids colour- or artwork-based card identity checks.
    """

    from .training_choice import _best_template_match, _canonical_rgb

    screen = _canonical_rgb(image)
    directory = Path(template_dir).resolve()
    common = Path(common_template_dir).resolve()
    sp_path = common / "sp.png"
    if not sp_path.is_file():
        raise FileNotFoundError(f"Maa SP template is missing: {sp_path}")
    with Image.open(sp_path) as template:
        template.load()
        sp_template = template.copy()

    candidates: list[WeeklyActionOption] = []
    for action, filename in _NIA_TEMPLATE_FILES.items():
        path = directory / filename
        if not path.is_file():
            raise FileNotFoundError(f"Maa N.I.A. action template is missing: {path}")
        with Image.open(path) as template:
            template.load()
            left, top, score = _coarse_then_fine_match(screen, template)
            width, height = template.size
        if score < minimum_score:
            continue
        box = (left, top, left + width, top + height)
        is_lesson = action in {
            VOCAL_LESSON,
            DANCE_LESSON,
            VISUAL_LESSON,
        }
        candidates.append(
            WeeklyActionOption(
                action=action,
                label=_NIA_ACTION_LABELS[action],
                canonical_box=box,
                match_score=score,
                is_sp=(
                    is_lesson
                    and _lesson_sp_badge_score(screen, sp_template, box)
                    >= minimum_sp_score
                ),
            )
        )

    # Rest retains the common Maa artwork and its separate upper-right slot.
    rest_path = common / "rest.png"
    if not rest_path.is_file():
        raise FileNotFoundError(f"Maa Rest template is missing: {rest_path}")
    with Image.open(rest_path) as template:
        template.load()
        left, top, score = _best_template_match(
            screen,
            template,
            x_range=range(500, 641, 2),
            y_range=range(750, 981, 2),
        )
        width, height = template.size
    if score >= minimum_score:
        candidates.append(
            WeeklyActionOption(
                REST,
                _NIA_ACTION_LABELS[REST],
                (left, top, left + width, top + height),
                score,
            )
        )

    # Related N.I.A. buttons share the same rounded outline.  Only one action
    # may own a physical tile; retain the strongest identity at that location.
    accepted: list[WeeklyActionOption] = []
    for candidate in sorted(
        candidates, key=lambda option: option.match_score, reverse=True
    ):
        cx, cy = candidate.center
        if any(
            abs(cx - other.center[0]) < 45 and abs(cy - other.center[1]) < 45
            for other in accepted
        ):
            continue
        accepted.append(candidate)
    if not accepted:
        raise ValueError("no supported N.I.A. weekly action is visible")
    return tuple(
        sorted(
            accepted,
            key=lambda option: (
                option.canonical_box[1],
                option.canonical_box[0],
            ),
        )
    )


def recommend_weekly_action(
    state: OverviewState,
    options: Iterable[WeeklyActionOption],
    route_week: RouteWeek,
    *,
    skip_consultation: bool = True,
    route_horizon: Iterable[RouteWeek] | None = None,
) -> WeeklyActionDecision:
    """Apply an explainable Regular policy with a route-level stamina budget."""

    state_options = {option.action: option for option in options}
    plan = plan_regular_stamina(
        state,
        route_week,
        route_horizon=route_horizon,
    )
    if not plan.route_horizon_exact:
        raise ValueError(
            "Regular weekly advice requires an exact route horizon through "
            "the final performance"
        )
    allowed = set(route_week.actions)
    policy = (
        "regular-stamina-plan-v3"
        if plan.route_horizon_exact
        else "regular-visible-options-v1"
    )

    def decide(
        option: WeeklyActionOption,
        *reasons: str,
        decision_policy: str | None = None,
    ) -> WeeklyActionDecision:
        return WeeklyActionDecision(
            option,
            tuple(reasons),
            decision_policy or policy,
            plan,
        )

    def budget_reason() -> str:
        lesson_weeks = "、".join(
            str(week) for week in plan.lesson_weeks_before_recovery
        ) or "無"
        if plan.next_recovery_week is None:
            destination = f"最終演出（保留 {plan.final_reserve}）"
        else:
            destination = f"第 {plan.next_recovery_week} 週恢復窗"
        return (
            f"距最終演出 {plan.weeks_until_final} 週；預估每堂課耗體 "
            f"{plan.estimated_lesson_cost}，到{destination}前的課程週為 "
            f"{lesson_weeks}，目前安全預算為 {plan.required_stamina}。"
        )

    def recover(reason: str, *, emergency: bool = False) -> WeeklyActionDecision | None:
        outing_allowed = emergency or OUTING in allowed or not route_week.exact_actions
        # A visible Outing preserves route value while recovering. Rest loses
        # a growth week, so use it only when that productive recovery is not
        # available. Class still must never be treated as recovery.
        if REST in state_options and not (
            OUTING in state_options
            and outing_allowed
            and state.produce_points >= 100
        ):
            return decide(
                state_options[REST],
                reason,
                budget_reason(),
            )
        if (
            OUTING in state_options
            and outing_allowed
            and state.produce_points >= 100
        ):
            return decide(
                state_options[OUTING],
                reason,
                budget_reason(),
                "P 點足以承擔外出選項。",
            )
        return None

    stamina_ratio = state.stamina / state.max_stamina
    if state.stamina < 8 or stamina_ratio < 0.20:
        recovery = recover(
            f"體力僅 {state.stamina}/{state.max_stamina}，已落入緊急回復線。",
            emergency=True,
        )
        if recovery is not None:
            return recovery
        raise ValueError(
            "stamina is below the emergency line and no proven Rest/Outing "
            "recovery action is visible; Class is not a recovery action"
        )

    lesson_actions = {
        VOCAL_LESSON: state.vocal,
        DANCE_LESSON: state.dance,
        VISUAL_LESSON: state.visual,
    }
    visible_lessons = [
        (value, state_options[action])
        for action, value in lesson_actions.items()
        if action in state_options
    ]
    visible_sp_lessons = [
        item for item in visible_lessons if item[1].is_sp
    ]
    lesson_route_available = not route_week.exact_actions or bool(
        {LESSON, CRAM_LESSON}.intersection(allowed)
    )
    if visible_lessons and lesson_route_available:
        immediate_lesson_safe = (
            plan.projected_stamina_after_lesson >= plan.safety_reserve
        )
        if not immediate_lesson_safe:
            recovery = recover(
                f"進課後預估只剩 {plan.projected_stamina_after_lesson} 體力，"
                f"低於安全保留 {plan.safety_reserve}。"
            )
            if recovery is not None:
                return recovery
            raise ValueError(
                "the visible lesson breaks the immediate stamina reserve and "
                "Rest is not visible"
            )

        # SP is valuable, but it is not allowed to consume stamina reserved
        # for later lessons or the final performance.
        if (
            visible_sp_lessons
            and immediate_lesson_safe
            and (
                not plan.route_horizon_exact
                or plan.lesson_safe_for_horizon
            )
        ):
            _, option = min(visible_sp_lessons, key=lambda item: item[0])
            return decide(
                option,
                "本週辨識到 SP 課程，維持 SP 高優先。",
                f"完成後預估仍有 {plan.projected_stamina_after_lesson} 體力，"
                f"高於安全保留 {plan.safety_reserve}。",
                budget_reason(),
            )

        if plan.route_horizon_exact and not plan.lesson_safe_for_horizon:
            recovery = recover(
                f"目前 {state.stamina} 體力不足以支應進課後到下一個恢復窗的預算。"
            )
            if recovery is not None:
                return recovery
            raise ValueError(
                "the visible lesson breaks the route stamina budget and Rest "
                "is not visible"
            )

        _, option = min(visible_lessons, key=lambda item: item[0])
        if not route_week.exact_actions:
            return decide(
                option,
                "逐週表尚未涵蓋此角色；通過單堂耗體安全線後，依可見選項訓練最低屬性。",
                budget_reason(),
            )
        return decide(
            option,
            "目前體力通過跨週預算，進課後再以 SP 優先。",
            budget_reason(),
        )

    # Use a route-listed outing before a run of lesson weeks instead of
    # waiting until the old 20% emergency line.  If next week is itself a
    # recovery window, the budget window is empty and recovery can be deferred.
    if (
        plan.route_horizon_exact
        and plan.below_horizon_budget
        and OUTING in allowed
    ):
        recovery = recover(
            f"目前 {state.stamina} 體力低於跨週安全預算 {plan.required_stamina}，"
            "本週先使用恢復窗。"
        )
        if recovery is not None:
            return recovery

    if SUPPLY in allowed and ACTIVITY in state_options:
        reasons = [
            "活動支給增加後續可用資源。",
            budget_reason(),
            "商店已依本次範圍排除。",
        ]
        if plan.next_week_has_recovery_option:
            reasons.insert(1, "下週仍有路線恢復選項，本週不必提前犧牲活動支給。")
        return decide(state_options[ACTIVITY], *reasons)

    if CLASS in allowed and CLASS in state_options:
        remaining_requirement = max(
            0,
            plan.required_stamina - plan.current_action_hp_cost,
        )
        predicted_after_class = max(0, state.stamina - LESSON_COST_FLOOR)
        if predicted_after_class < remaining_requirement:
            recovery = recover(
                f"授業預估耗體 {LESSON_COST_FLOOR} 後只剩 "
                f"{predicted_after_class}，低於後續需求 {remaining_requirement}。"
            )
            if recovery is not None:
                return recovery
            raise ValueError(
                "Class would break the route stamina reserve and Rest is not visible"
            )
        return decide(
            state_options[CLASS],
            "本週公開路線允許授業；授業不是恢復，扣除估計耗體後仍通過預算。",
            budget_reason(),
        )
    if (
        not skip_consultation
        and CONSULTATION in allowed
        and CONSULTATION in state_options
        and state.produce_points >= 100
    ):
        return decide(
            state_options[CONSULTATION],
            "P 點足以使用諮詢。",
            budget_reason(),
        )
    if OUTING in allowed and OUTING in state_options:
        return decide(
            state_options[OUTING],
            "本週其餘已支援選項不適用，外出可補足後續體力。",
            budget_reason(),
        )
    if REST in state_options and len(state_options) == 1:
        raise ValueError(
            "體力充足時僅辨識到休息；可能是其他行動圖示仍在動畫中"
        )
    if REST in state_options:
        return decide(
            state_options[REST],
            "沒有辨識到可安全評分的路線選項，以休息作保守候選。",
            budget_reason(),
        )
    raise ValueError("本週沒有可由目前基準安全推薦的行動")
