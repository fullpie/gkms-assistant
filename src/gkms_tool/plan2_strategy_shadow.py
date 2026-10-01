"""One-step neural shadow policy over exact Plan2 transitions.

The model can rank only actions already admitted by the native engine.  It is
therefore useful for A/B evaluation without becoming a second rules engine or
weakening MAA/Save authority.  Production clicking remains owned by the
native planner until the shadow policy wins held-out rollouts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np

from .master_db import DEFAULT_DATABASE
from .plan2_native_expectimax import (
    Plan2NativeEvaluation,
    Plan2NativeExpectimaxResult,
    Plan2NativeLeafValueModel,
    enumerate_plan2_native_actions,
)
from .plan2_native_horizon import (
    Plan2NativeAction,
    Plan2NativeBlocker,
    Plan2NativeDrinkAction,
    Plan2NativeHorizonState,
    Plan2NativeProgramCatalog,
    Plan2NativeTransition,
    simulate_plan2_native_action_lifecycle,
)
from .plan2_strategy_learning import extract_plan2_value_features


@runtime_checkable
class Plan2ValuePredictor(Protocol):
    def predict(self, features: np.ndarray) -> np.ndarray: ...


@dataclass(frozen=True, slots=True)
class Plan2ValueNetworkLeafModel:
    """Adapter that confines a trained network to non-terminal cutoffs."""

    model: Plan2ValuePredictor
    database: Path = DEFAULT_DATABASE

    def __post_init__(self) -> None:
        if not isinstance(self.model, Plan2ValuePredictor):
            raise TypeError("model must implement Plan2ValuePredictor")
        object.__setattr__(self, "database", Path(self.database))

    def value(
        self,
        state: Plan2NativeHorizonState,
        base_evaluation: Plan2NativeEvaluation,
    ) -> float:
        if not isinstance(base_evaluation, Plan2NativeEvaluation):
            raise TypeError("base_evaluation must be Plan2NativeEvaluation")
        features = np.asarray(
            [extract_plan2_value_features(state, database=self.database)],
            dtype=np.float64,
        )
        prediction = np.asarray(self.model.predict(features), dtype=np.float64).reshape(-1)
        if len(prediction) != 1 or not np.isfinite(prediction[0]):
            raise ValueError("value network returned an invalid leaf estimate")
        return float(prediction[0])


@dataclass(frozen=True, slots=True)
class Plan2ResidualValueNetworkLeafModel:
    """Blend a learned native-evaluation residual into the exact heuristic."""

    model: Plan2ValuePredictor
    correction_weight: float = 0.25
    database: Path = DEFAULT_DATABASE

    def __post_init__(self) -> None:
        if not isinstance(self.model, Plan2ValuePredictor):
            raise TypeError("model must implement Plan2ValuePredictor")
        if (
            isinstance(self.correction_weight, bool)
            or not isinstance(self.correction_weight, (int, float))
            or not math.isfinite(float(self.correction_weight))
            or not 0.0 <= float(self.correction_weight) <= 1.0
        ):
            raise ValueError("correction_weight must be between zero and one")
        object.__setattr__(self, "correction_weight", float(self.correction_weight))
        object.__setattr__(self, "database", Path(self.database))

    def value(
        self,
        state: Plan2NativeHorizonState,
        base_evaluation: Plan2NativeEvaluation,
    ) -> float:
        if not isinstance(base_evaluation, Plan2NativeEvaluation):
            raise TypeError("base_evaluation must be Plan2NativeEvaluation")
        features = np.asarray(
            [extract_plan2_value_features(state, database=self.database)],
            dtype=np.float64,
        )
        residual = np.asarray(self.model.predict(features), dtype=np.float64).reshape(-1)
        if len(residual) != 1 or not np.isfinite(residual[0]):
            raise ValueError("residual value network returned an invalid estimate")
        return base_evaluation.total + self.correction_weight * float(residual[0])


ShadowTransitioner = Callable[
    [
        Plan2NativeHorizonState,
        Plan2NativeAction | Plan2NativeDrinkAction,
        Plan2NativeProgramCatalog,
    ],
    Plan2NativeTransition,
]


@dataclass(frozen=True, slots=True)
class Plan2NeuralActionEstimate:
    action: Plan2NativeAction | Plan2NativeDrinkAction
    predicted_terminal_score: float
    successor_terminal: bool
    transition_trace: tuple[str, ...]

    @property
    def action_id(self) -> str:
        return self.action.action_id


@dataclass(frozen=True, slots=True)
class Plan2NeuralShadowDecision:
    estimates: tuple[Plan2NeuralActionEstimate, ...]
    native_action_id: str | None
    blockers: tuple[Plan2NativeBlocker, ...]

    @property
    def best_action(self) -> Plan2NativeAction | Plan2NativeDrinkAction | None:
        return None if not self.estimates else self.estimates[0].action

    @property
    def best_action_id(self) -> str | None:
        return None if self.best_action is None else self.best_action.action_id

    @property
    def agrees_with_native(self) -> bool | None:
        if self.native_action_id is None or self.best_action_id is None:
            return None
        return self.native_action_id == self.best_action_id


def rank_plan2_actions_by_value_network(
    root: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    model: Plan2ValuePredictor,
    *,
    native_result: Plan2NativeExpectimaxResult | None = None,
    database: Path = DEFAULT_DATABASE,
    transitioner: ShadowTransitioner = simulate_plan2_native_action_lifecycle,
) -> Plan2NeuralShadowDecision:
    """Rank exact legal successors with a learned terminal-value predictor."""

    if not isinstance(root, Plan2NativeHorizonState):
        raise TypeError("root must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if not isinstance(model, Plan2ValuePredictor):
        raise TypeError("model must implement Plan2ValuePredictor")
    if native_result is not None and not isinstance(
        native_result, Plan2NativeExpectimaxResult
    ):
        raise TypeError("native_result must be Plan2NativeExpectimaxResult or None")
    if not callable(transitioner):
        raise TypeError("transitioner must be callable")

    enumeration = enumerate_plan2_native_actions(root, catalog)
    blockers = list(enumeration.blockers)
    successors: list[
        tuple[Plan2NativeAction | Plan2NativeDrinkAction, Plan2NativeTransition]
    ] = []
    for action in enumeration.actions:
        transition = transitioner(root, action, catalog)
        if not transition.supported or transition.after is None:
            blockers.extend(transition.blockers)
            continue
        successors.append((action, transition))

    estimates: list[Plan2NeuralActionEstimate] = []
    pending = [
        (action, transition)
        for action, transition in successors
        if transition.after is not None and not transition.after.terminal
    ]
    predictions: dict[str, float] = {}
    if pending:
        features = np.asarray(
            [
                extract_plan2_value_features(
                    transition.after,
                    database=Path(database),
                )
                for _, transition in pending
            ],
            dtype=np.float64,
        )
        raw = np.asarray(model.predict(features), dtype=np.float64).reshape(-1)
        if len(raw) != len(pending) or not np.all(np.isfinite(raw)):
            raise ValueError("neural value model returned invalid predictions")
        predictions = {
            action.action_id: float(value)
            for (action, _), value in zip(pending, raw, strict=True)
        }
    for action, transition in successors:
        assert transition.after is not None
        value = (
            float(transition.after.scalar.score)
            if transition.after.terminal
            else predictions[action.action_id]
        )
        if not math.isfinite(value):
            raise ValueError("neural value estimate is not finite")
        estimates.append(
            Plan2NeuralActionEstimate(
                action,
                value,
                transition.after.terminal,
                transition.trace,
            )
        )
    estimates.sort(
        key=lambda value: (value.predicted_terminal_score, value.action_id),
        reverse=True,
    )
    native_action_id = (
        None
        if native_result is None or native_result.best_action is None
        else native_result.best_action.action_id
    )
    return Plan2NeuralShadowDecision(
        tuple(estimates),
        native_action_id,
        tuple(dict.fromkeys(blockers)),
    )


__all__ = [
    "Plan2NeuralActionEstimate",
    "Plan2NeuralShadowDecision",
    "Plan2ValuePredictor",
    "Plan2ValueNetworkLeafModel",
    "Plan2ResidualValueNetworkLeafModel",
    "rank_plan2_actions_by_value_network",
]
