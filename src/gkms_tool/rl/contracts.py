"""Immutable, JSON-only contracts. No game IO, native pointers or torch imports."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any, Mapping


class ContractError(ValueError):
    pass


def text(value: str, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ContractError(f"{name}: nonempty text required")
    return value


def finite(value: float, name: str, minimum: float | None = None) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ContractError(f"{name}: finite number required")
    if minimum is not None and value < minimum:
        raise ContractError(f"{name}: must be >= {minimum}")
    return float(value)


def integer(value: int, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{name}: integer >= {minimum} required")
    return value


def sha256(value: str, name: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ContractError(f"{name}: lowercase SHA-256 required")
    return value


def _json_value(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        finite(value, "JSON value")
        return
    if type(value) in (list, tuple):
        for child in value:
            _json_value(child)
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if type(key) is not str:
                raise ContractError("JSON keys must be strings")
            _json_value(child)
        return
    raise ContractError(f"not a JSON value: {type(value).__name__}")


def canonical(value: Any) -> str:
    _json_value(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenJSON:
    """Own serialized bytes, never a mutable caller-owned dictionary."""
    encoded: str

    def __post_init__(self) -> None:
        if type(self.encoded) is not str:
            raise ContractError("JSON text required")
        try:
            value = json.loads(self.encoded)
        except (ValueError, TypeError) as error:
            raise ContractError("invalid JSON") from error
        if canonical(value) != self.encoded:
            raise ContractError("JSON must be canonical (duplicates/nonfinite values rejected)")

    @classmethod
    def of(cls, value: Any) -> FrozenJSON:
        return cls(canonical(value))

    def unpack(self) -> Any:
        return json.loads(self.encoded)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.encoded.encode("utf-8")).hexdigest()


class DecisionKind(str, Enum):
    MAIN = "exam_main"
    SECONDARY = "exam_secondary"
    OUTER = "outer_choice"
    TERMINAL = "terminal"


class ActionKind(str, Enum):
    PLAY = "play"
    DRINK = "drink"
    END = "end_turn"
    SELECT = "select"
    CONFIRM = "confirm"
    TAKE_CARD = "take_card"
    UPGRADE = "upgrade"
    CUSTOMIZE = "customize"
    REMOVE = "remove"
    DUPLICATE = "duplicate"
    TAKE_DRINK = "take_drink"
    REPLACE_DRINK = "replace_drink"
    BUY = "buy"
    EVENT = "event"
    SCHEDULE = "schedule"
    DECLINE = "decline"


@dataclass(frozen=True, slots=True)
class Scope:
    run_id: str
    exam_id: str | None
    mode_id: str
    flow_id: str
    stage_id: str

    def __post_init__(self) -> None:
        for name in ("run_id", "mode_id", "flow_id", "stage_id"):
            text(getattr(self, name), name)
        if self.exam_id is not None:
            text(self.exam_id, "exam_id")


@dataclass(frozen=True, slots=True)
class Binding:
    engine_id: str
    metadata_sha256: str
    master_sha256: str
    feature_schema_sha256: str
    objective_id: str
    information_policy_id: str
    model_sha256: str
    planner_sha256: str
    continuation_policy_sha256: str

    def __post_init__(self) -> None:
        for name in ("engine_id", "objective_id", "information_policy_id"):
            text(getattr(self, name), name)
        for name in ("metadata_sha256", "master_sha256", "feature_schema_sha256",
                     "model_sha256", "planner_sha256", "continuation_policy_sha256"):
            sha256(getattr(self, name), name)

    @property
    def identity(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True, slots=True)
class State:
    scope: Scope
    session_generation: str
    revision: str
    kind: DecisionKind
    information: FrozenJSON
    binding_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope) or not isinstance(self.kind, DecisionKind):
            raise ContractError("typed scope and decision kind required")
        if not isinstance(self.information, FrozenJSON):
            raise ContractError("immutable information state required")
        text(self.session_generation, "session_generation")
        text(self.revision, "revision")
        sha256(self.binding_id, "binding_id")
        if self.kind in (DecisionKind.MAIN, DecisionKind.SECONDARY) and self.scope.exam_id is None:
            raise ContractError("exam decision requires exam_id")

    @property
    def identity(self) -> str:
        return digest({"scope": asdict(self.scope), "session": self.session_generation,
                       "revision": self.revision, "kind": self.kind.value,
                       "information": self.information.sha256, "binding": self.binding_id})


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    candidate_id: str
    target: FrozenJSON
    root_identity: str
    session_generation: str
    revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActionKind) or not isinstance(self.target, FrozenJSON):
            raise ContractError("typed action kind and immutable target required")
        text(self.candidate_id, "candidate_id")
        sha256(self.root_identity, "root_identity")
        text(self.session_generation, "session_generation")
        text(self.revision, "revision")

    def validate_at(self, state: State) -> None:
        if (self.root_identity != state.identity or self.revision != state.revision
                or self.session_generation != state.session_generation):
            raise ContractError("stale action/state/session binding")


@dataclass(frozen=True, slots=True)
class Outcome:
    probability: float
    after: State
    goal_done: bool = False
    domain_done: bool = False
    truncated: bool = False
    observed_terminal_score: float | None = None
    actual_game_failure: bool = False

    def __post_init__(self) -> None:
        if finite(self.probability, "probability", 0) > 1:
            raise ContractError("probability exceeds one")
        if not isinstance(self.after, State):
            raise ContractError("typed after state required")
        for name in ("goal_done", "domain_done", "truncated", "actual_game_failure"):
            if type(getattr(self, name)) is not bool:
                raise ContractError(f"{name}: boolean required")
        if self.goal_done and (not self.domain_done or self.truncated):
            raise ContractError("completed goal must finish domain and not be truncated")
        if (self.after.kind is DecisionKind.TERMINAL) != self.goal_done:
            raise ContractError("terminal state and completed goal must agree")
        if self.observed_terminal_score is not None:
            finite(self.observed_terminal_score, "terminal score", 0)
            if not self.domain_done or self.truncated:
                raise ContractError("observed terminal score needs a genuine domain end")
        if self.actual_game_failure and not self.goal_done:
            raise ContractError("actual failure must be a verified completed objective")
        if self.actual_game_failure and self.observed_terminal_score is not None:
            raise ContractError("do not invent actual score for failure utility")
        if self.goal_done and self.observed_terminal_score is None and not self.actual_game_failure:
            raise ContractError("completed goal lacks score/failure evidence")


def validate_expansion(root: State, action: Action, outcomes: tuple[Outcome, ...], *,
                       objective_id: str | None = None) -> None:
    # Lifetime adapters can validate an objective-neutral transition; a scorer
    # must additionally bind its objective. An exam's terminal score can never
    # be borrowed from another exam, even at a genuine domain boundary.
    if objective_id not in (None, "exam_score_v1", "produce_final_exam_v1"):
        raise ContractError("unknown transition objective")
    action.validate_at(root)
    if type(outcomes) is not tuple or not outcomes or any(not isinstance(x, Outcome) for x in outcomes):
        raise ContractError("immutable nonempty outcome tuple required")
    if not math.isclose(math.fsum(x.probability for x in outcomes), 1.0, rel_tol=0, abs_tol=1e-9):
        raise ContractError("chance probability mass differs from one")
    for item in outcomes:
        if item.after.binding_id != root.binding_id or item.after.scope.run_id != root.scope.run_id:
            raise ContractError("branch escaped run/asset binding")
        if item.after.session_generation != root.session_generation:
            raise ContractError("branch changed simulator session")
        if objective_id == "exam_score_v1" and item.after.scope != root.scope:
            raise ContractError("exam objective branch changed exam/mode/flow/stage scope")
        if not item.domain_done and item.after.scope != root.scope:
            raise ContractError("scope changed without a domain boundary")
