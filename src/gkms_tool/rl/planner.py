"""Iterative-deepening expectimax with full root coverage and batched leaf values.

All values are expected TOTAL terminal scores on the objective's scale.
No score_delta is added. Search submits no game actions. Cooperative time
checks cannot interrupt an arbitrary native call; use worker.run_isolated for
an actual wall-time boundary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math
import time
from typing import Callable, Sequence
from .contracts import Action, ContractError, DecisionKind, State, digest, finite, integer
from .objectives import ScoreObjective
from .simulator import Expansion, Simulator, SimulationCancelled, validate_actions, validate_branch


@dataclass(frozen=True, slots=True)
class SearchConfig:
    max_depth: int = 2
    max_nodes: int = 256
    samples: int = 4
    soft_ms: float = 300
    hard_ms: float = 1000
    seed: int = 0

    def __post_init__(self):
        for name in ("max_depth", "max_nodes", "samples"):
            integer(getattr(self, name), name, 1)
        integer(self.seed, "seed")
        finite(self.soft_ms, "soft_ms", 0)
        finite(self.hard_ms, "hard_ms", 0)
        if self.hard_ms <= 0 or self.soft_ms > self.hard_ms:
            raise ContractError("invalid soft/hard budget")
        if self.max_depth > 32:
            raise ContractError("depth exceeds guarded recursion budget")

    @property
    def identity(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True, slots=True)
class SearchResult:
    root_identity: str
    action: Action | None
    root_values: tuple[tuple[str, float], ...]
    completed_depth: int
    nodes: int
    samples: int
    elapsed_ms: float
    status: str
    reason: str


class _Budget(Exception):
    pass


class _Cancelled(Exception):
    pass


@dataclass(frozen=True)
class _Leaf:
    key: str


@dataclass(frozen=True)
class _Choice:
    children: tuple


@dataclass(frozen=True)
class _Chance:
    children: tuple  # (probability, child)


def search(root: State, simulator: Simulator, value_batch: Callable[[Sequence[State]], Sequence[float]],
           *, objective: ScoreObjective = ScoreObjective(), config: SearchConfig = SearchConfig(),
           cancelled: Callable[[], bool] = lambda: False, clock: Callable[[], float] = time.monotonic) -> SearchResult:
    started = clock()
    nodes = sample_count = 0
    completed_depth = 0
    last_values: tuple[tuple[str, float], ...] = ()
    action_cache: dict[str, tuple[Action, ...]] = {}
    expansion_cache: dict[tuple[str, str], Expansion] = {}

    def elapsed():
        return max(0., (clock() - started) * 1000)

    def check():
        if cancelled():
            raise _Cancelled()
        if elapsed() >= config.hard_ms:
            raise _Budget("time budget")

    def actions(s):
        check()
        if s.identity not in action_cache:
            result = simulator.actions(s)
            check()
            validate_actions(s, result)
            action_cache[s.identity] = result
        return action_cache[s.identity]

    def expand(s, a):
        nonlocal nodes, sample_count
        check()
        key = (s.identity, a.candidate_id)
        if key not in expansion_cache:
            if nodes >= config.max_nodes:
                raise _Budget("node budget")
            seed_bytes = hashlib.sha256(f"{config.seed}:{s.identity}:{a.candidate_id}".encode()).digest()
            branch = simulator.expand(s, a, seed=int.from_bytes(seed_bytes[:8], "big"), samples=config.samples)
            check()
            validate_branch(s, a, branch, objective_id=objective.objective_id)
            nodes += len(branch.outcomes)
            if nodes > config.max_nodes:
                raise _Budget("node budget")
            sample_count += branch.samples
            expansion_cache[key] = branch
        return expansion_cache[key]

    def build_action(s, a, depth, leaves):
        branch = expand(s, a)
        children = []
        for out in branch.outcomes:
            if out.probability == 0:
                continue
            value = objective.terminal_value(out)
            if value is not None:
                child = value
            elif depth == 1:
                leaves[out.after.identity] = out.after
                child = _Leaf(out.after.identity)
            else:
                child = _Choice(tuple(build_action(out.after, nxt, depth - 1, leaves)
                                      for nxt in actions(out.after)))
            children.append((out.probability, child))
        return _Chance(tuple(children))

    def backup(node, values):
        if isinstance(node, _Leaf):
            return values[node.key]
        if isinstance(node, _Chance):
            return math.fsum(p * backup(child, values) for p, child in node.children)
        if isinstance(node, _Choice):
            return max(backup(child, values) for child in node.children)
        return finite(node, "terminal value")

    def result(status, reason, root_actions=()):
        selected = None
        if status == "ready":
            # Stable tie-break does not use simulator action enumeration order.
            best_id = min(last_values, key=lambda row: (-row[1], row[0]))[0]
            selected = next(a for a in root_actions if a.candidate_id == best_id)
        return SearchResult(root.identity, selected, last_values, completed_depth,
                            nodes, sample_count, elapsed(), status, reason)

    root_actions = ()
    try:
        if root.kind is DecisionKind.TERMINAL:
            return result("unsupported", "terminal root has no action")
        root_actions = actions(root)
        for depth in range(1, config.max_depth + 1):
            leaves: dict[str, State] = {}
            trees = tuple(build_action(root, a, depth, leaves) for a in root_actions)
            check()
            keys = tuple(leaves)
            predictions = tuple(value_batch(tuple(leaves.values()))) if leaves else ()
            check()
            if len(predictions) != len(keys):
                raise ContractError("value batch length differs from leaves")
            values = {key: finite(v, "leaf value") for key, v in zip(keys, predictions)}
            iteration = tuple((a.candidate_id, finite(backup(tree, values), "root value"))
                              for a, tree in zip(root_actions, trees))
            last_values, completed_depth = iteration, depth
            if not leaves or elapsed() >= config.soft_ms:
                break
        return result("ready", "complete root comparison", root_actions)
    except (_Cancelled, SimulationCancelled):
        return result("cancelled", "stop requested")
    except _Budget as error:
        return result("ready" if last_values else "budget_exhausted", str(error), root_actions)
    except (ContractError, NotImplementedError) as error:
        return result("unsupported", str(error))
