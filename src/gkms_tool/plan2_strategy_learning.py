"""Offline Plan2 self-play data and a small NumPy value network.

The network never decides whether a click is legal.  Native simulation owns
transitions and terminal scores; this module learns only a bounded leaf value
from those exact trajectories.  A model is therefore safe to A/B behind
expectimax later, after held-out validation, without weakening MAA or Save
acceptance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import lru_cache
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

from .master_db import DEFAULT_DATABASE, get_idol_profile
from .plan2_native_expectimax import (
    Plan2NativeEvaluationWeights,
    Plan2NativeExpectimaxLimits,
)
from .plan2_native_horizon import (
    Plan2NativeHorizonState,
    load_master_plan2_initial_deck,
)
from .plan2_native_program_catalog import compile_plan2_native_program_catalog
from .plan2_native_self_play_acceptance import (
    Plan2NativeSelfPlayDependencies,
    Plan2NativeSelfPlayResult,
    run_plan2_native_self_play_acceptance,
)
from .reward_state import plan2_reward_card_features
from .logic_engine import load_master_card


PLAN2_STRATEGY_DATA_SCHEMA = "gkms.plan2-strategy-example.v1"
PLAN2_VALUE_MODEL_SCHEMA = "gkms.plan2-value-network.v1"

FEATURE_NAMES = (
    "current_turn",
    "remaining_turns",
    "score",
    "review",
    "review_raw_score",
    "review_count_add",
    "block",
    "aggressive",
    "stamina",
    "max_stamina",
    "stamina_ratio",
    "plays_remaining",
    "extra_turn",
    "exam_play_count",
    "turn_play_count",
    "hand_count",
    "deck_count",
    "grave_count",
    "lost_count",
    "review_multiplier_layers",
    "end_turn_listeners",
    "card_play_listeners",
    "status_enchant_listeners",
    "timer_count",
    "hand_review_gain",
    "hand_review_conversion",
    "hand_extra_play",
    "deck_review_gain",
    "deck_review_conversion",
    "deck_extra_play",
    "grave_review_gain",
    "grave_review_conversion",
    "item_source_count",
    "item_listener_count",
)


@lru_cache(maxsize=4_096)
def _card_semantics(
    card_id: str,
    effective_upgrade: int,
    database: str,
) -> tuple[float, float, float]:
    """Cache immutable Master card semantics used at every search leaf."""

    master = load_master_card(card_id, effective_upgrade, Path(database))
    features = plan2_reward_card_features(master)
    return (
        float(features.review_gain),
        float(features.review_conversion_permil),
        float(features.playable_count + features.extra_turns),
    )


def _zone_semantics(cards: Sequence[object], database: Path) -> tuple[float, float, float]:
    review_gain = conversion = extra_play = 0
    database_key = str(Path(database).resolve())
    for card in cards:
        card_review, card_conversion, card_extra_play = _card_semantics(
            str(getattr(card, "card_id")),
            int(getattr(card, "effective_upgrade")),
            database_key,
        )
        review_gain += card_review
        conversion += card_conversion
        extra_play += card_extra_play
    return float(review_gain), float(conversion), float(extra_play)


def extract_plan2_value_features(
    state: Plan2NativeHorizonState,
    *,
    database: Path = DEFAULT_DATABASE,
) -> tuple[float, ...]:
    """Project one settled native state into stable semantic features."""

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    scalar = state.scalar
    hand = _zone_semantics(state.zones.hand, database)
    deck = _zone_semantics(state.zones.deck, database)
    grave = _zone_semantics(state.zones.grave, database)
    max_stamina = max(1, scalar.max_stamina)
    values = (
        scalar.current_turn,
        state.remaining_turns,
        scalar.score,
        scalar.review,
        scalar.get_review_raw_score(),
        scalar.review_count_add,
        scalar.block,
        scalar.card_play_aggressive,
        scalar.stamina,
        scalar.max_stamina,
        scalar.stamina / max_stamina,
        state.plays_remaining,
        state.extra_turn,
        scalar.exam_card_play_count,
        scalar.turn_card_play_count,
        len(state.zones.hand),
        len(state.zones.deck),
        len(state.zones.grave),
        len(state.zones.lost),
        len(scalar.review_multiple_layers),
        len(scalar.end_turn_listeners),
        len(scalar.card_play_listeners),
        len(state.status_enchant.listeners),
        len(state.timers),
        *hand,
        *deck,
        grave[0],
        grave[1],
        len(state.item_runtime.sources),
        len(state.item_runtime.listeners),
    )
    if len(values) != len(FEATURE_NAMES):
        raise AssertionError("Plan2 value feature schema drift")
    return tuple(float(value) for value in values)


@dataclass(frozen=True, slots=True)
class Plan2StrategyExample:
    trajectory_id: str
    idol_card_id: str
    random_state: int
    limit_turn: int
    step_index: int
    action_id: str
    terminal_score: int
    features: tuple[float, ...]
    item_runtime_mode: str = "exact"
    schema: str = PLAN2_STRATEGY_DATA_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PLAN2_STRATEGY_DATA_SCHEMA:
            raise ValueError("unsupported Plan2 strategy example schema")
        if len(self.features) != len(FEATURE_NAMES):
            raise ValueError("Plan2 strategy example feature count mismatch")
        if self.item_runtime_mode not in {"exact", "ablated"}:
            raise ValueError("unsupported item_runtime_mode")

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "feature_names": list(FEATURE_NAMES),
            "features": list(self.features),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "Plan2StrategyExample":
        if value.get("feature_names") != list(FEATURE_NAMES):
            raise ValueError("Plan2 strategy feature schema mismatch")
        raw = value.get("features")
        if not isinstance(raw, list):
            raise ValueError("Plan2 strategy features must be a list")
        return cls(
            trajectory_id=str(value.get("trajectory_id", "")),
            idol_card_id=str(value.get("idol_card_id", "")),
            random_state=int(value.get("random_state", 0)),
            limit_turn=int(value.get("limit_turn", 0)),
            step_index=int(value.get("step_index", 0)),
            action_id=str(value.get("action_id", "")),
            terminal_score=int(value.get("terminal_score", 0)),
            features=tuple(float(item) for item in raw),
            item_runtime_mode=str(value.get("item_runtime_mode", "")),
            schema=str(value.get("schema", "")),
        )


def examples_from_self_play(
    result: Plan2NativeSelfPlayResult,
    *,
    database: Path = DEFAULT_DATABASE,
    item_runtime_mode: str = "exact",
) -> tuple[Plan2StrategyExample, ...]:
    if not isinstance(result, Plan2NativeSelfPlayResult):
        raise TypeError("result must be Plan2NativeSelfPlayResult")
    if not result.acceptance_passed or result.final_state is None:
        raise ValueError("only completed blocker-free self-play can label examples")
    if item_runtime_mode not in {"exact", "ablated"}:
        raise ValueError("unsupported item_runtime_mode")
    final_score = result.final_state.scalar.score
    trajectory_id = (
        f"{result.idol_card_id}:{result.random_state:08x}:"
        f"turns={result.limit_turn}"
    )
    return tuple(
        Plan2StrategyExample(
            trajectory_id=trajectory_id,
            idol_card_id=result.idol_card_id,
            random_state=result.random_state,
            limit_turn=result.limit_turn,
            step_index=step.step_index,
            action_id=step.action.action_id,
            terminal_score=final_score,
            features=extract_plan2_value_features(
                step.predicted_before,
                database=database,
            ),
            item_runtime_mode=item_runtime_mode,
        )
        for step in result.steps
    )


@dataclass(frozen=True, slots=True)
class Plan2StrategyDatasetSummary:
    requested_trajectories: int
    completed_trajectories: int
    failed_trajectories: int
    examples: int
    item_runtime_mode: str
    failure_reasons: tuple[str, ...]


def generate_plan2_strategy_dataset(
    idol_card_ids: Sequence[str],
    *,
    random_states: Sequence[int],
    limit_turns: Sequence[int] = (6, 9, 12),
    produce_id: str = "produce-003",
    output_path: Path | None = None,
    limits: Plan2NativeExpectimaxLimits = Plan2NativeExpectimaxLimits(
        max_depth=4,
        beam_width=6,
        max_nodes=2_500,
    ),
    database: Path = DEFAULT_DATABASE,
    item_runtime_mode: str = "exact",
) -> tuple[tuple[Plan2StrategyExample, ...], Plan2StrategyDatasetSummary]:
    """Generate exact-transition labels with one shared compiled catalog."""

    idols = tuple(dict.fromkeys(idol_card_ids))
    seeds = tuple(random_states)
    turns = tuple(limit_turns)
    if not idols or not seeds or not turns:
        raise ValueError("idol_card_ids, random_states and limit_turns cannot be empty")
    if item_runtime_mode not in {"exact", "ablated"}:
        raise ValueError("item_runtime_mode must be exact or ablated")
    compilation = compile_plan2_native_program_catalog(database=database)

    def manifest_loader(idol_card_id, requested_produce_id, upgrade, master_dir):
        manifest = load_master_plan2_initial_deck(
            idol_card_id,
            produce_id=requested_produce_id,
            idol_card_upgrade=upgrade,
            master_dir=master_dir,
        )
        return (
            manifest
            if item_runtime_mode == "exact"
            else replace(manifest, item_ids=())
        )

    dependencies = Plan2NativeSelfPlayDependencies(
        manifest_loader=manifest_loader,
        catalog_compiler=lambda _path: compilation,
    )
    examples: list[Plan2StrategyExample] = []
    failures: list[str] = []
    completed = 0
    for idol_card_id in idols:
        profile = get_idol_profile(idol_card_id, database)
        if profile is None or profile.plan_type != "ProducePlanType_Plan2":
            failures.append(f"{idol_card_id}:not-plan2")
            continue
        for limit_turn in turns:
            for random_state in seeds:
                result = run_plan2_native_self_play_acceptance(
                    idol_card_id,
                    random_state=int(random_state),
                    limit_turn=int(limit_turn),
                    stamina=profile.stamina,
                    max_stamina=profile.stamina,
                    produce_id=produce_id,
                    max_actions=max(30, int(limit_turn) * 4),
                    limits=limits,
                    database=database,
                    dependencies=dependencies,
                )
                if result.acceptance_passed:
                    completed += 1
                    examples.extend(
                        examples_from_self_play(
                            result,
                            database=database,
                            item_runtime_mode=item_runtime_mode,
                        )
                    )
                else:
                    failures.append(
                        f"{idol_card_id}:{int(random_state):08x}:"
                        f"{limit_turn}:{result.stop_reason}"
                    )
    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                json.dumps(item.to_dict(), ensure_ascii=False, allow_nan=False)
                + "\n"
                for item in examples
            ),
            encoding="utf-8",
        )
    requested = len(idols) * len(seeds) * len(turns)
    summary = Plan2StrategyDatasetSummary(
        requested,
        completed,
        requested - completed,
        len(examples),
        item_runtime_mode,
        tuple(failures),
    )
    return tuple(examples), summary


def read_plan2_strategy_dataset(path: Path) -> tuple[Plan2StrategyExample, ...]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError("Plan2 strategy JSONL row must be an object")
            rows.append(Plan2StrategyExample.from_dict(value))
    return tuple(rows)


@dataclass(slots=True)
class Plan2ValueNetwork:
    """Two-hidden-layer MLP trained only from offline native trajectories."""

    x_mean: np.ndarray
    x_scale: np.ndarray
    y_mean: float
    y_scale: float
    w1: np.ndarray
    b1: np.ndarray
    w2: np.ndarray
    b2: np.ndarray
    w3: np.ndarray
    b3: np.ndarray

    def predict(self, features: Sequence[Sequence[float]] | np.ndarray) -> np.ndarray:
        x = np.asarray(features, dtype=np.float64)
        if x.ndim == 1:
            x = x.reshape(1, -1)
        if x.shape[1] != len(FEATURE_NAMES):
            raise ValueError("Plan2 value input feature count mismatch")
        z = (x - self.x_mean) / self.x_scale
        h1 = np.maximum(0.0, z @ self.w1 + self.b1)
        h2 = np.maximum(0.0, h1 @ self.w2 + self.b2)
        normalized = h2 @ self.w3 + self.b3
        return normalized[:, 0] * self.y_scale + self.y_mean

    def save(self, path: Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            destination,
            schema=np.asarray([PLAN2_VALUE_MODEL_SCHEMA]),
            feature_names=np.asarray(FEATURE_NAMES),
            x_mean=self.x_mean,
            x_scale=self.x_scale,
            y_mean=np.asarray([self.y_mean]),
            y_scale=np.asarray([self.y_scale]),
            w1=self.w1,
            b1=self.b1,
            w2=self.w2,
            b2=self.b2,
            w3=self.w3,
            b3=self.b3,
        )

    @classmethod
    def load(cls, path: Path) -> "Plan2ValueNetwork":
        with np.load(Path(path), allow_pickle=False) as payload:
            if str(payload["schema"][0]) != PLAN2_VALUE_MODEL_SCHEMA:
                raise ValueError("unsupported Plan2 value model schema")
            if tuple(str(value) for value in payload["feature_names"]) != FEATURE_NAMES:
                raise ValueError("Plan2 value model feature schema mismatch")
            return cls(
                payload["x_mean"],
                payload["x_scale"],
                float(payload["y_mean"][0]),
                float(payload["y_scale"][0]),
                payload["w1"],
                payload["b1"],
                payload["w2"],
                payload["b2"],
                payload["w3"],
                payload["b3"],
            )


@dataclass(frozen=True, slots=True)
class Plan2ValueTrainingMetrics:
    examples: int
    epochs: int
    initial_rmse: float
    final_rmse: float


@dataclass(frozen=True, slots=True)
class Plan2PairwiseValueTrainingMetrics:
    """Training diagnostics for counterfactual action ordering.

    ``pairs`` contains only alternatives branched from the same native state.
    Ordinary on-policy rows still contribute to score calibration through the
    regression term, but are never compared as if they were interchangeable
    actions.
    """

    examples: int
    pairs: int
    epochs: int
    initial_rmse: float
    final_rmse: float
    initial_pair_accuracy: float
    final_pair_accuracy: float


def fit_plan2_value_network(
    examples: Sequence[Plan2StrategyExample],
    *,
    hidden_sizes: tuple[int, int] = (48, 24),
    epochs: int = 500,
    learning_rate: float = 0.003,
    random_state: int = 0,
) -> tuple[Plan2ValueNetwork, Plan2ValueTrainingMetrics]:
    """Fit a deterministic full-batch Adam MLP to terminal-score labels."""

    rows = tuple(examples)
    if len(rows) < 4:
        raise ValueError("at least four Plan2 strategy examples are required")
    if epochs < 1 or learning_rate <= 0:
        raise ValueError("epochs and learning_rate must be positive")
    h1_size, h2_size = hidden_sizes
    if h1_size < 1 or h2_size < 1:
        raise ValueError("hidden sizes must be positive")
    x = np.asarray([row.features for row in rows], dtype=np.float64)
    y = np.asarray([row.terminal_score for row in rows], dtype=np.float64).reshape(-1, 1)
    x_mean = x.mean(axis=0)
    x_scale = x.std(axis=0)
    x_scale[x_scale < 1e-9] = 1.0
    y_mean = float(y.mean())
    y_scale = max(float(y.std()), 1.0)
    xn = (x - x_mean) / x_scale
    yn = (y - y_mean) / y_scale
    rng = np.random.default_rng(random_state)
    w1 = rng.normal(0.0, np.sqrt(2.0 / xn.shape[1]), (xn.shape[1], h1_size))
    b1 = np.zeros((h1_size,), dtype=np.float64)
    w2 = rng.normal(0.0, np.sqrt(2.0 / h1_size), (h1_size, h2_size))
    b2 = np.zeros((h2_size,), dtype=np.float64)
    w3 = rng.normal(0.0, np.sqrt(1.0 / h2_size), (h2_size, 1))
    b3 = np.zeros((1,), dtype=np.float64)
    parameters = [w1, b1, w2, b2, w3, b3]
    first = [np.zeros_like(value) for value in parameters]
    second = [np.zeros_like(value) for value in parameters]

    def forward() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        z1 = xn @ w1 + b1
        a1 = np.maximum(0.0, z1)
        z2 = a1 @ w2 + b2
        a2 = np.maximum(0.0, z2)
        return z1, a1, z2, a2, a2 @ w3 + b3

    initial_prediction = forward()[-1] * y_scale + y_mean
    initial_rmse = float(np.sqrt(np.mean((initial_prediction - y) ** 2)))
    beta1, beta2, epsilon = 0.9, 0.999, 1e-8
    for step in range(1, epochs + 1):
        z1, a1, z2, a2, prediction = forward()
        dp = 2.0 * (prediction - yn) / len(rows)
        gradients = [
            None,
            None,
            None,
            None,
            a2.T @ dp,
            dp.sum(axis=0),
        ]
        da2 = dp @ w3.T
        dz2 = da2 * (z2 > 0.0)
        gradients[2] = a1.T @ dz2
        gradients[3] = dz2.sum(axis=0)
        da1 = dz2 @ w2.T
        dz1 = da1 * (z1 > 0.0)
        gradients[0] = xn.T @ dz1
        gradients[1] = dz1.sum(axis=0)
        for index, (parameter, gradient) in enumerate(zip(parameters, gradients, strict=True)):
            first[index] = beta1 * first[index] + (1.0 - beta1) * gradient
            second[index] = beta2 * second[index] + (1.0 - beta2) * (gradient * gradient)
            m_hat = first[index] / (1.0 - beta1**step)
            v_hat = second[index] / (1.0 - beta2**step)
            parameter -= learning_rate * m_hat / (np.sqrt(v_hat) + epsilon)

    model = Plan2ValueNetwork(
        x_mean,
        x_scale,
        y_mean,
        y_scale,
        w1,
        b1,
        w2,
        b2,
        w3,
        b3,
    )
    final_rmse = float(
        np.sqrt(np.mean((model.predict(x).reshape(-1, 1) - y) ** 2))
    )
    return model, Plan2ValueTrainingMetrics(
        len(rows), epochs, initial_rmse, final_rmse
    )


def _counterfactual_pairs(
    rows: Sequence[Plan2StrategyExample],
) -> tuple[tuple[int, int], ...]:
    groups: dict[tuple[str, int, int, int], list[int]] = {}
    for index, row in enumerate(rows):
        if not row.trajectory_id.startswith("counterfactual:"):
            continue
        key = (
            row.idol_card_id,
            row.random_state,
            row.limit_turn,
            row.step_index,
        )
        groups.setdefault(key, []).append(index)
    pairs: list[tuple[int, int]] = []
    for indices in groups.values():
        for left_offset, left in enumerate(indices):
            for right in indices[left_offset + 1 :]:
                if rows[left].terminal_score != rows[right].terminal_score:
                    pairs.append((left, right))
    return tuple(pairs)


def fit_plan2_pairwise_value_network(
    examples: Sequence[Plan2StrategyExample],
    *,
    hidden_sizes: tuple[int, int] = (64, 32),
    epochs: int = 700,
    learning_rate: float = 0.002,
    random_state: int = 0,
    pairwise_weight: float = 1.0,
    target_mode: str = "terminal",
    evaluation_weights: Plan2NativeEvaluationWeights = Plan2NativeEvaluationWeights(),
) -> tuple[Plan2ValueNetwork, Plan2PairwiseValueTrainingMetrics]:
    """Fit terminal value while directly ordering same-state alternatives.

    Counterfactual rows are grouped by idol, seed, horizon and sampled root.
    A logistic ranking loss is applied only within each such group.  This
    avoids the invalid comparison of unrelated turns while teaching the model
    the decision boundary that matters to card selection.
    """

    rows = tuple(examples)
    if len(rows) < 4:
        raise ValueError("at least four Plan2 strategy examples are required")
    if epochs < 1 or learning_rate <= 0 or pairwise_weight <= 0:
        raise ValueError("training controls must be positive")
    if target_mode not in {"terminal", "native-residual"}:
        raise ValueError("target_mode must be terminal or native-residual")
    if not isinstance(evaluation_weights, Plan2NativeEvaluationWeights):
        raise TypeError("evaluation_weights must be Plan2NativeEvaluationWeights")
    h1_size, h2_size = hidden_sizes
    if h1_size < 1 or h2_size < 1:
        raise ValueError("hidden sizes must be positive")
    pairs = _counterfactual_pairs(rows)
    if not pairs:
        raise ValueError("counterfactual alternatives with unequal scores are required")

    x = np.asarray([row.features for row in rows], dtype=np.float64)
    terminal_y = np.asarray(
        [row.terminal_score for row in rows], dtype=np.float64
    ).reshape(-1, 1)
    baseline = np.zeros_like(terminal_y)
    if target_mode == "native-residual":
        for index, row in enumerate(rows):
            features = row.features
            remaining = max(0.0, features[1])
            review_opportunities = min(
                remaining, float(evaluation_weights.review_turn_cap)
            )
            defensive_opportunities = min(
                remaining, float(evaluation_weights.defensive_turn_cap)
            )
            baseline[index, 0] = (
                features[2] * evaluation_weights.score
                + features[4]
                * evaluation_weights.review_per_turn
                * review_opportunities
                + features[7]
                * evaluation_weights.aggressive_per_turn
                * review_opportunities
                + features[6]
                * evaluation_weights.block_per_turn
                * defensive_opportunities
                + features[8]
                * evaluation_weights.stamina_per_turn
                * defensive_opportunities
                + remaining * evaluation_weights.remaining_turn
            )
    y = terminal_y - baseline
    x_mean = x.mean(axis=0)
    x_scale = x.std(axis=0)
    x_scale[x_scale < 1e-9] = 1.0
    y_mean = float(y.mean())
    y_scale = max(float(y.std()), 1.0)
    xn = (x - x_mean) / x_scale
    yn = (y - y_mean) / y_scale

    left = np.asarray([value[0] for value in pairs], dtype=np.int64)
    right = np.asarray([value[1] for value in pairs], dtype=np.int64)
    terminal_scale = max(float(terminal_y.std()), 1.0)
    signs = np.sign(terminal_y[left, 0] - terminal_y[right, 0])
    gaps = np.abs(terminal_y[left, 0] - terminal_y[right, 0]) / terminal_scale
    pair_weights = np.clip(gaps, 0.25, 4.0)
    pair_weight_sum = float(pair_weights.sum())

    rng = np.random.default_rng(random_state)
    w1 = rng.normal(0.0, np.sqrt(2.0 / xn.shape[1]), (xn.shape[1], h1_size))
    b1 = np.zeros((h1_size,), dtype=np.float64)
    w2 = rng.normal(0.0, np.sqrt(2.0 / h1_size), (h1_size, h2_size))
    b2 = np.zeros((h2_size,), dtype=np.float64)
    w3 = rng.normal(0.0, np.sqrt(1.0 / h2_size), (h2_size, 1))
    b3 = np.zeros((1,), dtype=np.float64)
    parameters = [w1, b1, w2, b2, w3, b3]
    first = [np.zeros_like(value) for value in parameters]
    second = [np.zeros_like(value) for value in parameters]

    def forward() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        z1 = xn @ w1 + b1
        a1 = np.maximum(0.0, z1)
        z2 = a1 @ w2 + b2
        a2 = np.maximum(0.0, z2)
        return z1, a1, z2, a2, a2 @ w3 + b3

    def pair_accuracy(prediction: np.ndarray) -> float:
        predicted_total = prediction * y_scale + y_mean + baseline
        margins = signs * (
            predicted_total[left, 0] - predicted_total[right, 0]
        )
        return float(np.mean(margins > 0.0))

    initial_normalized = forward()[-1]
    initial_prediction = initial_normalized * y_scale + y_mean
    initial_rmse = float(np.sqrt(np.mean((initial_prediction - y) ** 2)))
    initial_pair_accuracy = pair_accuracy(initial_normalized)
    beta1, beta2, epsilon = 0.9, 0.999, 1e-8
    for step in range(1, epochs + 1):
        z1, a1, z2, a2, prediction = forward()
        dp = 2.0 * (prediction - yn) / len(rows)

        baseline_delta = (baseline[left, 0] - baseline[right, 0]) / y_scale
        margins = signs * (
            prediction[left, 0] - prediction[right, 0] + baseline_delta
        )
        # sigmoid(-margin), written with logaddexp for stable large margins.
        rank_gradient = -signs * np.exp(-np.logaddexp(0.0, margins))
        rank_gradient *= pairwise_weight * pair_weights / pair_weight_sum
        np.add.at(dp[:, 0], left, rank_gradient)
        np.add.at(dp[:, 0], right, -rank_gradient)

        gradients = [
            None,
            None,
            None,
            None,
            a2.T @ dp,
            dp.sum(axis=0),
        ]
        da2 = dp @ w3.T
        dz2 = da2 * (z2 > 0.0)
        gradients[2] = a1.T @ dz2
        gradients[3] = dz2.sum(axis=0)
        da1 = dz2 @ w2.T
        dz1 = da1 * (z1 > 0.0)
        gradients[0] = xn.T @ dz1
        gradients[1] = dz1.sum(axis=0)
        for index, (parameter, gradient) in enumerate(zip(parameters, gradients, strict=True)):
            first[index] = beta1 * first[index] + (1.0 - beta1) * gradient
            second[index] = beta2 * second[index] + (1.0 - beta2) * (gradient * gradient)
            m_hat = first[index] / (1.0 - beta1**step)
            v_hat = second[index] / (1.0 - beta2**step)
            parameter -= learning_rate * m_hat / (np.sqrt(v_hat) + epsilon)

    model = Plan2ValueNetwork(
        x_mean,
        x_scale,
        y_mean,
        y_scale,
        w1,
        b1,
        w2,
        b2,
        w3,
        b3,
    )
    final_prediction = model.predict(x).reshape(-1, 1)
    final_rmse = float(np.sqrt(np.mean((final_prediction - y) ** 2)))
    final_normalized = (final_prediction - y_mean) / y_scale
    return model, Plan2PairwiseValueTrainingMetrics(
        len(rows),
        len(pairs),
        epochs,
        initial_rmse,
        final_rmse,
        initial_pair_accuracy,
        pair_accuracy(final_normalized),
    )


__all__ = [
    "FEATURE_NAMES",
    "PLAN2_STRATEGY_DATA_SCHEMA",
    "PLAN2_VALUE_MODEL_SCHEMA",
    "Plan2StrategyDatasetSummary",
    "Plan2StrategyExample",
    "Plan2PairwiseValueTrainingMetrics",
    "Plan2ValueNetwork",
    "Plan2ValueTrainingMetrics",
    "examples_from_self_play",
    "extract_plan2_value_features",
    "fit_plan2_value_network",
    "fit_plan2_pairwise_value_network",
    "generate_plan2_strategy_dataset",
    "read_plan2_strategy_dataset",
]
