"""Small deterministic advisor/search layer for :mod:`plan1_native_stage`.

This is a policy helper, not a stochastic simulator.  It explores only the
already-proven card transitions in the visible Hand, uses a bounded depth/node
budget, and resolves every tie by Hand index/card GUID.  No random roll or
synthetic turn rule is introduced.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Sequence

from .plan1_native_core import (
    Plan1Blocker,
    Plan1DeckCompilation,
    Plan1NativeSettings,
)
from .plan1_native_stage import (
    Plan1HandAddSupportResolver,
    Plan1NativeStageState,
    Plan1StageAction,
    apply_plan1_stage_action,
    enumerate_plan1_legal_actions,
)


@dataclass(frozen=True, slots=True)
class Plan1SearchLimits:
    """Hard bounds for the tiny deterministic card-only search."""

    max_depth: int = 2
    max_nodes: int = 64

    def __post_init__(self) -> None:
        if isinstance(self.max_depth, bool) or not isinstance(self.max_depth, int):
            raise TypeError("max_depth must be an integer")
        if self.max_depth < 1:
            raise ValueError("max_depth must be positive")
        if isinstance(self.max_nodes, bool) or not isinstance(self.max_nodes, int):
            raise TypeError("max_nodes must be an integer")
        if self.max_nodes < 1:
            raise ValueError("max_nodes must be positive")


@dataclass(frozen=True, slots=True)
class Plan1SearchResult:
    """Best bounded path found from one stage checkpoint."""

    actions: tuple[Plan1StageAction, ...]
    final_state: Plan1NativeStageState
    score: int
    nodes: int
    complete: bool
    # Explicit action blockers are retained when the root has no legal path.
    # This is materially different from ordinary ineligibility (for example,
    # zero playable values), which has no blocker payload.
    blockers: tuple[Plan1Blocker, ...] = ()

    def __post_init__(self) -> None:
        values = tuple(self.blockers)
        if any(not isinstance(value, Plan1Blocker) for value in values):
            raise TypeError("blockers must contain Plan1Blocker values")
        object.__setattr__(self, "blockers", values)

    @property
    def first_action(self) -> Plan1StageAction | None:
        return self.actions[0] if self.actions else None


@dataclass(frozen=True, slots=True)
class Plan1AdvisorDecision:
    """Stable one-step advisor acceptance payload."""

    action: Plan1StageAction
    score_gain: int
    tie_key: tuple[int, str, str]


# Advisory scores are consulted only after the native score/stamina objective
# is equal.  A prior cannot supply or remove an action through this callback.
Plan1ActionTieBreaker = Callable[[Plan1StageAction], float]
PLAN1_MAX_ACTION_TIE_BREAK = 1000.0


def _candidate_key(action: Plan1StageAction) -> tuple[int, int, int, str, str]:
    transition = action.transition
    if transition is None:
        return (-1, -1, 0, action.card.card_id, action.guid)
    return (
        transition.after.score,
        transition.after.stamina,
        -action.hand_index,
        action.card.card_id,
        action.guid,
    )


def _path_key(actions: Sequence[Plan1StageAction]) -> tuple[str, ...]:
    return tuple(action.guid for action in actions)


def _better(
    left: tuple[int, tuple[str, ...], tuple[Plan1StageAction, ...], Plan1NativeStageState],
    right: tuple[int, tuple[str, ...], tuple[Plan1StageAction, ...], Plan1NativeStageState],
    *,
    action_tie_breaker: Plan1ActionTieBreaker | None = None,
) -> tuple[int, tuple[str, ...], tuple[Plan1StageAction, ...], Plan1NativeStageState]:
    # Score is primary.  The optional prior is strictly a tie-break and is
    # intentionally evaluated before the existing GUID path order only after
    # the exact native score has tied.
    if left[0] != right[0]:
        return left if left[0] > right[0] else right
    if action_tie_breaker is not None:
        # Keep the native secondary state value ahead of the advisory.  The
        # prior may only choose among paths tied on the native score/stamina
        # objective, not trade away a native resource for behavior similarity.
        left_stamina = left[3].scalar.stamina
        right_stamina = right[3].scalar.stamina
        if left_stamina != right_stamina:
            return left if left_stamina > right_stamina else right

        def tie_value(candidate: tuple[int, tuple[str, ...], tuple[Plan1StageAction, ...], Plan1NativeStageState]) -> float:
            if not candidate[2]:
                return 0.0
            try:
                value = float(action_tie_breaker(candidate[2][0]))
            except (AttributeError, TypeError, ValueError):
                return 0.0
            return value if math.isfinite(value) and abs(value) <= PLAN1_MAX_ACTION_TIE_BREAK else 0.0

        left_tie = tie_value(left)
        right_tie = tie_value(right)
        if left_tie != right_tie:
            return left if left_tie > right_tie else right
    return left if left[1] < right[1] else right


def search_plan1_stage(
    state: Plan1NativeStageState,
    compilation: Plan1DeckCompilation | Sequence,
    *,
    settings: Plan1NativeSettings | None = None,
    limits: Plan1SearchLimits = Plan1SearchLimits(),
    hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
    imitation_tie_breaker: Plan1ActionTieBreaker | None = None,
) -> Plan1SearchResult:
    """Search up to ``limits.max_depth`` legal cards in the current Hand.

    Search ends naturally when playable values are exhausted or no legal card
    remains.  If a node budget is reached, the best prefix found so far is
    returned with ``complete=False``; no approximation is silently presented as
    an exact full-horizon result.  HandAdd runtime is caller authority and is
    threaded explicitly into every preview; it is never installed globally.
    ``imitation_tie_breaker`` is advisory only: native score/stamina and the
    legal action set remain authoritative.
    """

    if not isinstance(state, Plan1NativeStageState):
        raise TypeError("state must be Plan1NativeStageState")
    if not isinstance(limits, Plan1SearchLimits):
        raise TypeError("limits must be Plan1SearchLimits")
    if imitation_tie_breaker is not None and not callable(imitation_tie_breaker):
        raise TypeError("imitation_tie_breaker must be callable or None")
    nodes = 0
    budget_exhausted = False
    root_score = state.scalar.score

    def visit(
        current: Plan1NativeStageState,
        depth: int,
    ) -> tuple[int, tuple[str, ...], tuple[Plan1StageAction, ...], Plan1NativeStageState]:
        nonlocal nodes, budget_exhausted
        nodes += 1
        if nodes > limits.max_nodes:
            budget_exhausted = True
            return (current.scalar.score, (), (), current)
        actions = tuple(
            action
            for action in enumerate_plan1_legal_actions(
                current,
                compilation,
                settings=settings,
                hand_add_support_resolver=hand_add_support_resolver,
            )
            if action.legal
        )
        if depth >= limits.max_depth or not actions:
            return (current.scalar.score, (), (), current)
        best: tuple[int, tuple[str, ...], tuple[Plan1StageAction, ...], Plan1NativeStageState] | None = None
        for action in actions:
            if nodes >= limits.max_nodes:
                budget_exhausted = True
                break
            next_state, _step = apply_plan1_stage_action(current, action)
            if next_state.blockers:
                continue
            child = visit(next_state, depth + 1)
            candidate = (
                child[0],
                (action.guid, *child[1]),
                (action, *child[2]),
                child[3],
            )
            best = (
                candidate
                if best is None
                else _better(
                    best,
                    candidate,
                    action_tie_breaker=imitation_tie_breaker,
                )
            )
        return best or (current.scalar.score, (), (), current)

    score, _path_key_value, actions, final_state = visit(state, 0)
    blockers: tuple[Plan1Blocker, ...] = ()
    if not actions:
        root_previews = enumerate_plan1_legal_actions(
            state,
            compilation,
            settings=settings,
            hand_add_support_resolver=hand_add_support_resolver,
        )
        if not any(action.legal for action in root_previews):
            blockers = tuple(
                dict.fromkeys(
                    blocker
                    for action in root_previews
                    for blocker in action.blockers
                )
            )
    return Plan1SearchResult(
        actions=actions,
        final_state=final_state,
        score=score,
        nodes=min(nodes, limits.max_nodes),
        complete=not budget_exhausted and not blockers,
        blockers=blockers,
    )


def recommend_plan1_action(
    state: Plan1NativeStageState,
    compilation: Plan1DeckCompilation | Sequence,
    *,
    settings: Plan1NativeSettings | None = None,
    hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
    imitation_tie_breaker: Plan1ActionTieBreaker | None = None,
) -> Plan1AdvisorDecision | None:
    """Return a deterministic one-step accepted action, if one exists.

    Cards that require HandAdd remain non-legal unless the caller supplies the
    same typed resolver used by stage execution.  An optional
    ``imitation_tie_breaker`` can order equal native score/stamina choices.
    """

    actions = tuple(
        action
        for action in enumerate_plan1_legal_actions(
            state,
            compilation,
            settings=settings,
            hand_add_support_resolver=hand_add_support_resolver,
        )
        if action.legal
    )
    if not actions:
        return None
    if imitation_tie_breaker is not None and not callable(imitation_tie_breaker):
        raise TypeError("imitation_tie_breaker must be callable or None")

    def action_key(action: Plan1StageAction) -> tuple[object, ...]:
        base = _candidate_key(action)
        if imitation_tie_breaker is None:
            return base
        try:
            advisory = float(imitation_tie_breaker(action))
        except (AttributeError, TypeError, ValueError):
            advisory = 0.0
        if not math.isfinite(advisory) or abs(advisory) > PLAN1_MAX_ACTION_TIE_BREAK:
            advisory = 0.0
        # Native score/stamina remain the leading objective fields.  The
        # advisory value is inserted before deterministic Hand/GUID order.
        return (base[0], base[1], advisory, *base[2:])

    selected = max(actions, key=action_key)
    transition = selected.transition
    assert transition is not None
    return Plan1AdvisorDecision(
        action=selected,
        score_gain=transition.after.score - transition.before.score,
        tie_key=(selected.hand_index, selected.card.card_id, selected.guid),
    )


def choose_plan1_action(
    state: Plan1NativeStageState,
    actions: tuple[Plan1StageAction, ...],
    *,
    imitation_tie_breaker: Plan1ActionTieBreaker | None = None,
) -> Plan1StageAction | None:
    """ActionChooser-compatible deterministic acceptance function."""

    legal = tuple(action for action in actions if action.legal)
    if not legal:
        return None
    if imitation_tie_breaker is not None and not callable(imitation_tie_breaker):
        raise TypeError("imitation_tie_breaker must be callable or None")
    if imitation_tie_breaker is None:
        return max(legal, key=_candidate_key)

    def action_key(action: Plan1StageAction) -> tuple[object, ...]:
        base = _candidate_key(action)
        try:
            advisory = float(imitation_tie_breaker(action))
        except (AttributeError, TypeError, ValueError):
            advisory = 0.0
        if not math.isfinite(advisory) or abs(advisory) > PLAN1_MAX_ACTION_TIE_BREAK:
            advisory = 0.0
        return (base[0], base[1], advisory, *base[2:])

    return max(legal, key=action_key)


def accept_plan1_advisor_action(
    state: Plan1NativeStageState,
    decision: Plan1AdvisorDecision | None,
) -> Plan1NativeStageState:
    """Apply a previously returned decision, preserving stale-action blockers."""

    if decision is None:
        return state
    next_state, _step = apply_plan1_stage_action(state, decision.action)
    return next_state


# Descriptive aliases for callers that use “advisor” terminology.
deterministic_plan1_advisor = recommend_plan1_action
accept_plan1_action = accept_plan1_advisor_action


__all__ = [
    "Plan1AdvisorDecision",
    "Plan1ActionTieBreaker",
    "PLAN1_MAX_ACTION_TIE_BREAK",
    "Plan1SearchLimits",
    "Plan1SearchResult",
    "accept_plan1_action",
    "accept_plan1_advisor_action",
    "choose_plan1_action",
    "deterministic_plan1_advisor",
    "recommend_plan1_action",
    "search_plan1_stage",
]
