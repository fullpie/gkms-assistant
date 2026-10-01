"""Deterministic beam search over the strict Plan 3 transition kernel.

The search layer never reimplements card arithmetic.  It only composes the
public, Master-driven operations in :mod:`gkms_tool.plan3_engine`, retaining
the exact transition object for every played card and every turn start.

Future draw upgrades deserve special treatment.  A support card can grant a
temporary upgrade when a card is distributed even though the ordered zone
still contains its base upgrade.  Callers that know this uncertainty must
either provide ``draw_upgrade_resolver`` or explicitly attest that the zone
references already contain authoritative future upgrades.  Otherwise the
current hand is searched exactly and the first uncertain turn boundary is
reported as unsupported.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence, TypeAlias

from .master_db import DEFAULT_DATABASE
from .plan3_engine import (
    Plan3Card,
    Plan3CardRef,
    Plan3ExamSettings,
    Plan3GimmickProfile,
    Plan3State,
    Plan3Transition,
    Plan3TurnStart,
    apply_plan3_card,
    end_plan3_turn,
    load_plan3_card,
    load_plan3_exam_settings,
    start_plan3_turn,
)


ObjectiveValue: TypeAlias = float | int | tuple[float | int, ...]
Objective: TypeAlias = Callable[[Plan3State], ObjectiveValue]
DrawUpgradeResolver: TypeAlias = Callable[
    [Plan3State, tuple[Plan3CardRef, ...]], tuple[Plan3CardRef, ...]
]


@dataclass(frozen=True, slots=True)
class Plan3SearchEvaluation:
    """Visible outcome metrics retained independently of caller heuristics."""

    complete: bool
    clear: bool
    perfect: bool
    score: int
    stamina: int
    block: int
    objective: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Plan3SearchStep:
    """One exact card or turn-boundary transition in a candidate path."""

    kind: str
    before: Plan3State
    after: Plan3State
    card_ref: Plan3CardRef | None = None
    card_transition: Plan3Transition | None = None
    ended_state: Plan3State | None = None
    turn_start: Plan3TurnStart | None = None


@dataclass(frozen=True, slots=True)
class Plan3SearchDiagnostic:
    """A branch that was not used as an exact simulated outcome."""

    stage: str
    rules: tuple[str, ...]
    state: Plan3State
    actions: tuple[Plan3CardRef, ...]
    card_ref: Plan3CardRef | None = None


@dataclass(frozen=True, slots=True)
class Plan3SearchPath:
    state: Plan3State
    steps: tuple[Plan3SearchStep, ...]
    evaluation: Plan3SearchEvaluation
    exact: bool = True
    stopped_reason: str = ""

    @property
    def actions(self) -> tuple[Plan3CardRef, ...]:
        return tuple(
            step.card_ref
            for step in self.steps
            if step.kind == "card" and step.card_ref is not None
        )

    @property
    def transitions(self) -> tuple[Plan3Transition, ...]:
        return tuple(
            step.card_transition
            for step in self.steps
            if step.card_transition is not None
        )

    @property
    def complete(self) -> bool:
        return self.state.turns_remaining == 0


@dataclass(frozen=True, slots=True)
class Plan3SearchResult:
    best: Plan3SearchPath | None
    candidates: tuple[Plan3SearchPath, ...]
    diagnostics: tuple[Plan3SearchDiagnostic, ...]
    expanded_nodes: int
    deduplicated_nodes: int
    beam_width: int
    depth: int | None


def _objective_tuple(value: ObjectiveValue) -> tuple[float, ...]:
    raw: Sequence[float | int]
    if isinstance(value, bool):
        raise TypeError("objective must not return bool")
    if isinstance(value, (int, float)):
        raw = (value,)
    elif isinstance(value, tuple):
        raw = value
    else:
        raise TypeError("objective must return a number or tuple of numbers")
    normalized: list[float] = []
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, (int, float)):
            raise TypeError("objective tuple entries must be numbers")
        number = float(entry)
        if not math.isfinite(number):
            raise ValueError("objective values must be finite")
        normalized.append(number)
    if not normalized:
        raise ValueError("objective tuple must not be empty")
    return tuple(normalized)


def evaluate_plan3_search_state(
    state: Plan3State, objective: Objective | None = None
) -> Plan3SearchEvaluation:
    """Evaluate a state without hiding clear/perfect/completion semantics."""

    complete = state.turns_remaining == 0
    clear = state.clear_border >= 0 and state.score >= state.clear_border
    perfect = state.limit_border >= 0 and state.score >= state.limit_border
    if objective is None:
        value = (
            int(complete),
            int(perfect),
            int(clear),
            state.score,
            state.stamina,
            state.block,
        )
    else:
        value = objective(state)
    return Plan3SearchEvaluation(
        complete=complete,
        clear=clear,
        perfect=perfect,
        score=state.score,
        stamina=state.stamina,
        block=state.block,
        objective=_objective_tuple(value),
    )


def _path_key(path: Plan3SearchPath) -> tuple[object, ...]:
    # Reverse sorting uses the objective.  The negative action count prefers a
    # shorter proof when two paths arrive at an equivalent scored state.
    return (
        path.evaluation.objective,
        int(path.exact),
        -len(path.actions),
        tuple((ref.card_id, ref.upgrade) for ref in path.actions),
    )


def _replace_evaluation(
    path: Plan3SearchPath,
    objective: Objective | None,
    *,
    stopped_reason: str | None = None,
) -> Plan3SearchPath:
    return replace(
        path,
        evaluation=evaluate_plan3_search_state(path.state, objective),
        stopped_reason=(
            path.stopped_reason if stopped_reason is None else stopped_reason
        ),
    )


def search_plan3_deterministic(
    initial_state: Plan3State,
    *,
    beam_width: int = 64,
    depth: int | None = None,
    settings: Plan3ExamSettings | None = None,
    gimmick_profile: Plan3GimmickProfile | None = None,
    objective: Objective | None = None,
    database: Path = DEFAULT_DATABASE,
    future_upgrade_uncertain: bool = False,
    authoritative_future_upgrades: bool = False,
    draw_upgrade_resolver: DrawUpgradeResolver | None = None,
) -> Plan3SearchResult:
    """Search all exact legal card choices, retaining the best beam.

    ``depth`` counts card plays, not deterministic turn-boundary operations.
    With ``depth=None`` the search continues until the exam completes or every
    branch reaches an explicit unsupported/stalled boundary.
    """

    if (
        not isinstance(beam_width, int)
        or isinstance(beam_width, bool)
        or beam_width < 1
    ):
        raise ValueError("beam_width must be a positive integer")
    if depth is not None and (
        not isinstance(depth, int) or isinstance(depth, bool) or depth < 0
    ):
        raise ValueError("depth must be a non-negative integer or None")
    if authoritative_future_upgrades and draw_upgrade_resolver is not None:
        raise ValueError(
            "choose authoritative_future_upgrades or draw_upgrade_resolver, not both"
        )
    initial_state.validate(allow_completed=True)
    if settings is None:
        settings = load_plan3_exam_settings()

    card_cache: dict[tuple[str, int], Plan3Card] = {}
    diagnostics: list[Plan3SearchDiagnostic] = []
    finished: list[Plan3SearchPath] = []
    expanded_nodes = 0
    deduplicated_nodes = 0

    def path_for(
        state: Plan3State, steps: tuple[Plan3SearchStep, ...]
    ) -> Plan3SearchPath:
        return Plan3SearchPath(
            state=state,
            steps=steps,
            evaluation=evaluate_plan3_search_state(state, objective),
        )

    def diagnose(
        path: Plan3SearchPath,
        stage: str,
        *rules: str,
        card_ref: Plan3CardRef | None = None,
    ) -> None:
        diagnostics.append(
            Plan3SearchDiagnostic(
                stage=stage,
                rules=tuple(dict.fromkeys(rules)),
                state=path.state,
                actions=path.actions,
                card_ref=card_ref,
            )
        )

    def resolve_draw_upgrades(
        state: Plan3State,
        drawn: tuple[Plan3CardRef, ...],
    ) -> tuple[Plan3CardRef, ...]:
        if draw_upgrade_resolver is None:
            return drawn
        resolved = tuple(draw_upgrade_resolver(state, drawn))
        if len(resolved) != len(drawn):
            raise ValueError("draw_upgrade_resolver changed draw count")
        for base, effective in zip(drawn, resolved, strict=True):
            if not isinstance(effective, Plan3CardRef):
                raise TypeError("draw_upgrade_resolver must return Plan3CardRef values")
            if base.card_id != effective.card_id:
                raise ValueError("draw_upgrade_resolver changed card identity/order")
        return resolved

    def start_known_turn(path: Plan3SearchPath) -> Plan3SearchPath | None:
        state = path.state
        if future_upgrade_uncertain and not (
            authoritative_future_upgrades or draw_upgrade_resolver is not None
        ):
            diagnose(
                path,
                "turn-start",
                "future-draw-upgrade-unresolved",
            )
            finished.append(
                _replace_evaluation(
                    path, objective, stopped_reason="unsupported-turn-start"
                )
            )
            return None

        draw_count = min(
            settings.turn_start_distribute,
            len(state.draw_pile) + len(state.discard_pile),
        )
        known_order = tuple((*state.draw_pile, *state.discard_pile))
        drawn_base = known_order[:draw_count]
        started = start_plan3_turn(
            state,
            authoritative_draw_order=known_order,
            settings=settings,
            gimmick_profile=gimmick_profile,
        )
        if not started.supported:
            diagnose(path, "turn-start", *started.unsupported_rules)
            finished.append(
                _replace_evaluation(
                    path, objective, stopped_reason="unsupported-turn-start"
                )
            )
            return None

        try:
            effective_drawn = resolve_draw_upgrades(state, drawn_base)
        except (TypeError, ValueError) as error:
            diagnose(path, "draw-upgrade", f"{type(error).__name__}:{error}")
            finished.append(
                _replace_evaluation(
                    path, objective, stopped_reason="unsupported-draw-upgrade"
                )
            )
            return None
        if effective_drawn != drawn_base:
            effective_hand = tuple((*started.held_cards_returned, *effective_drawn))
            effective_after = replace(started.after, hand=effective_hand)
            started = replace(
                started,
                after=effective_after,
                drawn_cards=effective_drawn,
            )
        step = Plan3SearchStep(
            kind="turn_start",
            before=state,
            after=started.after,
            turn_start=started,
        )
        return path_for(started.after, (*path.steps, step))

    def settle_boundary(path: Plan3SearchPath) -> Plan3SearchPath | None:
        state = path.state
        if state.turns_remaining == 0:
            return path
        if state.awaiting_turn_start:
            return start_known_turn(path)
        if state.plays_remaining > 0:
            return path
        ended = end_plan3_turn(state)
        if ended.turns_remaining == 0:
            step = Plan3SearchStep(
                kind="end_turn",
                before=state,
                after=ended,
                ended_state=ended,
            )
            return path_for(ended, (*path.steps, step))
        boundary_base = path_for(ended, path.steps)
        started_path = start_known_turn(boundary_base)
        if started_path is None:
            return None
        # Fold the deterministic end into the following turn-start step so the
        # trace exposes both states without spending search depth.
        prior_steps = path.steps
        start_step = started_path.steps[-1]
        folded = replace(
            start_step,
            kind="turn_boundary",
            before=state,
            ended_state=ended,
        )
        return path_for(started_path.state, (*prior_steps, folded))

    frontier = (path_for(initial_state, ()),)
    while frontier:
        children: list[Plan3SearchPath] = []
        for raw_path in frontier:
            path = settle_boundary(raw_path)
            if path is None:
                continue
            if path.complete:
                finished.append(path)
                continue
            if depth is not None and len(path.actions) >= depth:
                finished.append(
                    _replace_evaluation(path, objective, stopped_reason="depth-limit")
                )
                continue

            expanded_nodes += 1
            any_exact_child = False
            # Identical card refs are behaviorally indistinguishable to the
            # public kernel; expanding one avoids redundant duplicate slots.
            for ref in dict.fromkeys(path.state.hand):
                cache_key = (ref.card_id, ref.upgrade)
                try:
                    card = card_cache.get(cache_key)
                    if card is None:
                        card = load_plan3_card(
                            ref.card_id,
                            ref.upgrade,
                            database,
                        )
                        card_cache[cache_key] = card
                except (KeyError, TypeError, ValueError) as error:
                    diagnose(
                        path,
                        "card-load",
                        f"{type(error).__name__}:{error}",
                        card_ref=ref,
                    )
                    continue

                transition = apply_plan3_card(path.state, card, settings=settings)
                if not transition.supported:
                    diagnose(
                        path,
                        "card-apply",
                        *(transition.unsupported_rules or ("engine-unsupported",)),
                        card_ref=ref,
                    )
                    continue
                if not transition.legal:
                    continue
                if transition.unverified_rules:
                    diagnose(
                        path,
                        "card-apply",
                        *(f"unverified:{rule}" for rule in transition.unverified_rules),
                        card_ref=ref,
                    )
                    continue
                any_exact_child = True
                step = Plan3SearchStep(
                    kind="card",
                    before=path.state,
                    after=transition.after,
                    card_ref=ref,
                    card_transition=transition,
                )
                children.append(
                    path_for(transition.after, (*path.steps, step))
                )

            if not any_exact_child:
                finished.append(
                    _replace_evaluation(
                        path, objective, stopped_reason="no-supported-legal-card"
                    )
                )

        if not children:
            break
        by_state: dict[Plan3State, Plan3SearchPath] = {}
        for child in children:
            previous = by_state.get(child.state)
            if previous is None or _path_key(child) > _path_key(previous):
                if previous is not None:
                    deduplicated_nodes += 1
                by_state[child.state] = child
            else:
                deduplicated_nodes += 1
        ranked = sorted(by_state.values(), key=_path_key, reverse=True)
        frontier = tuple(ranked[:beam_width])

    ranked_finished = tuple(sorted(finished, key=_path_key, reverse=True))
    return Plan3SearchResult(
        best=(ranked_finished[0] if ranked_finished else None),
        candidates=ranked_finished,
        diagnostics=tuple(diagnostics),
        expanded_nodes=expanded_nodes,
        deduplicated_nodes=deduplicated_nodes,
        beam_width=beam_width,
        depth=depth,
    )


__all__ = [
    "DrawUpgradeResolver",
    "Objective",
    "Plan3SearchDiagnostic",
    "Plan3SearchEvaluation",
    "Plan3SearchPath",
    "Plan3SearchResult",
    "Plan3SearchStep",
    "evaluate_plan3_search_state",
    "search_plan3_deterministic",
]
