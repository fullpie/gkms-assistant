"""Load a promoted Plan2 strategy without weakening native legality.

The champion supplies only a bounded residual leaf estimate.  Native Master
programs still enumerate legal cards, execute effects, own terminal scores,
and provide the baseline evaluation.  Unvalidated modes, idols and horizons
automatically retain the ordinary native planner.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import json
from pathlib import Path
from typing import Final, Mapping

from .master_db import DEFAULT_DATABASE, PROJECT_ROOT, get_idol_profile
from .plan2_native_expectimax import (
    Plan2NativeEvaluationWeights,
    Plan2NativeExpectimaxLimits,
    Plan2NativeExpectimaxResult,
    Plan2NativeRootActionBonus,
    plan_plan2_native_expectimax,
)
from .plan2_native_horizon import (
    Plan2NativeAction,
    Plan2NativeHorizonState,
    Plan2NativeOfflineAction,
    Plan2NativeProgramCatalog,
)
from .plan2_strategy_learning import Plan2ValueNetwork
from .plan2_strategy_shadow import Plan2ResidualValueNetworkLeafModel


PLAN2_STRATEGY_CHAMPION_SCHEMA: Final = "gkms.plan2-strategy-champion.v1"
DEFAULT_PLAN2_REVIEW_CHAMPION: Final = (
    PROJECT_ROOT / "var" / "strategy" / "plan2_review_residual_champion_v1.json"
)

# Live Final states can make one exact native transition substantially more
# expensive than the small synthetic/training fixtures.  Keep the full depth
# and beam (so root legality and drink/card candidates are unchanged), but
# cap long-horizon DFS work deterministically.  The last four turns retain the
# original full node budget: this is where Review conversion and extra-play
# drinks need the exact tactical continuation most.
PLAN2_PRODUCTION_LONG_HORIZON_MAX_NODES: Final = 256
PLAN2_PRODUCTION_EXACT_TAIL_TURNS: Final = 4
PLAN2_LEADERBOARD_MAX_ROOT_CARD_BONUS: Final = 25.0
DEFAULT_PLAN2_LEADERBOARD_PRIOR: Final = (
    PROJECT_ROOT
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_native_verified_v1"
    / "episodes.jsonl"
)
_STAGE_BY_STEP = {
    16: "ProduceStepType_AuditionMid1",
    17: "ProduceStepType_AuditionMid2",
    18: "ProduceStepType_AuditionFinal",
}


@dataclass(frozen=True, slots=True)
class Plan2LeaderboardCardRootBonus:
    """Bounded card preference over the planner's native PLAY subset.

    The strongest observed card receives zero adjustment; weaker known cards
    receive at most ``-maximum_penalty``.  Drinks, END_TURN and unknown cards
    stay at zero, so a card-only behavior prior cannot promote PLAY over a
    native-selected non-card action or punish a card absent from training.
    """

    prior: object
    produce_id: str
    exam_effect_type: str
    maximum_penalty: float = PLAN2_LEADERBOARD_MAX_ROOT_CARD_BONUS

    def __post_init__(self) -> None:
        if not isinstance(self.produce_id, str) or not self.produce_id:
            raise ValueError("produce_id must be non-empty text")
        if not isinstance(self.exam_effect_type, str) or not self.exam_effect_type:
            raise ValueError("exam_effect_type must be non-empty text")
        if not 0.0 <= float(self.maximum_penalty) <= 100.0:
            raise ValueError("maximum_penalty must be within 0..100")
        if not callable(getattr(self.prior, "rank_advisory_with_evidence", None)):
            raise TypeError("prior must expose rank_advisory_with_evidence")

    def __call__(
        self,
        state: Plan2NativeHorizonState,
        action: Plan2NativeOfflineAction,
        legal_actions: tuple[Plan2NativeOfflineAction, ...],
    ) -> float:
        score = self._score_for(state, action, legal_actions)
        if score is None:
            return 0.0
        maximum = max(
            (
                value
                for candidate in legal_actions
                for value in (self._score_for(state, candidate, legal_actions),)
                if value is not None
            ),
            default=0.0,
        )
        return -float(self.maximum_penalty) * (maximum - score) / 1000.0

    def applies_to_state(self, state: Plan2NativeHorizonState) -> bool:
        """Return whether this root can produce a stage-scoped prior score."""

        if not isinstance(state, Plan2NativeHorizonState):
            return False
        step_context = state.zones.binding.step_context_id
        try:
            step_value = int(step_context.rsplit(":", 1)[-1])
        except (AttributeError, TypeError, ValueError):
            return False
        return step_value in _STAGE_BY_STEP

    def _score_for(
        self,
        state: Plan2NativeHorizonState,
        action: Plan2NativeOfflineAction,
        legal_actions: tuple[Plan2NativeOfflineAction, ...],
    ) -> int | None:
        """Return the prior score without turning it into a native value."""

        if not isinstance(action, Plan2NativeAction) or action.kind != "play":
            return None
        by_guid = {card.guid: card.card_id for card in state.zones.hand}
        legal_ids = tuple(
            dict.fromkeys(
                by_guid[value.card_guid]
                for value in legal_actions
                if isinstance(value, Plan2NativeAction)
                and value.kind == "play"
                and value.card_guid in by_guid
            )
        )
        card_id = by_guid.get(action.card_guid)
        if card_id is None or not legal_ids:
            return None
        step_context = state.zones.binding.step_context_id
        try:
            step_value = int(step_context.rsplit(":", 1)[-1])
        except (AttributeError, TypeError, ValueError):
            return None
        stage = _STAGE_BY_STEP.get(step_value)
        if stage is None:
            return None
        features = {
            "flow": (
                f"{self.produce_id}|ProducePlanType_Plan2|"
                f"{self.exam_effect_type}"
            ),
            "stage": stage,
            "turn": state.scalar.current_turn,
            "action_history": [],
        }
        evidence = self.prior.rank_advisory_with_evidence(features, legal_ids)
        if evidence.abstained or not evidence.ranked:
            return None
        if card_id in evidence.unknown_candidates:
            return None
        maximum = max(evidence.scores.values(), default=0)
        score = int(evidence.scores.get(card_id, 0))
        if maximum <= 0:
            return None
        return score

    def tie_break_score(
        self,
        state: Plan2NativeHorizonState,
        action: Plan2NativeOfflineAction,
        legal_actions: tuple[Plan2NativeOfflineAction, ...],
    ) -> float:
        """Return a bounded rank for the safe equal-native-value seam."""

        value = self._score_for(state, action, legal_actions)
        return 0.0 if value is None else float(value)


@dataclass(frozen=True, slots=True)
class Plan2ActionTypeLockedRootBonus:
    """Keep a card prior inside the native planner's selected action type."""

    card_bonus: Plan2LeaderboardCardRootBonus
    locked_kind: str = "play"

    def __call__(
        self,
        state: Plan2NativeHorizonState,
        action: Plan2NativeOfflineAction,
        legal_actions: tuple[Plan2NativeOfflineAction, ...],
    ) -> float:
        if not isinstance(action, Plan2NativeAction) or action.kind != self.locked_kind:
            return -100.0
        return self.card_bonus(state, action, legal_actions)


