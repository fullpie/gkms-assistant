"""Agent-free Plan2 decisions over the exact native horizon transition.

The decision layer never interprets Master rows and never mutates the horizon
catalog.  It enumerates normal Hand plays plus an explicit early END_TURN when
the current native play allowance does not require consuming an extra play,
and delegates each player input to
:func:`plan2_native_horizon.simulate_plan2_native_action_lifecycle`.  A PLAY
that consumes the last playable-card count therefore already includes the
native automatic EndTurn/TurnStart suffix and never becomes a second UI
action.

Current horizon transitions carry a fixed native RNG state and are therefore
deterministic.  Expectimax still consumes an explicit outcome-model protocol:
the default model returns the one settled transition with probability 1.0,
while a future reviewed chance boundary can provide several exact outcomes
without changing action selection or evaluation code.

The heuristic follows Plan2 好印象 resource semantics.  Realized score is the
base value; Review is valued through ``Plan2State.get_review_raw_score()``,
Aggressive is a future Block enabler, and Block/Stamina retain short-horizon
payment value.  Those shadow values decay with remaining turns and become
zero at terminal, so late searches prefer converting accumulated resources
into realized score rather than ending with unused setup.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Protocol, Sequence, runtime_checkable

from .native_exam_formula import ProduceParameterType
from .plan2_native_horizon import (
    Plan2NativeAction,
    Plan2NativeBlocker,
    Plan2NativeDrinkAction,
    Plan2NativeHorizonState,
    Plan2NativeOfflineAction,
    Plan2NativePlayability,
    Plan2NativeProgramCatalog,
    Plan2NativeTransition,
    enumerate_plan2_native_drink_actions,
    evaluate_plan2_native_playability,
    simulate_plan2_native_action_lifecycle,
)


PLAN2_NATIVE_EXPECTIMAX_SCHEMA_VERSION = 1
PLAN2_NATIVE_MAX_ROOT_ACTION_BONUS = 100.0
# A tie-break rank is deliberately separate from ``root_action_bonus``.  The
# latter is a numeric value and may change native beam/value ordering; this
# bound is only used after the exact native expected value is equal.
PLAN2_NATIVE_MAX_ROOT_ACTION_TIE_BREAK = 1000.0

# ``produce-001`` currently tops out at 12 base turns (auditions 9--12;
# Plan2 lessons 6--9).  Review can score at every remaining turn, so a
# three-turn default horizon systematically discards most of its value at
# early nodes.  Sixteen deliberately leaves four turns of solver headroom; it
# is not a native hard maximum, and callers with longer settled horizons can
# raise the cap.
PLAN2_NATIVE_DEFAULT_REVIEW_TURN_CAP = 16


class Plan2NativeExpectimaxError(ValueError):
    """A planner input or chance expansion is outside the bounded contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.blocker = Plan2NativeBlocker(code, detail)
        super().__init__(code if not detail else f"{code}:{detail}")


@runtime_checkable
class Plan2NativeLeafValueModel(Protocol):
    """Optional non-terminal cutoff evaluator.

    Native transitions, terminal scores, action legality, and beam ordering
    remain authoritative.  A learned model may estimate only a non-terminal
    leaf reached at the configured search cutoff.
    """

    def value(
        self,
        state: Plan2NativeHorizonState,
        base_evaluation: "Plan2NativeEvaluation",
    ) -> float: ...


@runtime_checkable
class Plan2NativeRootActionBonus(Protocol):
    """Bounded preference over actions already admitted at the root."""

    def __call__(
        self,
        state: Plan2NativeHorizonState,
        action: Plan2NativeOfflineAction,
        legal_actions: tuple[Plan2NativeOfflineAction, ...],
    ) -> float: ...


@runtime_checkable
class Plan2NativeRootActionTieBreaker(Protocol):
    """Bounded ordering hint used only after equal native values.

    Unlike :class:`Plan2NativeRootActionBonus`, this hook never contributes to
    an evaluation or terminal score.  It can select between equal exact
    ``expected`` values, and can order equal-immediate-value beam entries, but
    it cannot promote a lower native objective.
    """

    def __call__(
        self,
        state: Plan2NativeHorizonState,
        action: Plan2NativeOfflineAction,
        legal_actions: tuple[Plan2NativeOfflineAction, ...],
    ) -> float: ...


