"""Pure, conservative outer-route ranking for First Produce Regular.

The detailed exam simulators intentionally solve a *lesson after it has
started*.  This module is the small bridge before that boundary: it ranks the
currently visible weekly tiles using the exact calendar horizon and the shared
``RegularStaminaPlan``.  It does not inspect a screen, click a tile, or claim
that a card's Master description predicts a complete future deck.

Master card data is only used as a bounded tie-breaker for lesson candidates:
verified direct lesson effects and stamina-relief effects make a deck more able
to convert a safe lesson opportunity.  Unknown effects are not interpreted.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

from .logic_engine import (
    EFFECT_LESSON,
    EFFECT_LESSON_BY_BLOCK,
    EFFECT_LESSON_BY_GOOD_IMPRESSION,
    EFFECT_LESSON_BY_MOTIVATION,
    EFFECT_STAMINA_CONSUMPTION_DOWN,
    EFFECT_STAMINA_CONSUMPTION_DOWN_FIXED,
    MasterCard,
)
from .overview_actions import (
    ACTIVITY,
    CLASS,
    DANCE_LESSON,
    REST,
    VOCAL_LESSON,
    VISUAL_LESSON,
    RegularStaminaPlan,
    WeeklyActionOption,
    plan_regular_stamina,
)
from .route_calendar import (
    CLASS as ROUTE_CLASS,
    CONSULTATION,
    CRAM_LESSON,
    LESSON,
    OUTING,
    SUPPLY,
    RouteWeek,
    DEFAULT_MASTER_DIR,
)
from .screen_state import OverviewState


_LESSON_ACTIONS = frozenset({VOCAL_LESSON, DANCE_LESSON, VISUAL_LESSON})
_LESSON_EFFECTS = frozenset(
    {
        EFFECT_LESSON,
        EFFECT_LESSON_BY_GOOD_IMPRESSION,
        EFFECT_LESSON_BY_BLOCK,
        EFFECT_LESSON_BY_MOTIVATION,
    }
)
_STAMINA_RELIEF_EFFECTS = frozenset(
    {EFFECT_STAMINA_CONSUMPTION_DOWN, EFFECT_STAMINA_CONSUMPTION_DOWN_FIXED}
)


@dataclass(frozen=True, slots=True)
class MasterDeckRouteSignal:
    """Small, auditable deck summary used before a lesson starts.

    Values deliberately describe only static Master rows, not draw odds,
    support passives, or the outcome of an exam.  Those belong to
    :mod:`audition_horizon` once the deck and turn schedule are observable.
    """

    card_count: int
    direct_lesson_value: int
    stamina_relief_value: int

    @property
    def lesson_tiebreak(self) -> int:
        """Bound Master metadata to a small score contribution (0..40)."""

        return min(40, self.direct_lesson_value // 10 + self.stamina_relief_value)


@dataclass(frozen=True, slots=True)
class RegularLessonExpectedGainSignal:
    """Planning estimate for one week's capped lesson reward.

    ``normal_base_gain`` and ``sp_base_gain`` are the selected
    ``ProduceStepLessonLevel.resultTargetValueLimit`` values.  Growth rates are
    the *effective* run values in permille, after the idol card and equipped
    configuration have been combined.  This value is useful for choosing an
    attribute and avoiding wasted cap headroom; it is not an authoritative
    prediction of the post-lesson Vo/Da/Vi snapshot.  Memories, support
    passives and later reward effects are allowed to settle in the game, and
    the outer state is then rebased from the result screen/LocalSave.

    Support-card counts are deliberately not scored on their own: their
    gameplay contribution belongs in these resolved growth rates (and in the
    already-visible SP flag).
    """

    normal_base_gain: int
    sp_base_gain: int
    vocal_growth_permil: int
    dance_growth_permil: int
    visual_growth_permil: int
    source: str

    def __post_init__(self) -> None:
        for name in (
            "normal_base_gain",
            "sp_base_gain",
            "vocal_growth_permil",
            "dance_growth_permil",
            "visual_growth_permil",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be non-empty text")

    def expected_gain(self, option: WeeklyActionOption) -> int:
        growth = {
            VOCAL_LESSON: self.vocal_growth_permil,
            DANCE_LESSON: self.dance_growth_permil,
            VISUAL_LESSON: self.visual_growth_permil,
        }.get(option.action)
        if growth is None:
            raise ValueError(f"not a lesson action: {option.action}")
        base = self.sp_base_gain if option.is_sp else self.normal_base_gain
        # Android applies the displayed growth percentage to the lesson result
        # and truncates the integer parameter award.
        return base * (1000 + growth) // 1000


@lru_cache(maxsize=32)
def _week_one_lesson_base_gains(
    character_id: str,
    master_dir: Path,
) -> tuple[int, int]:
    """Resolve W1 normal/SP limits through exact Master joins.

    Later Regular weeks can have multiple structurally valid SP stage rows and
    require a caller-selected stage identity.  This narrow loader therefore
    covers only W1, where ``*-001`` is exact, rather than guessing a later SP
    row from the week number.
    """

    if not isinstance(character_id, str) or not character_id.strip():
        raise ValueError("character_id must be non-empty text")

    def rows(name: str) -> tuple[dict[str, object], ...]:
        path = master_dir / name
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{name} must contain a list")
        return tuple(value for value in payload if isinstance(value, dict))

    lesson_rows = rows("ProduceStepLesson.yaml")
    level_rows = rows("ProduceStepLessonLevel.yaml")
    def unique(
        source: tuple[dict[str, object], ...], row_id: str, label: str
    ) -> dict[str, object]:
        matches = tuple(row for row in source if row.get("id") == row_id)
        if len(matches) != 1:
            raise ValueError(f"{label} must resolve exactly once: {row_id}")
        return matches[0]

    result: list[int] = []
    for variant in ("normal", "sp"):
        gains: set[int] = set()
        for code in ("vo", "da", "vi"):
            stage_id = (
                f"p_step_lesson_level-001-{character_id}-{variant}-{code}-001"
            )
            stage = unique(lesson_rows, stage_id, "ProduceStepLesson")
            level_id = stage.get("produceStepLessonLevelId")
            if not isinstance(level_id, str) or not level_id:
                raise ValueError(f"missing lesson level identity: {stage_id}")
            level = unique(level_rows, level_id, "ProduceStepLessonLevel")
            gain = level.get("resultTargetValueLimit")
            if isinstance(gain, bool) or not isinstance(gain, int) or gain <= 0:
                raise ValueError(f"invalid resultTargetValueLimit: {level_id}")
            gains.add(gain)
        if len(gains) != 1:
            raise ValueError(f"W1 {variant} lesson gains differ by attribute")
        result.append(gains.pop())
    return result[0], result[1]


def load_week_one_regular_lesson_gain_signal(
    *,
    character_id: str,
    vocal_growth_permil: int,
    dance_growth_permil: int,
    visual_growth_permil: int,
    source: str,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> RegularLessonExpectedGainSignal:
    """Join effective run growth rates to exact W1 lesson Master limits."""

    normal, sp = _week_one_lesson_base_gains(
        character_id, Path(master_dir).resolve()
    )
    return RegularLessonExpectedGainSignal(
        normal_base_gain=normal,
        sp_base_gain=sp,
        vocal_growth_permil=vocal_growth_permil,
        dance_growth_permil=dance_growth_permil,
        visual_growth_permil=visual_growth_permil,
        source=source,
    )


@dataclass(frozen=True, slots=True)
class RegularRouteCandidate:
    option: WeeklyActionOption
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegularRouteRanking:
    """A pure ranking that callers may display, persist, or execute later."""

    candidates: tuple[RegularRouteCandidate, ...]
    stamina_plan: RegularStaminaPlan
    deck_signal: MasterDeckRouteSignal
    lesson_gain_signal: RegularLessonExpectedGainSignal | None = None

    @property
    def recommended(self) -> RegularRouteCandidate:
        return self.candidates[0]


def summarize_master_deck(cards: Iterable[MasterCard]) -> MasterDeckRouteSignal:
    """Extract only verified outer-route-relevant fields from Master cards."""

    card_count = 0
    lesson_value = 0
    stamina_relief = 0
    for card in cards:
        if not isinstance(card, MasterCard):
            raise TypeError("cards must contain MasterCard values")
        card_count += 1
        for effect in card.effects:
            if effect.effect_type in _LESSON_EFFECTS:
                lesson_value += max(0, effect.value1)
            elif effect.effect_type in _STAMINA_RELIEF_EFFECTS:
                stamina_relief += max(0, effect.value1)
    return MasterDeckRouteSignal(card_count, lesson_value, stamina_relief)


def _is_allowed(option: WeeklyActionOption, route_week: RouteWeek) -> bool:
    """Keep a visible rest tile legal while enforcing exact route actions."""

    if not route_week.exact_actions or option.action == REST:
        return True
    allowed = set(route_week.actions)
    if option.action in _LESSON_ACTIONS:
        return bool({LESSON, CRAM_LESSON}.intersection(allowed))
    return {
        ACTIVITY: SUPPLY,
        "outing": OUTING,
        CLASS: ROUTE_CLASS,
        "consultation": CONSULTATION,
    }.get(option.action) in allowed


def _attribute_need(state: OverviewState, action: str) -> int:
    values = {
        VOCAL_LESSON: state.vocal,
        DANCE_LESSON: state.dance,
        VISUAL_LESSON: state.visual,
    }
    if action not in values:
        return 0
    return max(values.values()) - values[action]


def rank_regular_route_candidates(
    state: OverviewState,
    options: Iterable[WeeklyActionOption],
    route_week: RouteWeek,
    *,
    route_horizon: Iterable[RouteWeek],
    master_cards: Iterable[MasterCard] = (),
    lesson_gain_signal: RegularLessonExpectedGainSignal | None = None,
    attribute_cap: int | None = None,
) -> RegularRouteRanking:
    """Rank visible First Produce Regular options without game I/O.

    A full horizon is required so stamina safety has a real destination.  The
    result intentionally contains competing candidates; choosing/clicking is a
    separate layer.  Unsafe lessons receive a strong penalty rather than being
    omitted, which makes the policy inspectable and keeps captured bad choices
    useful as training data.
    """

    if attribute_cap is not None and (
        isinstance(attribute_cap, bool)
        or not isinstance(attribute_cap, int)
        or attribute_cap < 1
    ):
        raise ValueError("attribute_cap must be a positive integer")
    horizon = tuple(route_horizon)
    plan = plan_regular_stamina(state, route_week, route_horizon=horizon)
    signal = summarize_master_deck(master_cards)
    candidates: list[RegularRouteCandidate] = []
    for option in options:
        if not _is_allowed(option, route_week):
            continue
        score = 0
        reasons: list[str] = []
        if option.action in _LESSON_ACTIONS:
            if lesson_gain_signal is None:
                score = 100 + _attribute_need(state, option.action) // 4
                if option.is_sp:
                    score += 80
                    reasons.append("SP lesson")
                reasons.append("raw attribute deficit fallback; expected gain unavailable")
            else:
                nominal_gain = lesson_gain_signal.expected_gain(option)
                current_attribute = {
                    VOCAL_LESSON: state.vocal,
                    DANCE_LESSON: state.dance,
                    VISUAL_LESSON: state.visual,
                }[option.action]
                headroom = (
                    nominal_gain
                    if attribute_cap is None
                    else max(0, attribute_cap - current_attribute)
                )
                usable_gain = min(nominal_gain, headroom)
                overflow = nominal_gain - usable_gain
                # Encode a lexicographic ordering in the existing integer
                # score.  This is a route heuristic for attribute direction
                # and SP/cap value, never a post-settlement equality claim;
                # raw deficit remains a deterministic tie-break.
                score = (
                    100
                    + usable_gain * 1000
                    + min(999, _attribute_need(state, option.action))
                )
                reasons.append(
                    f"estimated usable lesson gain {usable_gain} "
                    f"({lesson_gain_signal.source})"
                )
                if attribute_cap is not None:
                    reasons.append(
                        f"observed {option.action} headroom {headroom} "
                        f"to Master cap {attribute_cap}"
                    )
                    if overflow:
                        reasons.append(
                            f"avoid estimated overflow {overflow}; settle from game result"
                        )
                if option.is_sp:
                    reasons.append("SP lesson Master limit")
            score += signal.lesson_tiebreak
            if signal.lesson_tiebreak:
                reasons.append("verified Master lesson/stamina effects")
            if not plan.lesson_safe_for_horizon:
                score -= 10_000
                reasons.append("below route stamina budget")
            else:
                reasons.append("route stamina budget holds")
        elif option.action == "outing":
            score = 90 if plan.below_horizon_budget else 12
            if state.produce_points >= 100:
                score += 8
            reasons.append("recovery window" if plan.below_horizon_budget else "optional recovery")
        elif option.action == REST:
            score = 75 if plan.below_horizon_budget else -20
            reasons.append("protect route reserve" if plan.below_horizon_budget else "low immediate value")
        elif option.action == CLASS:
            score = 28
            reasons.append("class route value")
        elif option.action == ACTIVITY:
            score = 20
            reasons.append("supply/activity route value")
        elif option.action == "consultation":
            score = 16 if state.produce_points >= 100 else -12
            reasons.append("points-dependent consultation")
        else:
            score = 0
            reasons.append("unmodelled visible action")
        candidates.append(RegularRouteCandidate(option, score, tuple(reasons)))
    if not candidates:
        raise ValueError("no visible action is compatible with the route week")
    return RegularRouteRanking(
        tuple(sorted(candidates, key=lambda item: (-item.score, item.option.action))),
        plan,
        signal,
        lesson_gain_signal,
    )


__all__ = [
    "MasterDeckRouteSignal",
    "RegularLessonExpectedGainSignal",
    "RegularRouteCandidate",
    "RegularRouteRanking",
    "rank_regular_route_candidates",
    "load_week_one_regular_lesson_gain_signal",
    "summarize_master_deck",
]