@dataclass(frozen=True, slots=True)
class Plan2ActionTypeLockedRootTieBreaker:
    """Apply a leaderboard rank only inside the native root action kind.

    This is the production-safe counterpart to the legacy numeric root bonus:
    it cannot change a native action's value, terminal score or legality.
    """

    card_bonus: Plan2LeaderboardCardRootBonus
    locked_kind: str = "play"

    def __post_init__(self) -> None:
        if not isinstance(self.card_bonus, Plan2LeaderboardCardRootBonus):
            raise TypeError("card_bonus must be Plan2LeaderboardCardRootBonus")
        if not isinstance(self.locked_kind, str) or not self.locked_kind:
            raise ValueError("locked_kind must be non-empty text")

    def __call__(
        self,
        state: Plan2NativeHorizonState,
        action: Plan2NativeOfflineAction,
        legal_actions: tuple[Plan2NativeOfflineAction, ...],
    ) -> float:
        if not isinstance(action, Plan2NativeAction) or action.kind != self.locked_kind:
            return 0.0
        return self.card_bonus.tie_break_score(state, action, legal_actions)


@dataclass(frozen=True, slots=True)
class Plan2PromotedStrategy:
    effect_type: str
    produce_ids: tuple[str, ...]
    idol_card_ids: tuple[str, ...]
    limit_turns: tuple[int, ...]
    correction_weight: float
    model: Plan2ValueNetwork
    manifest_path: Path
    produce_limit_turns: tuple[tuple[str, tuple[int, ...]], ...] = ()

    def active_for(
        self, *, idol_card_id: str, produce_id: str, limit_turn: int
    ) -> bool:
        if self.produce_limit_turns:
            scoped_turns = dict(self.produce_limit_turns).get(produce_id, ())
            return idol_card_id in self.idol_card_ids and limit_turn in scoped_turns
        return (
            idol_card_id in self.idol_card_ids
            and produce_id in self.produce_ids
            and limit_turn in self.limit_turns
        )