def _plain_positive(value: object, label: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise Plan2NativeExpectimaxError("invalid-planner-limit", label)
    return value


def _finite_nonnegative(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Plan2NativeExpectimaxError("invalid-evaluation-weight", label)
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise Plan2NativeExpectimaxError("invalid-evaluation-weight", label)
    return result


@dataclass(frozen=True, slots=True)
class Plan2NativeEvaluationWeights:
    """Small, explicit shadow prices for non-terminal Plan2 resources.

    The default Review cap covers every base turn in the current Initial
    Regular Master stages.  It is still an explicit cap, rather than an
    unbounded projection, so scenarios with runtime-added turns must opt in
    to valuing a longer horizon. ``review_per_turn`` is the conservative
    fallback only when the horizon has no complete authoritative scoring
    schedule for every valued opportunity.
    """

    score: float = 1.0
    review_per_turn: float = 0.55
    aggressive_per_turn: float = 0.18
    block_per_turn: float = 0.10
    stamina_per_turn: float = 0.08
    remaining_turn: float = 0.02
    review_turn_cap: int = PLAN2_NATIVE_DEFAULT_REVIEW_TURN_CAP
    defensive_turn_cap: int = 2

    def __post_init__(self) -> None:
        for name in (
            "score",
            "review_per_turn",
            "aggressive_per_turn",
            "block_per_turn",
            "stamina_per_turn",
            "remaining_turn",
        ):
            _finite_nonnegative(getattr(self, name), name)
        _plain_positive(self.review_turn_cap, "review_turn_cap")
        _plain_positive(self.defensive_turn_cap, "defensive_turn_cap")


@dataclass(frozen=True, slots=True)
class Plan2NativeEvaluation:
    """Auditable value components for one settled horizon state."""

    realized_score: float
    review_value: float
    aggressive_value: float
    block_value: float
    stamina_value: float
    remaining_turn_value: float
    total: float
    remaining_turns: int
    terminal: bool

    def __post_init__(self) -> None:
        for name in (
            "realized_score",
            "review_value",
            "aggressive_value",
            "block_value",
            "stamina_value",
            "remaining_turn_value",
            "total",
        ):
            value = getattr(self, name)
            if not isinstance(value, float) or not math.isfinite(value):
                raise Plan2NativeExpectimaxError("non-finite-evaluation", name)
        if type(self.remaining_turns) is not int or self.remaining_turns < 0:
            raise Plan2NativeExpectimaxError(
                "invalid-evaluation-remaining-turns"
            )
        if type(self.terminal) is not bool:
            raise TypeError("terminal must be bool")

    def to_dict(self) -> dict[str, object]:
        return {
            "realized_score": self.realized_score,
            "review_value": self.review_value,
            "aggressive_value": self.aggressive_value,
            "block_value": self.block_value,
            "stamina_value": self.stamina_value,
            "remaining_turn_value": self.remaining_turn_value,
            "total": self.total,
            "remaining_turns": self.remaining_turns,
            "terminal": self.terminal,
        }


def _authoritative_review_multiplier_sum_permille(
    state: Plan2NativeHorizonState,
    opportunities: int,
) -> int | None:
    """Return the exact known Review multiplier sum for the cutoff horizon.

    Auditions serialize one parameter type per scheduled turn and three
    corresponding battle bonus values.  Lessons deliberately serialize no
    turn schedule and use the native identity (1000 permille) score path.
    A partial extra-turn schedule cannot value the requested cutoff exactly,
    so callers must use their conservative fallback for the whole projection.
    """

    scoring = state.scalar.battle_scoring
    if scoring is None:
        return None
    if (
        scoring.current_turn != state.scalar.current_turn
        or scoring.limit_turn != state.limit_turn
        or scoring.extra_turn != state.extra_turn
        or scoring.exam_mode != state.exam_mode
    ):
        return None
    if scoring.exam_mode.is_lesson:
        return opportunities * 1000

    start = scoring.current_turn - 1
    end = start + opportunities
    if start < 0 or end > len(scoring.turn_parameter_types):
        return None
    bonus_by_parameter = {
        ProduceParameterType.VOCAL: scoring.vocal_bonus_permille,
        ProduceParameterType.DANCE: scoring.dance_bonus_permille,
        ProduceParameterType.VISUAL: scoring.visual_bonus_permille,
    }
    try:
        return sum(
            bonus_by_parameter[parameter_type]
            for parameter_type in scoring.turn_parameter_types[start:end]
        )
    except KeyError:
        # The typed context currently rejects UNKNOWN, but retaining this
        # boundary keeps evaluation conservative if that contract broadens.
        return None


def evaluate_plan2_native_state(
    state: Plan2NativeHorizonState,
    weights: Plan2NativeEvaluationWeights = Plan2NativeEvaluationWeights(),
) -> Plan2NativeEvaluation:
    """Evaluate score and temporary Plan2 resources without mutating state."""

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    if not isinstance(weights, Plan2NativeEvaluationWeights):
        raise TypeError("weights must be Plan2NativeEvaluationWeights")

    scalar = state.scalar
    score_value = float(scalar.score) * weights.score
    remaining = state.remaining_turns
    if state.terminal:
        return Plan2NativeEvaluation(
            score_value,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            score_value,
            remaining,
            True,
        )

    review_opportunities = min(remaining, weights.review_turn_cap)
    defensive_opportunities = min(remaining, weights.defensive_turn_cap)
    review_multiplier_permille = (
        _authoritative_review_multiplier_sum_permille(
            state,
            review_opportunities,
        )
    )
    if review_multiplier_permille is None:
        review_value = (
            float(scalar.get_review_raw_score())
            * weights.review_per_turn
            * review_opportunities
        )
    else:
        review_value = (
            float(scalar.get_review_raw_score())
            * float(review_multiplier_permille)
            / 1000.0
        )
    aggressive_value = (
        float(scalar.card_play_aggressive)
        * weights.aggressive_per_turn
        * review_opportunities
    )
    block_value = (
        float(scalar.block)
        * weights.block_per_turn
        * defensive_opportunities
    )
    stamina_value = (
        float(scalar.stamina)
        * weights.stamina_per_turn
        * defensive_opportunities
    )
    remaining_value = float(remaining) * weights.remaining_turn
    total = (
        score_value
        + review_value
        + aggressive_value
        + block_value
        + stamina_value
        + remaining_value
    )
    return Plan2NativeEvaluation(
        score_value,
        review_value,
        aggressive_value,
        block_value,
        stamina_value,
        remaining_value,
        total,
        remaining,
        False,
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeActionEnumeration:
    actions: tuple[Plan2NativeOfflineAction, ...]
    playability: tuple[Plan2NativePlayability, ...]
    blockers: tuple[Plan2NativeBlocker, ...]

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, (Plan2NativeAction, Plan2NativeDrinkAction))
            for value in self.actions
        ):
            raise TypeError("actions must contain typed Plan2 offline actions")
        if any(
            not isinstance(value, Plan2NativePlayability)
            for value in self.playability
        ):
            raise TypeError("playability must contain Plan2NativePlayability")
        if any(not isinstance(value, Plan2NativeBlocker) for value in self.blockers):
            raise TypeError("blockers must contain Plan2NativeBlocker")


def enumerate_plan2_native_actions(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
) -> Plan2NativeActionEnumeration:
    """Enumerate drink slots, playable Hand GUIDs, then a legal early end."""

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if state.terminal:
        return Plan2NativeActionEnumeration((), (), ())

    playability: list[Plan2NativePlayability] = []
    actions: list[Plan2NativeOfflineAction] = list(
        enumerate_plan2_native_drink_actions(state, catalog)
    )
    blockers: list[Plan2NativeBlocker] = []
    for card in state.zones.hand:
        result = evaluate_plan2_native_playability(state, card.guid, catalog)
        playability.append(result)
        if result.playable:
            actions.append(Plan2NativeAction("play", card.guid))
        else:
            blockers.extend(result.blockers)
    # Runtime-verified ``ExamScreenPresenter.UpdateEndButton`` keeps END_TURN
    # available even while an extra play remains.  Candidate enumeration is a
    # legality surface, not a heuristic; whether spending that extra play is
    # wise belongs to the selected policy/search value.
    actions.append(Plan2NativeAction("end_turn"))
    return Plan2NativeActionEnumeration(
        tuple(actions),
        tuple(playability),
        tuple(dict.fromkeys(blockers)),
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeChanceOutcome:
    """One exact settled outcome of an already-supported action transition."""

    probability: float
    state: Plan2NativeHorizonState
    label: str

    def __post_init__(self) -> None:
        probability = _finite_nonnegative(self.probability, "probability")
        if probability <= 0.0 or probability > 1.0:
            raise Plan2NativeExpectimaxError(
                "invalid-chance-probability", repr(self.probability)
            )
        if not isinstance(self.state, Plan2NativeHorizonState):
            raise TypeError("chance state must be Plan2NativeHorizonState")
        if type(self.label) is not str or not self.label:
            raise Plan2NativeExpectimaxError("invalid-chance-label")


@runtime_checkable
class Plan2NativeChanceModel(Protocol):
    """Future chance-node hook; implementations must return total mass 1."""

    def outcomes(
        self, transition: Plan2NativeTransition
    ) -> tuple[Plan2NativeChanceOutcome, ...]: ...


@dataclass(frozen=True, slots=True)
class DeterministicPlan2NativeChanceModel:
    """Current exact model: fixed RNG makes a supported transition singular."""

    label: str = "deterministic-fixed-rng"

    def outcomes(
        self, transition: Plan2NativeTransition
    ) -> tuple[Plan2NativeChanceOutcome, ...]:
        if not isinstance(transition, Plan2NativeTransition):
            raise TypeError("transition must be Plan2NativeTransition")
        if not transition.supported or transition.after is None:
            raise Plan2NativeExpectimaxError(
                "chance-model-received-unsupported-transition"
            )
        return (Plan2NativeChanceOutcome(1.0, transition.after, self.label),)


def _validated_outcomes(
    model: Plan2NativeChanceModel,
    transition: Plan2NativeTransition,
) -> tuple[Plan2NativeChanceOutcome, ...]:
    try:
        outcomes = tuple(model.outcomes(transition))
    except Plan2NativeExpectimaxError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise Plan2NativeExpectimaxError(
            "chance-model-failed", f"{type(error).__name__}:{error}"
        ) from error
    if not outcomes or any(
        not isinstance(value, Plan2NativeChanceOutcome) for value in outcomes
    ):
        raise Plan2NativeExpectimaxError("chance-model-invalid-outcomes")
    mass = math.fsum(value.probability for value in outcomes)
    if not math.isclose(mass, 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise Plan2NativeExpectimaxError(
            "chance-probability-mass", repr(mass)
        )
    return outcomes


@dataclass(frozen=True, slots=True)
class Plan2NativeExpectimaxLimits:
    max_depth: int = 8
    beam_width: int = 8
    max_nodes: int = 5_000

    def __post_init__(self) -> None:
        _plain_positive(self.max_depth, "max_depth")
        _plain_positive(self.beam_width, "beam_width")
        _plain_positive(self.max_nodes, "max_nodes")


@dataclass(frozen=True, slots=True)
class Plan2NativeDecisionStep:
    depth: int
    action: Plan2NativeOfflineAction
    expected_value: float
    chance_label: str
    transition_trace: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.depth) is not int or self.depth < 0:
            raise Plan2NativeExpectimaxError("invalid-decision-depth")
        if not isinstance(
            self.action, (Plan2NativeAction, Plan2NativeDrinkAction)
        ):
            raise TypeError("action must be a typed Plan2 offline action")
        if not isinstance(self.expected_value, float) or not math.isfinite(
            self.expected_value
        ):
            raise Plan2NativeExpectimaxError("invalid-decision-value")
        if type(self.chance_label) is not str or not self.chance_label:
            raise Plan2NativeExpectimaxError("invalid-decision-chance-label")
        if any(type(value) is not str for value in self.transition_trace):
            raise TypeError("transition_trace must contain text")

    @property
    def action_id(self) -> str:
        return self.action.action_id

    def to_dict(self) -> dict[str, object]:
        return {
            "depth": self.depth,
            "action": self.action_id,
            "expected_value": self.expected_value,
            "chance_label": self.chance_label,
            "transition_trace": list(self.transition_trace),
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeExpectimaxResult:
    schema_version: int
    root: Plan2NativeHorizonState
    best_action: Plan2NativeOfflineAction | None
    value: float
    root_evaluation: Plan2NativeEvaluation
    principal_leaf_evaluation: Plan2NativeEvaluation
    path: tuple[Plan2NativeDecisionStep, ...]
    blockers: tuple[Plan2NativeBlocker, ...]
    nodes_evaluated: int
    transitions_evaluated: int
    maximum_depth_reached: int
    beam_pruned_actions: int
    terminal_reached: bool
    horizon_complete: bool

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_EXPECTIMAX_SCHEMA_VERSION:
            raise Plan2NativeExpectimaxError("invalid-result-schema")
        if not isinstance(self.root, Plan2NativeHorizonState):
            raise TypeError("root must be Plan2NativeHorizonState")
        if self.best_action is not None and not isinstance(
            self.best_action, (Plan2NativeAction, Plan2NativeDrinkAction)
        ):
            raise TypeError("best_action must be a typed offline action or None")
        if not isinstance(self.value, float) or not math.isfinite(self.value):
            raise Plan2NativeExpectimaxError("invalid-result-value")
        if self.path and self.best_action != self.path[0].action:
            raise Plan2NativeExpectimaxError("best-action-path-mismatch")
        if not self.path and self.best_action is not None:
            raise Plan2NativeExpectimaxError("best-action-without-path")
        for name in (
            "nodes_evaluated",
            "transitions_evaluated",
            "maximum_depth_reached",
            "beam_pruned_actions",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise Plan2NativeExpectimaxError("invalid-result-counter", name)

    @property
    def action_path(self) -> tuple[str, ...]:
        return tuple(step.action_id for step in self.path)

    @property
    def fail_closed(self) -> bool:
        return self.best_action is None and not self.root.terminal

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "best_action": (
                None if self.best_action is None else self.best_action.action_id
            ),
            "value": self.value,
            "root_evaluation": self.root_evaluation.to_dict(),
            "principal_leaf_evaluation": (
                self.principal_leaf_evaluation.to_dict()
            ),
            "path": [step.to_dict() for step in self.path],
            "blockers": [blocker.to_dict() for blocker in self.blockers],
            "nodes_evaluated": self.nodes_evaluated,
            "transitions_evaluated": self.transitions_evaluated,
            "maximum_depth_reached": self.maximum_depth_reached,
            "beam_pruned_actions": self.beam_pruned_actions,
            "terminal_reached": self.terminal_reached,
            "horizon_complete": self.horizon_complete,
            "fail_closed": self.fail_closed,
        }


@dataclass(frozen=True, slots=True)
class _NodeValue:
    value: float
    leaf_evaluation: Plan2NativeEvaluation
    path: tuple[Plan2NativeDecisionStep, ...]
    terminal_reached: bool
    complete: bool


@dataclass(frozen=True, slots=True)
class _ActionCandidate:
    order: int
    action: Plan2NativeOfflineAction
    transition: Plan2NativeTransition
    outcomes: tuple[Plan2NativeChanceOutcome, ...]
    immediate_value: float
    root_bonus: float = 0.0
    root_tie_break: float = 0.0


class _Planner:
    def __init__(
        self,
        catalog: Plan2NativeProgramCatalog,
        weights: Plan2NativeEvaluationWeights,
        limits: Plan2NativeExpectimaxLimits,
        chance_model: Plan2NativeChanceModel,
        leaf_value_model: Plan2NativeLeafValueModel | None,
        root_action_bonus: Plan2NativeRootActionBonus | None,
        root_action_tiebreaker: Plan2NativeRootActionTieBreaker | None,
    ) -> None:
        self.catalog = catalog
        self.weights = weights
        self.limits = limits
        self.chance_model = chance_model
        self.leaf_value_model = leaf_value_model
        self.root_action_bonus = root_action_bonus
        self.root_action_tiebreaker = root_action_tiebreaker
        self.nodes = 0
        self.transitions = 0
        self.maximum_depth = 0
        self.beam_pruned = 0
        self.blockers: list[Plan2NativeBlocker] = []
        self.node_limit_reported = False

    def add_blockers(self, values: Sequence[Plan2NativeBlocker]) -> None:
        self.blockers.extend(values)

    def evaluate(self, state: Plan2NativeHorizonState) -> Plan2NativeEvaluation:
        return evaluate_plan2_native_state(state, self.weights)

    def leaf_value(
        self,
        state: Plan2NativeHorizonState,
        evaluation: Plan2NativeEvaluation,
    ) -> float:
        if state.terminal or self.leaf_value_model is None:
            return evaluation.total
        try:
            result = float(self.leaf_value_model.value(state, evaluation))
        except Plan2NativeExpectimaxError:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise Plan2NativeExpectimaxError(
                "leaf-value-model-failed",
                f"{type(error).__name__}:{error}",
            ) from error
        if not math.isfinite(result):
            raise Plan2NativeExpectimaxError("leaf-value-model-non-finite")
        return result

    def visit(self, state: Plan2NativeHorizonState, depth: int) -> _NodeValue:
        self.maximum_depth = max(self.maximum_depth, depth)
        evaluation = self.evaluate(state)
        if self.nodes >= self.limits.max_nodes:
            if not self.node_limit_reported:
                self.blockers.append(
                    Plan2NativeBlocker(
                        "expectimax-node-limit", str(self.limits.max_nodes)
                    )
                )
                self.node_limit_reported = True
            return _NodeValue(
                self.leaf_value(state, evaluation),
                evaluation,
                (),
                state.terminal,
                False,
            )
        self.nodes += 1
        if state.terminal:
            return _NodeValue(evaluation.total, evaluation, (), True, True)
        if depth >= self.limits.max_depth:
            return _NodeValue(
                self.leaf_value(state, evaluation),
                evaluation,
                (),
                False,
                False,
            )

        enumeration = enumerate_plan2_native_actions(state, self.catalog)
        self.add_blockers(enumeration.blockers)
        candidates: list[_ActionCandidate] = []
        for order, action in enumerate(enumeration.actions):
            transition = simulate_plan2_native_action_lifecycle(
                state, action, self.catalog
            )
            self.transitions += 1
            if not transition.supported:
                self.add_blockers(transition.blockers)
                continue
            try:
                outcomes = _validated_outcomes(self.chance_model, transition)
            except Plan2NativeExpectimaxError as error:
                self.blockers.append(error.blocker)
                continue
            immediate = math.fsum(
                outcome.probability * self.evaluate(outcome.state).total
                for outcome in outcomes
            )
            root_bonus = 0.0
            if depth == 0 and self.root_action_bonus is not None:
                try:
                    root_bonus = float(
                        self.root_action_bonus(
                            state,
                            action,
                            tuple(enumeration.actions),
                        )
                    )
                except (TypeError, ValueError) as error:
                    self.blockers.append(
                        Plan2NativeBlocker(
                            "root-action-bonus-failed",
                            f"{type(error).__name__}:{error}",
                        )
                    )
                    root_bonus = 0.0
                if (
                    not math.isfinite(root_bonus)
                    or abs(root_bonus) > PLAN2_NATIVE_MAX_ROOT_ACTION_BONUS
                ):
                    self.blockers.append(
                        Plan2NativeBlocker(
                            "root-action-bonus-out-of-bounds",
                            repr(root_bonus),
                        )
                    )
                    root_bonus = 0.0
            root_tie_break = 0.0
            if depth == 0 and self.root_action_tiebreaker is not None:
                try:
                    root_tie_break = float(
                        self.root_action_tiebreaker(
                            state,
                            action,
                            tuple(enumeration.actions),
                        )
                    )
                except (TypeError, ValueError) as error:
                    self.blockers.append(
                        Plan2NativeBlocker(
                            "root-action-tie-break-failed",
                            f"{type(error).__name__}:{error}",
                        )
                    )
                    root_tie_break = 0.0
                if (
                    not math.isfinite(root_tie_break)
                    or abs(root_tie_break)
                    > PLAN2_NATIVE_MAX_ROOT_ACTION_TIE_BREAK
                ):
                    self.blockers.append(
                        Plan2NativeBlocker(
                            "root-action-tie-break-out-of-bounds",
                            repr(root_tie_break),
                        )
                    )
                    root_tie_break = 0.0
            candidates.append(
                _ActionCandidate(
                    order,
                    action,
                    transition,
                    outcomes,
                    immediate + root_bonus,
                    root_bonus,
                    root_tie_break,
                )
            )

        if not candidates:
            self.blockers.append(Plan2NativeBlocker("expectimax-no-supported-actions"))
            return _NodeValue(evaluation.total, evaluation, (), False, False)

        ranked = sorted(
            candidates,
            # Ordering hints may only break an equal native immediate value;
            # the exact value remains the first sort key.
            key=lambda value: (
                -value.immediate_value,
                -value.root_tie_break,
                value.order,
            ),
        )
        kept = ranked[: self.limits.beam_width]
        pruned_here = len(ranked) - len(kept)
        self.beam_pruned += pruned_here

        best_value = -math.inf
        best_path: tuple[Plan2NativeDecisionStep, ...] = ()
        best_leaf = evaluation
        best_terminal = False
        best_complete = False
        best_tie_break = -math.inf
        for candidate in kept:
            expected = candidate.root_bonus
            branches: list[tuple[Plan2NativeChanceOutcome, _NodeValue]] = []
            action_complete = pruned_here == 0
            action_terminal = True
            for outcome in candidate.outcomes:
                child = self.visit(outcome.state, depth + 1)
                expected += outcome.probability * child.value
                branches.append((outcome, child))
                action_complete = action_complete and child.complete
                action_terminal = action_terminal and child.terminal_reached
            principal_outcome, principal = max(
                branches,
                key=lambda pair: (
                    pair[0].probability,
                    pair[1].value,
                    pair[0].label,
                ),
            )
            step = Plan2NativeDecisionStep(
                depth,
                candidate.action,
                float(expected),
                principal_outcome.label,
                (
                    *candidate.transition.trace,
                    *(
                        ()
                        if candidate.root_bonus == 0.0
                        else (
                            f"root-action-advisory-bonus:{candidate.root_bonus:.6f}",
                        )
                    ),
                    *(
                        ()
                        if candidate.root_tie_break == 0.0
                        else (
                            f"root-action-advisory-tie-break:{candidate.root_tie_break:.6f}",
                        )
                    ),
                ),
            )
            path = (step, *principal.path)
            if expected > best_value or (
                expected == best_value
                and candidate.root_tie_break > best_tie_break
            ):
                best_value = expected
                best_path = path
                best_leaf = principal.leaf_evaluation
                best_terminal = action_terminal
                best_complete = action_complete
                best_tie_break = candidate.root_tie_break

        return _NodeValue(
            float(best_value),
            best_leaf,
            best_path,
            best_terminal,
            best_complete,
        )


def plan_plan2_native_expectimax(
    root: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    *,
    weights: Plan2NativeEvaluationWeights = Plan2NativeEvaluationWeights(),
    limits: Plan2NativeExpectimaxLimits = Plan2NativeExpectimaxLimits(),
    chance_model: Plan2NativeChanceModel | None = None,
    leaf_value_model: Plan2NativeLeafValueModel | None = None,
    root_action_bonus: Plan2NativeRootActionBonus | None = None,
    root_action_tiebreaker: Plan2NativeRootActionTieBreaker | None = None,
) -> Plan2NativeExpectimaxResult:
    """Return one bounded best action, value, principal path, and blockers.

    ``root_action_tiebreaker`` is a bounded advisory ordering callback.  It is
    never added to the native value; it can only order equal-immediate beam
    nodes and select an equal exact ``expected`` result.  The older numeric
    ``root_action_bonus`` remains a separate model hook and is not the safe
    imitation-prior seam.
    """

    if not isinstance(root, Plan2NativeHorizonState):
        raise TypeError("root must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if not isinstance(weights, Plan2NativeEvaluationWeights):
        raise TypeError("weights must be Plan2NativeEvaluationWeights")
    if not isinstance(limits, Plan2NativeExpectimaxLimits):
        raise TypeError("limits must be Plan2NativeExpectimaxLimits")
    if leaf_value_model is not None and not isinstance(
        leaf_value_model, Plan2NativeLeafValueModel
    ):
        raise TypeError("leaf_value_model must implement Plan2NativeLeafValueModel")
    if root_action_bonus is not None and not isinstance(
        root_action_bonus, Plan2NativeRootActionBonus
    ):
        raise TypeError("root_action_bonus must implement Plan2NativeRootActionBonus")
    if root_action_tiebreaker is not None and not isinstance(
        root_action_tiebreaker, Plan2NativeRootActionTieBreaker
    ):
        raise TypeError(
            "root_action_tiebreaker must implement "
            "Plan2NativeRootActionTieBreaker"
        )
    if root_action_bonus is not None and root_action_tiebreaker is not None:
        raise ValueError(
            "root_action_bonus and root_action_tiebreaker are mutually exclusive"
        )
    model: Plan2NativeChanceModel = (
        DeterministicPlan2NativeChanceModel()
        if chance_model is None
        else chance_model
    )
    if not isinstance(model, Plan2NativeChanceModel):
        raise TypeError("chance_model must implement Plan2NativeChanceModel")

    planner = _Planner(
        catalog,
        weights,
        limits,
        model,
        leaf_value_model,
        root_action_bonus,
        root_action_tiebreaker,
    )
    root_evaluation = planner.evaluate(root)
    node = planner.visit(root, 0)
    blockers = tuple(dict.fromkeys(planner.blockers))
    best_action = node.path[0].action if node.path else None
    return Plan2NativeExpectimaxResult(
        PLAN2_NATIVE_EXPECTIMAX_SCHEMA_VERSION,
        root,
        best_action,
        node.value,
        root_evaluation,
        node.leaf_evaluation,
        node.path,
        blockers,
        planner.nodes,
        planner.transitions,
        planner.maximum_depth,
        planner.beam_pruned,
        node.terminal_reached,
        node.complete,
    )


choose_plan2_native_action = plan_plan2_native_expectimax
search_plan2_native_expectimax = plan_plan2_native_expectimax


__all__ = [
    "PLAN2_NATIVE_DEFAULT_REVIEW_TURN_CAP",
    "PLAN2_NATIVE_EXPECTIMAX_SCHEMA_VERSION",
    "PLAN2_NATIVE_MAX_ROOT_ACTION_BONUS",
    "PLAN2_NATIVE_MAX_ROOT_ACTION_TIE_BREAK",
    "DeterministicPlan2NativeChanceModel",
    "Plan2NativeActionEnumeration",
    "Plan2NativeChanceModel",
    "Plan2NativeChanceOutcome",
    "Plan2NativeDecisionStep",
    "Plan2NativeEvaluation",
    "Plan2NativeEvaluationWeights",
    "Plan2NativeExpectimaxError",
    "Plan2NativeExpectimaxLimits",
    "Plan2NativeExpectimaxResult",
    "Plan2NativeLeafValueModel",
    "Plan2NativeRootActionBonus",
    "Plan2NativeRootActionTieBreaker",
    "choose_plan2_native_action",
    "enumerate_plan2_native_actions",
    "evaluate_plan2_native_state",
    "plan_plan2_native_expectimax",
    "search_plan2_native_expectimax",
]
