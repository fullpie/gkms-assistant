"""Pure ownership router for learned Outer and Exam policies.

This module deliberately has no game, screen, Maa, or input dependencies.  It
only decides which learned component owns *one* already-observed decision
boundary.  Maa is metadata on the result (the executor), never a policy or a
fallback decision source.

The two fixed chains are::

    Outer: outer_offline_rl -> outer_bc
    Exam:  exam_offline_rl  -> exact_exam_bc

Only an explicitly enabled, live-apply-allowed component may own a boundary.
An eligible RL component that abstains yields to BC.  If every eligible
component abstains, the result is a typed no-decision; rules and Maa are not
consulted.  If no component is live-enabled, the router stays inactive so the
existing formal controller remains untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from .produce_transaction_family import TRANSACTION_FAMILIES


OUTER_OFFLINE_RL: Final = "outer_offline_rl"
OUTER_BC: Final = "outer_bc"
EXAM_OFFLINE_RL: Final = "exam_offline_rl"
EXACT_EXAM_BC: Final = "exact_exam_bc"
EXAM_SURFACE: Final = "exam"
MAA_EXECUTOR: Final = "maa"

OUTER_CHAIN: Final = (OUTER_OFFLINE_RL, OUTER_BC)
EXAM_CHAIN: Final = (EXAM_OFFLINE_RL, EXACT_EXAM_BC)

# Existing bundle role names remain readable without changing their meaning.
# The generic Offline RL artifact in policy_bundle.py is the existing Exam IQL
# artifact, hence it is intentionally not an alias for Outer RL.
_COMPONENT_ALIASES: Final[Mapping[str, tuple[str, ...]]] = {
    OUTER_OFFLINE_RL: (OUTER_OFFLINE_RL,),
    OUTER_BC: (OUTER_BC, "outer_policy"),
    EXAM_OFFLINE_RL: (EXAM_OFFLINE_RL, "offline_rl_policy"),
    EXACT_EXAM_BC: (EXACT_EXAM_BC, "exact_exam_policy"),
}


class RouteStatus(StrEnum):
    """Exhaustive outcome type for one routing boundary."""

    READY = "ready"
    NO_DECISION = "no-decision"
    INACTIVE = "inactive"
    HANDOFF = "handoff"
    UNKNOWN_SURFACE = "unknown-surface"


class ProposalStatus(StrEnum):
    DECISION = "decision"
    ABSTAIN = "abstain"


@dataclass(frozen=True, slots=True)
class PolicyProposal:
    """One component's answer, independent of the action's payload type."""

    status: ProposalStatus
    decision: object | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProposalStatus):
            try:
                object.__setattr__(self, "status", ProposalStatus(self.status))
            except (TypeError, ValueError) as error:
                raise ValueError("unknown policy proposal status") from error
        if self.status is ProposalStatus.DECISION:
            if self.decision is None:
                raise ValueError("decision proposal requires a payload")
            if self.reason is not None:
                raise ValueError("decision proposal cannot have an abstain reason")
        else:
            if self.decision is not None:
                raise ValueError("abstaining proposal cannot carry a decision")
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("abstaining proposal requires a reason")

    @classmethod
    def choose(cls, decision: object) -> "PolicyProposal":
        return cls(ProposalStatus.DECISION, decision=decision)

    @classmethod
    def abstain(cls, reason: str = "policy-abstained") -> "PolicyProposal":
        return cls(ProposalStatus.ABSTAIN, reason=reason)


@dataclass(frozen=True, slots=True)
class PolicyRouteDecision:
    """Typed Router output with an at-most-one-owner invariant."""

    status: RouteStatus
    surface: str
    transaction_family: str | None
    owner: str | None = None
    decision: object | None = None
    executor: str = MAA_EXECUTOR
    attempted_owners: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    handoff_to: str | None = None
    existing_formal_control_preserved: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, RouteStatus):
            object.__setattr__(self, "status", RouteStatus(self.status))
        if self.status is RouteStatus.READY:
            if self.owner not in (*OUTER_CHAIN, *EXAM_CHAIN):
                raise ValueError("ready route requires exactly one learned owner")
            if self.decision is None:
                raise ValueError("ready route requires a decision")
        elif self.owner is not None or self.decision is not None:
            raise ValueError("non-ready route cannot have a decision owner")
        if len(set(self.attempted_owners)) != len(self.attempted_owners):
            raise ValueError("a policy owner may be attempted at most once")

    @property
    def ready(self) -> bool:
        return self.status is RouteStatus.READY

    @property
    def no_decision(self) -> bool:
        return self.status is RouteStatus.NO_DECISION

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "ready": self.ready,
            "surface": self.surface,
            "transaction_family": self.transaction_family,
            "owner": self.owner,
            "decision": self.decision,
            "executor": self.executor,
            "attempted_owners": list(self.attempted_owners),
            "blockers": list(self.blockers),
            "handoff_to": self.handoff_to,
            "existing_formal_control_preserved": (
                self.existing_formal_control_preserved
            ),
        }


def _components(value: object) -> Mapping[str, object]:
    """Accept a PolicyBundle-like object or its raw component mapping."""

    if isinstance(value, Mapping):
        nested = value.get("components")
        return nested if isinstance(nested, Mapping) else value
    payload = getattr(value, "payload", None)
    if isinstance(payload, Mapping) and isinstance(payload.get("components"), Mapping):
        return payload["components"]
    return {}


def _component(
    components: Mapping[str, object], canonical_name: str
) -> tuple[Mapping[str, Any] | None, str | None]:
    for name in _COMPONENT_ALIASES[canonical_name]:
        value = components.get(name)
        if isinstance(value, Mapping):
            return value, name
    return None, None


def _proposal(
    canonical_name: str,
    component: Mapping[str, Any],
    proposals: Mapping[str, object],
    source_name: str,
) -> tuple[PolicyProposal | None, str | None]:
    raw: object = None
    supplied = False
    for name in (canonical_name, source_name):
        if name in proposals:
            raw = proposals[name]
            supplied = True
            break
    if not supplied:
        if "proposal" in component:
            raw = component["proposal"]
            supplied = True
        elif "decision" in component:
            raw = component["decision"]
            supplied = True
    if not supplied or raw is None:
        return PolicyProposal.abstain("decision-unavailable"), None
    if isinstance(raw, PolicyProposal):
        return raw, None
    if isinstance(raw, Mapping) and "status" in raw:
        status = raw.get("status")
        try:
            if status in {"decision", "ready"}:
                raw = PolicyProposal.choose(
                    raw.get("decision", raw.get("action"))
                )
            elif status in {"abstain", "no-decision"}:
                raw = PolicyProposal.abstain(
                    str(raw.get("reason") or "policy-abstained")
                )
            else:
                return None, "invalid-proposal-status"
        except (TypeError, ValueError):
            return None, "invalid-proposal"
    elif not isinstance(raw, PolicyProposal):
        try:
            raw = PolicyProposal.choose(raw)
        except (TypeError, ValueError):
            return None, "invalid-proposal"
    return raw, None


def _route_chain(
    *,
    surface: str,
    transaction_family: str | None,
    chain: tuple[str, str],
    components: Mapping[str, object],
    proposals: Mapping[str, object],
) -> PolicyRouteDecision:
    attempted: list[str] = []
    blockers: list[str] = []
    live_component_seen = False

    for owner in chain:
        raw_component, source_name = _component(components, owner)
        if raw_component is None or source_name is None:
            blockers.append(f"{owner}:component-unavailable")
            continue

        enabled = raw_component.get("enabled")
        live_allowed = raw_component.get("live_apply_allowed")
        # Missing, integer, and truthy string values all fail closed.  The
        # gates must be literal booleans in the PolicyBundle component.
        if type(enabled) is not bool:
            blockers.append(f"{owner}:enabled-not-explicit-bool")
            continue
        if type(live_allowed) is not bool:
            blockers.append(f"{owner}:live-apply-not-explicit-bool")
            continue
        if not enabled:
            blockers.append(f"{owner}:disabled")
            continue
        if not live_allowed:
            blockers.append(f"{owner}:shadow-only")
            continue

        live_component_seen = True
        attempted.append(owner)
        proposal, proposal_error = _proposal(
            owner, raw_component, proposals, source_name
        )
        if proposal_error is not None or proposal is None:
            blockers.append(f"{owner}:{proposal_error}")
            continue
        if proposal.status is ProposalStatus.ABSTAIN:
            blockers.append(f"{owner}:{proposal.reason}")
            continue
        return PolicyRouteDecision(
            status=RouteStatus.READY,
            surface=surface,
            transaction_family=transaction_family,
            owner=owner,
            decision=proposal.decision,
            attempted_owners=tuple(attempted),
        )

    status = RouteStatus.NO_DECISION if live_component_seen else RouteStatus.INACTIVE
    return PolicyRouteDecision(
        status=status,
        surface=surface,
        transaction_family=transaction_family,
        attempted_owners=tuple(attempted),
        blockers=tuple(blockers),
        existing_formal_control_preserved=status is RouteStatus.INACTIVE,
    )


def route_policy(
    surface_or_family: str,
    components: object,
    *,
    proposals: Mapping[str, object] | None = None,
    exam_terminal: bool = False,
    resume_outer_family: str | None = None,
) -> PolicyRouteDecision:
    """Route one boundary without evaluating a policy or executing an action.

    ``surface_or_family`` is either ``"exam"`` or one of the nine values in
    :data:`produce_transaction_family.TRANSACTION_FAMILIES`.  At an Exam
    terminal, ownership is relinquished to Outer.  If the next Outer family is
    already known, ``resume_outer_family`` routes that boundary immediately;
    otherwise a typed handoff result asks the caller to observe the next family.
    """

    component_map = _components(components)
    proposal_map = proposals if isinstance(proposals, Mapping) else {}

    if not isinstance(surface_or_family, str):
        return PolicyRouteDecision(
            status=RouteStatus.UNKNOWN_SURFACE,
            surface=str(surface_or_family),
            transaction_family=None,
            blockers=("surface-must-be-text",),
            existing_formal_control_preserved=True,
        )
    surface_or_family = surface_or_family.strip().lower()

    if surface_or_family == EXAM_SURFACE:
        if type(exam_terminal) is not bool:
            return PolicyRouteDecision(
                status=RouteStatus.UNKNOWN_SURFACE,
                surface=EXAM_SURFACE,
                transaction_family=None,
                blockers=("exam-terminal-must-be-bool",),
                existing_formal_control_preserved=True,
            )
        if exam_terminal:
            if resume_outer_family is not None:
                resumed = str(resume_outer_family).strip().lower()
                if resumed in TRANSACTION_FAMILIES:
                    return _route_chain(
                        surface="outer",
                        transaction_family=resumed,
                        chain=OUTER_CHAIN,
                        components=component_map,
                        proposals=proposal_map,
                    )
            blockers = (
                ("unknown-resume-outer-family",)
                if resume_outer_family is not None
                else ("awaiting-outer-family",)
            )
            return PolicyRouteDecision(
                status=RouteStatus.HANDOFF,
                surface=EXAM_SURFACE,
                transaction_family=None,
                blockers=blockers,
                handoff_to="outer",
                existing_formal_control_preserved=True,
            )
        return _route_chain(
            surface=EXAM_SURFACE,
            transaction_family=None,
            chain=EXAM_CHAIN,
            components=component_map,
            proposals=proposal_map,
        )

    if surface_or_family in TRANSACTION_FAMILIES:
        return _route_chain(
            surface="outer",
            transaction_family=surface_or_family,
            chain=OUTER_CHAIN,
            components=component_map,
            proposals=proposal_map,
        )

    return PolicyRouteDecision(
        status=RouteStatus.UNKNOWN_SURFACE,
        surface=surface_or_family,
        transaction_family=None,
        blockers=("unknown-surface-or-transaction-family",),
        existing_formal_control_preserved=True,
    )


class PolicyRouter:
    """Small state-free object façade for dependency injection."""

    def route(
        self,
        surface_or_family: str,
        components: object,
        **kwargs: object,
    ) -> PolicyRouteDecision:
        return route_policy(surface_or_family, components, **kwargs)


__all__ = [
    "EXACT_EXAM_BC",
    "EXAM_CHAIN",
    "EXAM_OFFLINE_RL",
    "EXAM_SURFACE",
    "MAA_EXECUTOR",
    "OUTER_BC",
    "OUTER_CHAIN",
    "OUTER_OFFLINE_RL",
    "PolicyProposal",
    "PolicyRouteDecision",
    "PolicyRouter",
    "ProposalStatus",
    "RouteStatus",
    "route_policy",
]
