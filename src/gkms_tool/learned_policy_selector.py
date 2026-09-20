"""Plan-neutral learned-policy selection over one exact legal boundary.

The selector owns no game input path and has no Maa fallback.  A verified,
runtime-enabled Offline RL policy gets first refusal; Exact BC is the only
fallback.  Both policies consume the same immutable
``UnifiedLegalActionSnapshot`` so candidate identity and normalization remain
owned by the existing native/legal and Exact BC contracts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import math
from typing import Any, Final, Protocol

from .exact_bc_live_canary import (
    ExactBCCanaryPolicy,
    ExactBCPolicyBundle,
    select_exact_bc_canary_action,
)
from .policy_router import (
    EXAM_OFFLINE_RL,
    EXACT_EXAM_BC,
    PolicyProposal,
    RouteStatus,
    route_policy,
)
from .unified_legal_action_snapshot import UnifiedLegalActionSnapshot


SCHEMA: Final = "gkms.plan-neutral-learned-selector.v1"


@dataclass(frozen=True, slots=True)
class OfflineRLActionProposal:
    """Dependency-injected Offline RL answer at one exact boundary."""

    action_id: str | None = None
    probability: float | None = None
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        blockers = tuple(self.blockers)
        if any(not isinstance(value, str) or not value for value in blockers):
            raise ValueError("Offline RL proposal blockers must be non-empty text")
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(blockers)))
        if self.action_id is None:
            if not blockers:
                raise ValueError("abstaining Offline RL proposal needs a blocker")
            if self.probability is not None:
                raise ValueError("abstaining Offline RL proposal has no probability")
            return
        if not isinstance(self.action_id, str) or not self.action_id:
            raise ValueError("Offline RL proposal action_id must be non-empty text")
        if blockers:
            raise ValueError("ready Offline RL proposal cannot have blockers")
        probability = self.probability
        if (
            isinstance(probability, bool)
            or not isinstance(probability, (int, float))
            or not math.isfinite(float(probability))
            or not 0.0 <= float(probability) <= 1.0
        ):
            raise ValueError("Offline RL proposal probability must be within 0..1")

    @property
    def ready(self) -> bool:
        return self.action_id is not None and not self.blockers


class OfflineRLSelector(Protocol):
    """Minimal seam for the validated IQL scorer that is still being trained."""

    @property
    def policy_id(self) -> str: ...

    @property
    def validated(self) -> bool: ...

    @property
    def runtime_enabled(self) -> bool: ...

    def select_action(
        self,
        *,
        state_before: Mapping[str, Any],
        snapshot: UnifiedLegalActionSnapshot,
        stage: str,
    ) -> OfflineRLActionProposal: ...


@dataclass(frozen=True, slots=True)
class LearnedPolicyDecision:
    status: str
    flow: str
    stage: str
    boundary_digest: str
    action_id: str | None = None
    source: str | None = None
    probability: float | None = None
    candidate: Mapping[str, object] | None = None
    blockers: tuple[str, ...] = ()
    policy_id: str | None = None
    schema: str = SCHEMA

    @property
    def ready(self) -> bool:
        return (
            self.status == "ready"
            and self.action_id is not None
            and self.source in {"offline_rl", "behavior_cloning"}
            and not self.blockers
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status,
            "ready": self.ready,
            "flow": self.flow,
            "stage": self.stage,
            "boundary_digest": self.boundary_digest,
            "action_id": self.action_id,
            "source": self.source,
            "probability": self.probability,
            "candidate": None if self.candidate is None else dict(self.candidate),
            "blockers": list(self.blockers),
            "policy_id": self.policy_id,
        }


def _decision(
    snapshot: UnifiedLegalActionSnapshot,
    stage: str,
    *,
    action_id: str | None = None,
    source: str | None = None,
    probability: float | None = None,
    candidate: Mapping[str, object] | None = None,
    blockers: tuple[str, ...] = (),
    policy_id: str | None = None,
) -> LearnedPolicyDecision:
    return LearnedPolicyDecision(
        status="ready" if action_id is not None and not blockers else "no-decision",
        flow=snapshot.flow_id,
        stage=stage,
        boundary_digest=snapshot.boundary_digest,
        action_id=action_id,
        source=source,
        probability=probability,
        candidate=None if candidate is None else dict(candidate),
        blockers=tuple(dict.fromkeys(blockers)),
        policy_id=policy_id,
    )


def _exam_router_components(
    bc_bundle: ExactBCPolicyBundle | None,
) -> dict[str, Mapping[str, Any]]:
    """Expose only existing PolicyBundle component gates to the pure Router.

    Older injected/test bundles may not carry explicit Router gates.  In that
    case the Router reports INACTIVE and the selector preserves its pre-Router
    behavior exactly.
    """

    if bc_bundle is None:
        return {}
    components: dict[str, Mapping[str, Any]] = {}
    for role in ("offline_rl_policy", "exact_exam_policy"):
        try:
            component = bc_bundle.component(role)
        except Exception:
            continue
        if isinstance(component, Mapping):
            components[role] = component
    return components


def _proposal_reason(prefix: str, blockers: tuple[str, ...]) -> str:
    detail = ",".join(blockers)
    return f"{prefix}:{detail}" if detail else f"{prefix}:abstained"


def _apply_exam_route(
    *,
    snapshot: UnifiedLegalActionSnapshot,
    stage: str,
    components: Mapping[str, object],
    proposals: Mapping[str, PolicyProposal],
    legacy: LearnedPolicyDecision,
) -> LearnedPolicyDecision:
    """Apply Router ownership without changing the learned decision schema."""

    routed = route_policy(
        "exam",
        components,
        proposals=proposals,
    )
    if routed.status is RouteStatus.INACTIVE:
        # No Router component acquired ownership.  Direct callers and older
        # bundles retain the exact behavior they had before Router wiring.
        return legacy
    if routed.status is RouteStatus.READY:
        selected = routed.decision
        if isinstance(selected, LearnedPolicyDecision) and selected.ready:
            return selected
        return _decision(
            snapshot,
            stage,
            blockers=("policy-router-ready-decision-invalid",),
        )
    if routed.status is RouteStatus.NO_DECISION:
        if not legacy.ready:
            # Keep the established learned-chain blocker vocabulary consumed
            # by Plan1/2/3 stop receipts.
            return legacy
        return _decision(
            snapshot,
            stage,
            blockers=tuple(
                dict.fromkeys(
                    f"policy-router:{value}" for value in routed.blockers
                )
            )
            or ("policy-router:no-decision",),
        )
    # "exam" cannot normally produce handoff/unknown here.  Fail closed if a
    # future Router contract does so instead of leaking a learned action.
    return _decision(
        snapshot,
        stage,
        blockers=(f"policy-router:{routed.status.value}",),
    )


def select_plan_neutral_learned_action(
    *,
    state_before: Mapping[str, Any],
    snapshot: UnifiedLegalActionSnapshot,
    stage: str,
    offline_rl: OfflineRLSelector | None = None,
    bc_bundle: ExactBCPolicyBundle | None = None,
    bc_policy: ExactBCCanaryPolicy = ExactBCCanaryPolicy(),
) -> LearnedPolicyDecision:
    """Choose Offline RL, then Exact BC, or return an explicit no-decision."""

    if not isinstance(state_before, Mapping):
        raise TypeError("learned selector state_before must be a mapping")
    if not isinstance(snapshot, UnifiedLegalActionSnapshot):
        raise TypeError("learned selector snapshot must be UnifiedLegalActionSnapshot")
    if not isinstance(stage, str) or not stage:
        raise ValueError("learned selector stage must be non-empty text")

    blockers: list[str] = []
    transfer_route = False
    if bc_bundle is not None:
        try:
            declaration = bc_bundle.component("exact_exam_policy").get("mode_transfer")
        except Exception:
            declaration = None
        if declaration is not None:
            from .policy_mode_coverage import resolve_exam_policy_mode_route
            route = resolve_exam_policy_mode_route(
                bc_bundle,
                produce_id=state_before.get("produceId", state_before.get("produce_id")),
                plan_type=state_before.get("planType", state_before.get("plan_type")),
                main_effect_type=state_before.get("mainEffectType", state_before.get("main_effect_type")),
                stage=stage,
                stage_kind="lesson" if state_before.get("examType", state_before.get("exam_type")) == 0 else "audition",
            )
            if not route.available or route.flow != snapshot.flow_id:
                return _decision(snapshot, stage, blockers=tuple("model-mode:" + reason for reason in route.blockers) or ("model-mode:native-flow-mismatch",))
            transfer_route = route.status == "bc-only-transfer"
            if transfer_route:
                # Only the declared actual flow is granted to BC. No state,
                # stage, scoring context or confidence threshold is rewritten.
                bc_policy = replace(bc_policy, allowed_flows=(snapshot.flow_id,))
    router_components = _exam_router_components(bc_bundle)
    router_proposals: dict[str, PolicyProposal] = {}
    if transfer_route:
        blockers.append("offline-rl-disabled-for-unvalidated-mode-transfer")
    elif offline_rl is None:
        blockers.append("offline-rl-unavailable")
    elif getattr(offline_rl, "validated", None) is not True:
        blockers.append("offline-rl-not-validated")
    elif getattr(offline_rl, "runtime_enabled", None) is not True:
        blockers.append("offline-rl-runtime-disabled")
    else:
        policy_id = getattr(offline_rl, "policy_id", None)
        policy_id = policy_id if isinstance(policy_id, str) and policy_id else None
        try:
            proposal = offline_rl.select_action(
                state_before=state_before,
                snapshot=snapshot,
                stage=stage,
            )
        except Exception as error:
            blockers.append(f"offline-rl-score-failed:{type(error).__name__}")
        else:
            if not isinstance(proposal, OfflineRLActionProposal):
                blockers.append("offline-rl-proposal-invalid")
            elif not proposal.ready:
                blockers.extend(f"offline-rl:{value}" for value in proposal.blockers)
            elif proposal.action_id not in snapshot.action_ids:
                blockers.append("offline-rl-action-not-legal")
            else:
                index = snapshot.action_ids.index(proposal.action_id)
                rl_decision = _decision(
                    snapshot,
                    stage,
                    action_id=proposal.action_id,
                    source="offline_rl",
                    probability=float(proposal.probability),
                    candidate=snapshot.legal_candidates[index],
                    policy_id=policy_id,
                )
                router_proposals[EXAM_OFFLINE_RL] = PolicyProposal.choose(
                    rl_decision
                )
                routed = _apply_exam_route(
                    snapshot=snapshot,
                    stage=stage,
                    components=router_components,
                    proposals=router_proposals,
                    legacy=rl_decision,
                )
                if routed.ready or routed is rl_decision:
                    return routed
                # RL produced a decision but its Router component did not own
                # this active Exam chain.  Let Exact BC produce the only
                # possible fallback proposal before the final route.

    if EXAM_OFFLINE_RL not in router_proposals:
        router_proposals[EXAM_OFFLINE_RL] = PolicyProposal.abstain(
            _proposal_reason("offline-rl", tuple(blockers))
        )

    if bc_bundle is None:
        blockers.append("bc-unavailable")
        router_proposals[EXACT_EXAM_BC] = PolicyProposal.abstain(
            "bc:unavailable"
        )
    else:
        bc = select_exact_bc_canary_action(
            bundle=bc_bundle,
            state_before=state_before,
            snapshot=snapshot,
            stage=stage,
            policy=bc_policy,
        )
        if bc.ready:
            bc_decision = _decision(
                snapshot,
                stage,
                action_id=bc.action_id,
                source="behavior_cloning",
                probability=bc.probability,
                candidate=bc.candidate,
                policy_id=bc.bundle_id,
            )
            router_proposals[EXACT_EXAM_BC] = PolicyProposal.choose(
                bc_decision
            )
            return _apply_exam_route(
                snapshot=snapshot,
                stage=stage,
                components=router_components,
                proposals=router_proposals,
                legacy=bc_decision,
            )
        blockers.extend(f"bc:{value}" for value in bc.blockers)
        router_proposals[EXACT_EXAM_BC] = PolicyProposal.abstain(
            _proposal_reason("bc", tuple(bc.blockers))
        )

    no_decision = _decision(snapshot, stage, blockers=tuple(blockers))
    return _apply_exam_route(
        snapshot=snapshot,
        stage=stage,
        components=router_components,
        proposals=router_proposals,
        legacy=no_decision,
    )


__all__ = [
    "LearnedPolicyDecision",
    "OfflineRLActionProposal",
    "OfflineRLSelector",
    "SCHEMA",
    "select_plan_neutral_learned_action",
]
