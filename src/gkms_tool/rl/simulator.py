"""The single Planner boundary: actions(State) and expand(State, Action).

Keep native restore/clone/release behind the existing IsolatedBranchAdapter.
Stateless backends may implement Simulator directly. Neither protocol owns live
input. Contract checks do not qualify game physics or hidden-RNG information.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable
from .contracts import Action, ContractError, DecisionKind, Outcome, State, integer, validate_expansion


@dataclass(frozen=True, slots=True)
class Expansion:
    outcomes: tuple[Outcome, ...]
    exact: bool = True
    samples: int = 0

    def __post_init__(self):
        if type(self.exact) is not bool:
            raise ContractError("exact must be boolean")
        integer(self.samples, "samples")
        if self.exact and self.samples or not self.exact and not self.samples:
            raise ContractError("sample count and exactness disagree")
        if type(self.outcomes) is not tuple or not self.outcomes or any(
                not isinstance(outcome, Outcome) for outcome in self.outcomes):
            raise ContractError("immutable nonempty outcome tuple required")


@runtime_checkable
class Simulator(Protocol):
    """Public decision boundary; only information available now may affect it.

    actions: all legal atomic choices at this main/secondary/outer boundary.
    expand: independent hypothetical successors, never a live submission.
    The seed is SEARCH sampling randomness, not the actual environment RNG.
    A next visible draw is a new decision; hidden worlds must not get separate
    hindsight choices while they still represent the same information state.
    """
    def actions(self, state: State) -> tuple[Action, ...]: ...
    def expand(self, state: State, action: Action, *, seed: int, samples: int) -> Expansion: ...


def validate_actions(state: State, actions: tuple[Action, ...]) -> tuple[Action, ...]:
    """One shared candidate contract for adapters, Planner and offline checks."""
    if not isinstance(state, State) or state.kind is DecisionKind.TERMINAL:
        raise ContractError("legal actions require a nonterminal State")
    if type(actions) is not tuple or not actions or any(not isinstance(a, Action) for a in actions):
        raise ContractError("nonterminal node has no complete action tuple")
    if len({a.candidate_id for a in actions}) != len(actions):
        raise ContractError("duplicate candidate IDs")
    for action in actions:
        action.validate_at(state)
    return actions


def validate_branch(state: State, action: Action, expansion: Expansion, *,
                    objective_id: str | None = None) -> Expansion:
    """Validate chance mass, scope, terminal semantics and comparable outcomes."""
    if not isinstance(expansion, Expansion):
        raise ContractError("simulator did not return Expansion")
    validate_expansion(state, action, expansion.outcomes, objective_id=objective_id)
    if any(outcome.truncated for outcome in expansion.outcomes):
        raise ContractError("truncated branch has no completed comparison")
    # This expectimax cannot choose separately for hidden worlds that expose
    # the same information. Reject that representation before Value or max;
    # do not pick/cache the first world or pretend it is a belief transition.
    # Genuine visible draws and repeated copies of one complete State remain
    # supported. This local guard is not native information-safety qualification.
    visible = {}
    for outcome in expansion.outcomes:
        if outcome.probability == 0 or outcome.goal_done:
            continue
        after = outcome.after
        key = (after.scope, after.kind, after.information, after.binding_id)
        previous = visible.setdefault(key, after.identity)
        if previous != after.identity:
            raise ContractError("indistinguishable nonterminal outcomes require an information-state backend")
    return expansion


class NativeBranchBackend(Protocol):
    """Private adapter details. Implement over an isolated engine, not Gateway.

    restore must own an independent handle. fingerprint covers all restorable
    state, including RNG/queues/selectors. clone must own another handle.
    Return only portable information-state Outcomes from expand. Caller must
    separately establish those native and information-safety properties.
    """
    def restore(self, state: State) -> object: ...
    def fingerprint(self, handle: object) -> str: ...
    def clone(self, handle: object) -> object: ...
    def actions(self, handle: object) -> tuple[Action, ...]: ...
    def expand(self, handle: object, action: Action, *, seed: int, samples: int) -> Expansion: ...
    def release(self, handle: object) -> None: ...


class SimulationCancelled(ContractError):
    """Cooperative cancellation; owned handles must still be released."""


class IsolatedBranchAdapter:
    """Existing native lifetime adapter; no second engine or controller.

    Cancellation is checked around backend calls, not inside an uninterruptible
    native call. The existing isolated worker remains the hard-timeout boundary.
    A backend that allocates and then raises before returning a handle owns that
    failed allocation's cleanup. Handle aliasing inside native objects must also
    be qualified by the backend; Python object checks cannot establish that.
    """
    def __init__(self, backend: NativeBranchBackend, *, cancelled: Callable[[], bool] = lambda: False):
        if not callable(cancelled):
            raise ContractError("cancelled must be callable")
        self.backend, self.cancelled = backend, cancelled

    def _check_cancelled(self):
        if self.cancelled():
            raise SimulationCancelled("stop requested")

    def _read_actions(self, handle, state):
        self._check_cancelled()
        before = self.backend.fingerprint(handle)
        try:
            result = validate_actions(state, self.backend.actions(handle))
            self._check_cancelled()
            return result
        finally:
            if self.backend.fingerprint(handle) != before:
                raise ContractError("legality query mutated native parent")

    def actions(self, state: State) -> tuple[Action, ...]:
        if not isinstance(state, State) or state.kind is DecisionKind.TERMINAL:
            raise ContractError("legal actions require a nonterminal State")
        self._check_cancelled()
        handle = self.backend.restore(state)
        if handle is None:
            raise ContractError("restore returned a null handle")
        try:
            result = self._read_actions(handle, state)
        finally:
            self.backend.release(handle)
        self._check_cancelled()
        return result

    def expand(self, state: State, action: Action, *, seed: int, samples: int) -> Expansion:
        if not isinstance(state, State) or state.kind is DecisionKind.TERMINAL:
            raise ContractError("candidate expansion requires a nonterminal State")
        if not isinstance(action, Action):
            raise ContractError("candidate expansion requires a typed Action")
        action.validate_at(state)
        integer(seed, "search seed")
        integer(samples, "chance samples", 1)
        self._check_cancelled()
        parent = self.backend.restore(state)
        if parent is None:
            raise ContractError("restore returned a null handle")
        branch = None
        try:
            # State identity alone does not prove the exact target was offered.
            # Re-query this restored parent, not a caller-supplied candidate ID.
            legal = self._read_actions(parent, state)
            if action not in legal:
                raise ContractError("candidate is not an exact offered native action")
            before = self.backend.fingerprint(parent)
            try:
                self._check_cancelled()
                branch = self.backend.clone(parent)
                if branch is parent:
                    branch = None
                    raise ContractError("clone returned the parent object")
                if branch is None:
                    raise ContractError("clone returned a null handle")
                self._check_cancelled()
                if self.backend.fingerprint(branch) != before:
                    raise ContractError("clone does not reproduce complete native parent")
                result = self.backend.expand(branch, action, seed=seed, samples=samples)
                self._check_cancelled()
                result = validate_branch(state, action, result)
            finally:
                try:
                    if branch is not None:
                        self.backend.release(branch)
                finally:
                    # Includes failed clone and branch cleanup, not just step.
                    if self.backend.fingerprint(parent) != before:
                        raise ContractError("candidate expansion polluted native parent or RNG")
        finally:
            self.backend.release(parent)
        self._check_cancelled()
        return result


def qualify_branch_order(adapter: Simulator, root: State, *, seed: int = 0, samples: int = 4) -> dict:
    """Check action order/replay stability; not proof of full native compatibility."""
    actions = validate_actions(root, adapter.actions(root))
    def run(order):
        return {a.candidate_id: validate_branch(root, a, adapter.expand(root, a, seed=seed, samples=samples))
                for a in order}
    if run(actions) != run(reversed(actions)):
        raise ContractError("branch expansion depends on evaluation order")
    return {"action_count": len(actions), "order_independent": True,
            "native_physics_qualified": False, "scope": "supplied-root-only"}
