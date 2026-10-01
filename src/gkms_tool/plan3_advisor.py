"""Safe, explainable one-step advice for an authoritative Plan 3 hand.

This module intentionally performs no look-ahead.  It loads every visible
card from local Master, asks :mod:`gkms_tool.plan3_engine` to simulate exactly
one play, and ranks only legal, supported transitions with a transparent
state-difference heuristic.  Engine uncertainty remains a hard auto-click
blocker even when an analysis-only score can still be shown.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .master_db import DEFAULT_DATABASE
from .plan3_engine import (
    MOVE_LOST,
    STANCE_CONCENTRATION,
    STANCE_FULL_POWER,
    STANCE_NEUTRAL,
    STANCE_PRESERVATION,
    Plan3CardRef,
    Plan3ExamSettings,
    Plan3State,
    Plan3Transition,
    apply_plan3_card,
    load_plan3_card,
    load_plan3_exam_settings,
)


PHASE_EARLY = "early"
PHASE_MIDDLE = "middle"
PHASE_LATE = "late"
SUPPORTED_PHASES = frozenset({PHASE_EARLY, PHASE_MIDDLE, PHASE_LATE})


def _unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class Plan3HeuristicComponent:
    key: str
    value: int
    reason: str


@dataclass(frozen=True, slots=True)
class Plan3HeuristicScore:
    phase: str
    total: int
    components: tuple[Plan3HeuristicComponent, ...]

    @property
    def explanation(self) -> tuple[str, ...]:
        return tuple(
            f"{component.key} {component.value:+d}: {component.reason}"
            for component in self.components
        )


@dataclass(frozen=True, slots=True)
class Plan3CandidateAdvice:
    slot_index: int
    card_ref: Plan3CardRef
    card_name: str
    transition: Plan3Transition | None
    heuristic: Plan3HeuristicScore | None
    blockers: tuple[str, ...]

    @property
    def analysis_eligible(self) -> bool:
        return bool(
            self.transition is not None
            and self.transition.supported
            and self.transition.legal
            and not self.transition.unsupported_rules
            and self.heuristic is not None
        )

    @property
    def auto_executable(self) -> bool:
        return self.analysis_eligible and not self.blockers

    @property
    def score(self) -> int | None:
        return None if self.heuristic is None else self.heuristic.total

    @property
    def explanation(self) -> tuple[str, ...]:
        if self.heuristic is None:
            return ()
        return self.heuristic.explanation


@dataclass(frozen=True, slots=True)
class Plan3HandAdvice:
    phase: str
    total_turns: int
    visible_hand: tuple[Plan3CardRef, ...]
    candidates: tuple[Plan3CandidateAdvice, ...]
    global_blockers: tuple[str, ...] = ()
    recommended_slot_index: int | None = None
    auto_execute_slot_index: int | None = None

    @property
    def recommended(self) -> Plan3CandidateAdvice | None:
        if self.recommended_slot_index is None:
            return None
        return next(
            (
                candidate
                for candidate in self.candidates
                if candidate.slot_index == self.recommended_slot_index
            ),
            None,
        )


def classify_plan3_phase(state: Plan3State, total_turns: int) -> str:
    """Classify a run from authoritative round/remaining-turn counts."""

    if total_turns < 1 or total_turns < state.turns_remaining:
        raise ValueError("total_turns is inconsistent with the current state")
    late_cutoff = max(2, math.ceil(total_turns * 0.25))
    early_cutoff = math.ceil(total_turns * 0.60)
    if state.turns_remaining <= late_cutoff:
        return PHASE_LATE
    if state.turns_remaining > early_cutoff:
        return PHASE_EARLY
    return PHASE_MIDDLE


def _stance_utility(state: Plan3State, phase: str) -> int:
    if state.stance == STANCE_NEUTRAL:
        return 0
    if state.stance == STANCE_FULL_POWER:
        return {PHASE_EARLY: 18, PHASE_MIDDLE: 24, PHASE_LATE: 32}[phase]
    if state.stance == STANCE_CONCENTRATION:
        by_level = {
            PHASE_EARLY: (4, 7),
            PHASE_MIDDLE: (10, 15),
            PHASE_LATE: (16, 24),
        }
        return by_level[phase][min(2, state.stance_level) - 1]
    if state.stance == STANCE_PRESERVATION:
        by_level = {
            PHASE_EARLY: (12, 20, 24),
            PHASE_MIDDLE: (8, 14, 17),
            PHASE_LATE: (1, -3, -7),
        }
        return by_level[phase][min(3, state.stance_level) - 1]
    return 0


def _stamina_risk(stamina: int) -> int:
    if stamina <= 0:
        return 80
    if stamina <= 2:
        return 35
    if stamina <= 5:
        return 15
    return 0


def score_plan3_transition(
    transition: Plan3Transition, phase: str
) -> Plan3HeuristicScore:
    """Score a supported one-card transition without inspecting card identity."""

    if phase not in SUPPORTED_PHASES:
        raise ValueError(f"unsupported Plan 3 advice phase: {phase}")
    if (
        not transition.supported
        or not transition.legal
        or transition.unsupported_rules
    ):
        raise ValueError("cannot score an unsupported or illegal transition")

    return score_plan3_state_delta(transition.before, transition.after, phase,
        extra_plays=transition.extra_plays, card_moves_to_lost=transition.card.move_position_type == MOVE_LOST)


def score_plan3_state_delta(before: Plan3State, after: Plan3State, phase: str, *,
                           extra_plays: int = 0, card_moves_to_lost: bool = False) -> Plan3HeuristicScore:
    """The same local state weights for a proven card, drink or turn-end step.

    The caller owns transition legality; this pure function never fabricates a
    card wrapper to value a non-card action, and makes no terminal forecast.
    """
    if phase not in SUPPORTED_PHASES or not isinstance(before, Plan3State) or not isinstance(after, Plan3State):
        raise ValueError("local Plan3 state/phase is unavailable")
    if type(extra_plays) is not int or extra_plays < 0 or type(card_moves_to_lost) is not bool:
        raise ValueError("local Plan3 action consequences are invalid")
    components: list[Plan3HeuristicComponent] = []

    def add(key: str, value: int, reason: str, *, keep_zero: bool = False) -> None:
        if value or keep_zero:
            components.append(Plan3HeuristicComponent(key, value, reason))

    score_gain = after.score - before.score
    score_weight = {PHASE_EARLY: 1, PHASE_MIDDLE: 2, PHASE_LATE: 3}[phase]
    add(
        "score",
        score_gain * score_weight,
        f"score {before.score}→{after.score}; {phase} weight ×{score_weight}",
        keep_zero=True,
    )

    stamina_spent = max(0, before.stamina - after.stamina)
    stamina_weight = {PHASE_EARLY: 4, PHASE_MIDDLE: 3, PHASE_LATE: 2}[phase]
    add(
        "stamina",
        -(stamina_spent * stamina_weight),
        f"spent {stamina_spent}; preservation matters more before the finish",
        keep_zero=True,
    )
    risk_change = _stamina_risk(after.stamina) - _stamina_risk(before.stamina)
    add(
        "stamina-risk",
        -risk_change,
        f"risk tier from stamina {before.stamina}→{after.stamina}",
    )

    block_delta = after.block - before.block
    block_weight = {PHASE_EARLY: 2, PHASE_MIDDLE: 2, PHASE_LATE: 1}[phase]
    add(
        "block",
        block_delta * block_weight,
        f"block {before.block}→{after.block}",
    )

    stance_delta = _stance_utility(after, phase) - _stance_utility(before, phase)
    add(
        "stance",
        stance_delta,
        (
            f"{before.stance} L{before.stance_level}→"
            f"{after.stance} L{after.stance_level} for {phase} priorities"
        ),
    )

    gauge_delta = after.full_power_points - before.full_power_points
    total_gauge_delta = (
        after.full_power_points_total - before.full_power_points_total
    )
    gauge_weight = {PHASE_EARLY: 8, PHASE_MIDDLE: 6, PHASE_LATE: 3}[phase]
    add(
        "full-power-gauge",
        gauge_delta * gauge_weight,
        (
            f"current {before.full_power_points}→{after.full_power_points}; "
            f"cumulative +{total_gauge_delta}"
        ),
    )
    if (
        before.full_power_points < 10 <= after.full_power_points
        and after.turns_remaining > 1
    ):
        threshold_value = {
            PHASE_EARLY: 20,
            PHASE_MIDDLE: 15,
            PHASE_LATE: 6,
        }[phase]
        add(
            "full-power-ready",
            threshold_value,
            "gauge can enter Full Power at the next verified turn start",
        )

    enthusiasm_delta = after.enthusiasm - before.enthusiasm
    heat_weight = {PHASE_EARLY: 2, PHASE_MIDDLE: 3, PHASE_LATE: 4}[phase]
    future_plays = max(1, after.plays_remaining)
    add(
        "enthusiasm",
        enthusiasm_delta * heat_weight * future_plays,
        (
            f"turn heat {before.enthusiasm}→{after.enthusiasm}; "
            f"non-consuming across {after.plays_remaining} remaining play(s)"
        ),
    )
    additive_delta = after.enthusiasm_additive - before.enthusiasm_additive
    additive_weight = {PHASE_EARLY: 6, PHASE_MIDDLE: 4, PHASE_LATE: 2}[phase]
    add(
        "enthusiasm-additive",
        additive_delta * additive_weight,
        f"future preservation release additive +{additive_delta}",
    )

    add(
        "extra-play",
        extra_plays * 14,
        f"local action added {extra_plays} extra play(s)",
    )
    if card_moves_to_lost:
        lost_penalty = {PHASE_EARLY: -5, PHASE_MIDDLE: -3, PHASE_LATE: 0}[phase]
        add(
            "lost-zone",
            lost_penalty,
            "card leaves the deck permanently after this play",
        )

    total = sum(component.value for component in components)
    return Plan3HeuristicScore(phase, total, tuple(components))


def advise_plan3_visible_hand(
    state: Plan3State,
    visible_hand: tuple[Plan3CardRef, ...],
    *,
    total_turns: int | None = None,
    database: Path = DEFAULT_DATABASE,
    settings: Plan3ExamSettings | None = None,
) -> Plan3HandAdvice:
    """Simulate and rank only the authoritative, currently visible hand."""

    settings_blocker = ""
    if settings is None:
        try:
            settings = load_plan3_exam_settings()
        except (KeyError, ValueError, OSError) as error:
            settings_blocker = (
                f"exam-settings-load:{type(error).__name__}:{error}"
            )
    if total_turns is None:
        total_turns = state.round_number + state.turns_remaining - 1

    global_blockers: list[str] = []
    if settings_blocker:
        global_blockers.append(settings_blocker)
    try:
        state.validate(allow_completed=True)
    except (TypeError, ValueError) as error:
        global_blockers.append(f"state-invalid:{type(error).__name__}:{error}")
    try:
        phase = classify_plan3_phase(state, total_turns)
    except ValueError as error:
        phase = PHASE_LATE
        global_blockers.append(f"phase-invalid:{error}")
    if not visible_hand:
        global_blockers.append("visible-hand-empty")
    if tuple(visible_hand) != state.hand:
        global_blockers.append("visible-hand-checkpoint-mismatch")
    if state.awaiting_turn_start:
        global_blockers.append("phase-awaiting-turn-start")
    if state.turns_remaining == 0:
        global_blockers.append("phase-exam-complete")
    if state.plays_remaining <= 0:
        global_blockers.append("phase-no-card-plays-remaining")
    global_blockers = list(_unique(global_blockers))

    authoritative_counts = Counter(state.hand)
    seen_counts: Counter[Plan3CardRef] = Counter()
    candidates: list[Plan3CandidateAdvice] = []
    for slot_index, card_ref in enumerate(visible_hand):
        blockers = list(global_blockers)
        seen_counts[card_ref] += 1
        if seen_counts[card_ref] > authoritative_counts[card_ref]:
            blockers.append(
                f"card-not-in-authoritative-hand:{card_ref.card_id}+{card_ref.upgrade}"
            )

        card_name = card_ref.card_id
        transition: Plan3Transition | None = None
        heuristic: Plan3HeuristicScore | None = None
        try:
            card = load_plan3_card(
                card_ref.card_id,
                card_ref.upgrade,
                database,
            )
            card_name = card.name
        except (KeyError, ValueError, OSError, sqlite3.Error) as error:
            blockers.append(
                f"master-card-load:{type(error).__name__}:"
                f"{card_ref.card_id}+{card_ref.upgrade}"
            )
            candidates.append(
                Plan3CandidateAdvice(
                    slot_index,
                    card_ref,
                    card_name,
                    None,
                    None,
                    _unique(blockers),
                )
            )
            continue

        try:
            transition = apply_plan3_card(state, card, settings=settings)
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
            blockers.append(
                f"engine-error:{type(error).__name__}:{card_ref.card_id}"
            )
        if transition is not None:
            if not transition.supported:
                blockers.append("engine-unsupported")
            blockers.extend(
                f"engine-rule:{rule}" for rule in transition.unsupported_rules
            )
            if not transition.legal:
                blockers.append("engine-illegal")
            blockers.extend(
                f"engine-unverified:{rule}" for rule in transition.unverified_rules
            )
            if (
                transition.supported
                and transition.legal
                and not transition.unsupported_rules
            ):
                heuristic = score_plan3_transition(transition, phase)

        candidates.append(
            Plan3CandidateAdvice(
                slot_index=slot_index,
                card_ref=card_ref,
                card_name=card_name,
                transition=transition,
                heuristic=heuristic,
                blockers=_unique(blockers),
            )
        )

    recommendation_pool = [
        candidate for candidate in candidates if candidate.analysis_eligible
    ]
    recommended_slot: int | None = None
    if recommendation_pool and not global_blockers:
        recommended_slot = max(
            recommendation_pool,
            key=lambda candidate: (
                candidate.score if candidate.score is not None else -10**12,
                -candidate.slot_index,
            ),
        ).slot_index

    auto_pool = [candidate for candidate in candidates if candidate.auto_executable]
    auto_slot: int | None = None
    if auto_pool and not global_blockers:
        auto_slot = max(
            auto_pool,
            key=lambda candidate: (
                candidate.score if candidate.score is not None else -10**12,
                -candidate.slot_index,
            ),
        ).slot_index

    return Plan3HandAdvice(
        phase=phase,
        total_turns=total_turns,
        visible_hand=tuple(visible_hand),
        candidates=tuple(candidates),
        global_blockers=tuple(global_blockers),
        recommended_slot_index=recommended_slot,
        auto_execute_slot_index=auto_slot,
    )