def _text_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{field} must be a non-empty text list")
    return tuple(value)


def _produce_limit_turns(
    value: object,
    *,
    produce_ids: tuple[str, ...],
    fallback_turns: tuple[int, ...],
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Decode an optional per-mode horizon allowlist.

    The original manifest exposed separate produce and turn lists, which
    implicitly formed a Cartesian product.  That is too broad once one model
    is validated for Initial 6/9/12 turns but N.I.A. only for 9/12 turns.
    Older manifests retain their original behavior through the fallback.
    """

    if value is None:
        return tuple((produce_id, fallback_turns) for produce_id in produce_ids)
    if not isinstance(value, Mapping) or set(value) != set(produce_ids):
        raise ValueError(
            "validated_produce_limit_turns must exactly cover produce_ids"
        )
    result: list[tuple[str, tuple[int, ...]]] = []
    for produce_id in produce_ids:
        raw_turns = value[produce_id]
        if (
            not isinstance(raw_turns, list)
            or not raw_turns
            or any(
                isinstance(turn, bool) or not isinstance(turn, int) or turn < 1
                for turn in raw_turns
            )
            or len(set(raw_turns)) != len(raw_turns)
        ):
            raise ValueError(
                "validated_produce_limit_turns values must be unique positive integers"
            )
        result.append((produce_id, tuple(raw_turns)))
    return tuple(result)


@lru_cache(maxsize=4)
def _load_plan2_promoted_strategy_cached(
    manifest_path: str,
) -> Plan2PromotedStrategy:
    path = Path(manifest_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Plan2 strategy champion must be an object")
    if payload.get("schema") != PLAN2_STRATEGY_CHAMPION_SCHEMA:
        raise ValueError("unsupported Plan2 strategy champion schema")
    if payload.get("target_mode") != "native-residual":
        raise ValueError("Plan2 champion must contain a native residual model")
    model_name = payload.get("model_path")
    correction_weight = payload.get("correction_weight")
    raw_turns = payload.get("validated_limit_turns")
    if not isinstance(model_name, str) or not model_name:
        raise ValueError("Plan2 champion model_path is missing")
    if (
        isinstance(correction_weight, bool)
        or not isinstance(correction_weight, (int, float))
        or not 0.0 < float(correction_weight) <= 1.0
    ):
        raise ValueError("Plan2 champion correction_weight is invalid")
    if not isinstance(raw_turns, list) or not raw_turns or any(
        type(value) is not int or value < 1 for value in raw_turns
    ):
        raise ValueError("validated_limit_turns must be positive integers")
    model_path = Path(model_name)
    if not model_path.is_absolute():
        model_path = path.parent / model_path
    produce_ids = _text_tuple(payload.get("produce_ids"), "produce_ids")
    limit_turns = tuple(raw_turns)
    return Plan2PromotedStrategy(
        effect_type=str(payload.get("effect_type", "")),
        produce_ids=produce_ids,
        idol_card_ids=_text_tuple(
            payload.get("validated_idol_card_ids"), "validated_idol_card_ids"
        ),
        limit_turns=limit_turns,
        correction_weight=float(correction_weight),
        model=Plan2ValueNetwork.load(model_path),
        manifest_path=path,
        produce_limit_turns=_produce_limit_turns(
            payload.get("validated_produce_limit_turns"),
            produce_ids=produce_ids,
            fallback_turns=limit_turns,
        ),
    )


def load_plan2_promoted_strategy(
    manifest_path: str | Path = DEFAULT_PLAN2_REVIEW_CHAMPION,
) -> Plan2PromotedStrategy:
    return _load_plan2_promoted_strategy_cached(str(Path(manifest_path).resolve()))


@lru_cache(maxsize=4)
def _load_leaderboard_prior_cached(path: str):
    from .leaderboard_card_imitation_prior import (
        load_leaderboard_card_imitation_prior,
    )

    return load_leaderboard_card_imitation_prior(path)


@dataclass(frozen=True, slots=True)
class Plan2PromotedStrategyPlanner:
    idol_card_id: str
    produce_id: str
    strategy: Plan2PromotedStrategy | None
    database: Path = DEFAULT_DATABASE
    root_action_bonus: Plan2NativeRootActionBonus | None = None

    def __call__(
        self,
        state: Plan2NativeHorizonState,
        catalog: Plan2NativeProgramCatalog,
        weights: Plan2NativeEvaluationWeights,
        limits: Plan2NativeExpectimaxLimits,
    ) -> Plan2NativeExpectimaxResult:
        leaf = None
        if self.strategy is not None and self.strategy.active_for(
            idol_card_id=self.idol_card_id,
            produce_id=self.produce_id,
            limit_turn=state.limit_turn,
        ):
            leaf = Plan2ResidualValueNetworkLeafModel(
                self.strategy.model,
                self.strategy.correction_weight,
                self.database,
            )
        baseline = plan_plan2_native_expectimax(
            state,
            catalog,
            weights=weights,
            limits=limits,
            leaf_value_model=leaf,
        )
        if (
            self.root_action_bonus is None
            or not isinstance(
                self.root_action_bonus,
                Plan2LeaderboardCardRootBonus,
            )
            or not self.root_action_bonus.applies_to_state(state)
            or baseline.best_action is None
        ):
            return baseline
        return plan_plan2_native_expectimax(
            state,
            catalog,
            weights=weights,
            limits=limits,
            leaf_value_model=leaf,
            root_action_tiebreaker=Plan2ActionTypeLockedRootTieBreaker(
                self.root_action_bonus,
                locked_kind=(
                    baseline.best_action.kind
                    if isinstance(baseline.best_action, Plan2NativeAction)
                    else "__non_card_action__"
                ),
            ),
        )

    def active_for_limit_turn(self, limit_turn: int) -> bool:
        return self.strategy is not None and self.strategy.active_for(
            idol_card_id=self.idol_card_id,
            produce_id=self.produce_id,
            limit_turn=limit_turn,
        )


@dataclass(frozen=True, slots=True)
class Plan2ProductionStrategyPlanner:
    """Deterministic live-search budget around the promoted native planner.

    Only the node budget changes.  Native action enumeration, transition
    legality, full search depth, beam ordering and the residual leaf model all
    remain owned by the existing planner.  A deterministic bound is important
    here: a wall-clock timeout could choose a different action after a restart
    solely because the machine was busier during retained-history recovery.
    """

    planner: Plan2PromotedStrategyPlanner
    long_horizon_max_nodes: int = PLAN2_PRODUCTION_LONG_HORIZON_MAX_NODES
    exact_tail_turns: int = PLAN2_PRODUCTION_EXACT_TAIL_TURNS

    def __post_init__(self) -> None:
        if not isinstance(self.planner, Plan2PromotedStrategyPlanner):
            raise TypeError("planner must be a promoted Plan2 strategy planner")
        if (
            type(self.long_horizon_max_nodes) is not int
            or self.long_horizon_max_nodes < 1
        ):
            raise ValueError("long_horizon_max_nodes must be a positive integer")
        if type(self.exact_tail_turns) is not int or self.exact_tail_turns < 0:
            raise ValueError("exact_tail_turns must be a nonnegative integer")

    def __call__(
        self,
        state: Plan2NativeHorizonState,
        catalog: Plan2NativeProgramCatalog,
        weights: Plan2NativeEvaluationWeights,
        limits: Plan2NativeExpectimaxLimits,
    ) -> Plan2NativeExpectimaxResult:
        effective_limits = limits
        if (
            state.remaining_turns > self.exact_tail_turns
            and limits.max_nodes > self.long_horizon_max_nodes
        ):
            effective_limits = replace(
                limits,
                max_nodes=self.long_horizon_max_nodes,
            )
        return self.planner(state, catalog, weights, effective_limits)

    def active_for_limit_turn(self, limit_turn: int) -> bool:
        return self.planner.active_for_limit_turn(limit_turn)


def build_plan2_promoted_strategy_planner(
    idol_card_id: str,
    produce_id: str,
    *,
    manifest_path: str | Path = DEFAULT_PLAN2_REVIEW_CHAMPION,
    database: str | Path = DEFAULT_DATABASE,
    leaderboard_prior_path: str | Path = DEFAULT_PLAN2_LEADERBOARD_PRIOR,
    leaderboard_max_penalty: float = PLAN2_LEADERBOARD_MAX_ROOT_CARD_BONUS,
    enable_leaderboard_advisory: bool = True,
) -> Plan2PromotedStrategyPlanner:
    if type(enable_leaderboard_advisory) is not bool:
        raise TypeError("enable_leaderboard_advisory must be bool")
    if not 0.0 <= float(leaderboard_max_penalty) <= 100.0:
        raise ValueError("leaderboard_max_penalty must be within 0..100")
    profile = get_idol_profile(idol_card_id, Path(database))
    strategy = None
    root_action_bonus = None
    if profile is not None:
        try:
            candidate = load_plan2_promoted_strategy(manifest_path)
        except (OSError, ValueError):
            candidate = None
        if candidate is not None and profile.exam_effect_type == candidate.effect_type:
            strategy = candidate
        if enable_leaderboard_advisory:
            try:
                prior = _load_leaderboard_prior_cached(
                    str(Path(leaderboard_prior_path).resolve())
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                prior = None
            if (
                prior is not None
                and prior.applies_to(
                    produce_id=produce_id,
                    plan_type="ProducePlanType_Plan2",
                    exam_effect_type=profile.exam_effect_type,
                )
            ):
                root_action_bonus = Plan2LeaderboardCardRootBonus(
                    prior,
                    produce_id,
                    profile.exam_effect_type,
                    float(leaderboard_max_penalty),
                )
    return Plan2PromotedStrategyPlanner(
        idol_card_id,
        produce_id,
        strategy,
        Path(database),
        root_action_bonus,
    )


def build_plan2_production_strategy_planner(
    idol_card_id: str,
    produce_id: str,
    *,
    manifest_path: str | Path = DEFAULT_PLAN2_REVIEW_CHAMPION,
    database: str | Path = DEFAULT_DATABASE,
    leaderboard_prior_path: str | Path = DEFAULT_PLAN2_LEADERBOARD_PRIOR,
    leaderboard_max_penalty: float = PLAN2_LEADERBOARD_MAX_ROOT_CARD_BONUS,
    enable_leaderboard_advisory: bool = True,
) -> Plan2ProductionStrategyPlanner:
    """Build the promoted planner with the deterministic live node budget."""

    return Plan2ProductionStrategyPlanner(
        build_plan2_promoted_strategy_planner(
            idol_card_id,
            produce_id,
            manifest_path=manifest_path,
            database=database,
            leaderboard_prior_path=leaderboard_prior_path,
            leaderboard_max_penalty=leaderboard_max_penalty,
            enable_leaderboard_advisory=enable_leaderboard_advisory,
        )
    )


__all__ = [
    "DEFAULT_PLAN2_LEADERBOARD_PRIOR",
    "DEFAULT_PLAN2_REVIEW_CHAMPION",
    "PLAN2_LEADERBOARD_MAX_ROOT_CARD_BONUS",
    "PLAN2_PRODUCTION_EXACT_TAIL_TURNS",
    "PLAN2_PRODUCTION_LONG_HORIZON_MAX_NODES",
    "PLAN2_STRATEGY_CHAMPION_SCHEMA",
    "Plan2PromotedStrategy",
    "Plan2ActionTypeLockedRootBonus",
    "Plan2ActionTypeLockedRootTieBreaker",
    "Plan2LeaderboardCardRootBonus",
    "Plan2PromotedStrategyPlanner",
    "Plan2ProductionStrategyPlanner",
    "build_plan2_production_strategy_planner",
    "build_plan2_promoted_strategy_planner",
    "load_plan2_promoted_strategy",
]
