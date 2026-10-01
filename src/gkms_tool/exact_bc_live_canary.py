"""Fail-closed Exact BC canary selection and dispatch boundary.

This module contains no game reader and no Maa implementation.  It accepts an
already-settled native state plus one complete unified legal-action snapshot,
scores the unchanged candidate order through a policy bundle, and returns a
decision only when every explicit canary gate passes.  Dispatch remains an
injected callback and is protected by a second boundary-digest comparison.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any, Final, Protocol

from .unified_legal_action_snapshot import UnifiedLegalActionSnapshot
from .exact_exam_state_projection import normalize_exact_exam_legal_candidates


SCHEMA: Final = "gkms.exact-bc-live-canary.v1"
SUPPORTED_ACTION_KINDS: Final = frozenset({"play", "drink", "end_turn"})


class ExactBCPolicyBundle(Protocol):
    @property
    def bundle_id(self) -> str: ...

    def component(self, role: str) -> Mapping[str, Any]: ...

    def score_exam(
        self,
        *,
        state_before: Mapping[str, Any],
        flow: str,
        stage: str,
        legal_candidates: Sequence[object],
        allow_diagnostic: bool = False,
    ) -> tuple[float, ...]: ...


@dataclass(frozen=True, slots=True)
class ExactBCCanaryPolicy:
    enabled: bool = False
    allowed_flows: tuple[str, ...] = ()
    minimum_probability: float = 0.55
    minimum_margin: float = 0.10

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("canary enabled must be bool")
        flows = tuple(self.allowed_flows)
        if any(not isinstance(value, str) or not value for value in flows):
            raise ValueError("canary allowed_flows must contain non-empty text")
        if len(flows) != len(set(flows)):
            raise ValueError("canary allowed_flows contains duplicates")
        object.__setattr__(self, "allowed_flows", flows)
        for name in ("minimum_probability", "minimum_margin"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"canary {name} must be within 0..1")


@dataclass(frozen=True, slots=True)
class ExactBCCanaryDecision:
    status: str
    flow: str
    stage: str
    boundary_digest: str
    bundle_id: str | None = None
    action_id: str | None = None
    candidate_index: int | None = None
    candidate: Mapping[str, object] | None = None
    probability: float | None = None
    margin: float | None = None
    blockers: tuple[str, ...] = ()
    schema: str = SCHEMA

    @property
    def ready(self) -> bool:
        return self.status == "ready" and not self.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status,
            "ready": self.ready,
            "flow": self.flow,
            "stage": self.stage,
            "boundary_digest": self.boundary_digest,
            "bundle_id": self.bundle_id,
            "action_id": self.action_id,
            "candidate_index": self.candidate_index,
            "candidate": None if self.candidate is None else dict(self.candidate),
            "probability": self.probability,
            "margin": self.margin,
            "blockers": list(self.blockers),
        }


def _blocked(
    snapshot: UnifiedLegalActionSnapshot,
    stage: str,
    *blockers: str,
    bundle_id: str | None = None,
) -> ExactBCCanaryDecision:
    return ExactBCCanaryDecision(
        status="blocked",
        flow=snapshot.flow_id,
        stage=stage,
        boundary_digest=snapshot.boundary_digest,
        bundle_id=bundle_id,
        blockers=tuple(dict.fromkeys(value for value in blockers if value)),
    )


def select_exact_bc_canary_action(
    *,
    bundle: ExactBCPolicyBundle,
    state_before: Mapping[str, Any],
    snapshot: UnifiedLegalActionSnapshot,
    stage: str,
    policy: ExactBCCanaryPolicy = ExactBCCanaryPolicy(),
) -> ExactBCCanaryDecision:
    """Return one BC action only after every formal canary gate passes."""

    if not isinstance(state_before, Mapping):
        raise TypeError("canary state_before must be a mapping")
    if not isinstance(snapshot, UnifiedLegalActionSnapshot):
        raise TypeError("canary snapshot must be UnifiedLegalActionSnapshot")
    if not isinstance(stage, str) or not stage:
        raise ValueError("canary stage must be non-empty text")
    if not isinstance(policy, ExactBCCanaryPolicy):
        raise TypeError("canary policy must be ExactBCCanaryPolicy")
    bundle_id = getattr(bundle, "bundle_id", None)
    bundle_id = bundle_id if isinstance(bundle_id, str) and bundle_id else None
    if not policy.enabled:
        return _blocked(snapshot, stage, "canary-disabled", bundle_id=bundle_id)
    if snapshot.complete is not True or snapshot.candidate_set_kind != "unified":
        return _blocked(
            snapshot,
            stage,
            "unified-legal-set-incomplete",
            bundle_id=bundle_id,
        )
    if snapshot.flow_id not in policy.allowed_flows:
        return _blocked(snapshot, stage, "flow-not-allowed", bundle_id=bundle_id)
    try:
        component = bundle.component("exact_exam_policy")
    except Exception as error:
        return _blocked(
            snapshot,
            stage,
            f"exact-component-unavailable:{type(error).__name__}",
            bundle_id=bundle_id,
        )
    if component.get("shadow_ready") is not True:
        return _blocked(snapshot, stage, "component-not-shadow-ready", bundle_id=bundle_id)
    if component.get("live_apply_allowed") is not True:
        return _blocked(snapshot, stage, "component-live-apply-disabled", bundle_id=bundle_id)
    candidates = snapshot.legal_candidates
    try:
        model_candidates = normalize_exact_exam_legal_candidates(
            state_before,
            candidates,
        )
        probabilities = tuple(
            bundle.score_exam(
                state_before=state_before,
                flow=snapshot.flow_id,
                stage=stage,
                legal_candidates=model_candidates,
                allow_diagnostic=False,
            )
        )
    except Exception as error:
        return _blocked(
            snapshot,
            stage,
            f"bc-score-failed:{type(error).__name__}",
            bundle_id=bundle_id,
        )
    if len(probabilities) != len(candidates) or not probabilities:
        return _blocked(snapshot, stage, "bc-score-count-mismatch", bundle_id=bundle_id)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
        for value in probabilities
    ):
        return _blocked(snapshot, stage, "bc-score-invalid", bundle_id=bundle_id)
    total = sum(float(value) for value in probabilities)
    if not math.isclose(total, 1.0, rel_tol=1e-4, abs_tol=1e-4):
        return _blocked(snapshot, stage, "bc-score-not-normalized", bundle_id=bundle_id)
    ranked = sorted(
        range(len(probabilities)),
        key=lambda index: (-float(probabilities[index]), index),
    )
    top = ranked[0]
    top_probability = float(probabilities[top])
    second_probability = (
        float(probabilities[ranked[1]]) if len(ranked) > 1 else 0.0
    )
    margin = top_probability - second_probability
    if len(ranked) > 1 and math.isclose(top_probability, second_probability, abs_tol=1e-12):
        return _blocked(snapshot, stage, "bc-top-action-tied", bundle_id=bundle_id)
    if top_probability < float(policy.minimum_probability):
        return _blocked(snapshot, stage, "bc-confidence-below-threshold", bundle_id=bundle_id)
    if margin < float(policy.minimum_margin):
        return _blocked(snapshot, stage, "bc-margin-below-threshold", bundle_id=bundle_id)
    candidate = candidates[top]
    action_id = candidate.get("action_id")
    kind = candidate.get("kind")
    if not isinstance(action_id, str) or not action_id:
        return _blocked(snapshot, stage, "chosen-action-id-missing", bundle_id=bundle_id)
    if kind not in SUPPORTED_ACTION_KINDS:
        return _blocked(snapshot, stage, "chosen-action-kind-unsupported", bundle_id=bundle_id)
    return ExactBCCanaryDecision(
        status="ready",
        flow=snapshot.flow_id,
        stage=stage,
        boundary_digest=snapshot.boundary_digest,
        bundle_id=bundle_id,
        action_id=action_id,
        candidate_index=top,
        candidate=dict(candidate),
        probability=top_probability,
        margin=margin,
    )


def dispatch_exact_bc_canary_action(
    decision: ExactBCCanaryDecision,
    *,
    current_boundary_digest: str,
    dispatcher: Callable[[Mapping[str, object]], object],
) -> Mapping[str, object]:
    """Dispatch a ready decision only while the exact boundary is unchanged."""

    if not isinstance(decision, ExactBCCanaryDecision):
        raise TypeError("canary decision must be ExactBCCanaryDecision")
    if not callable(dispatcher):
        raise TypeError("canary dispatcher must be callable")
    if not decision.ready or decision.candidate is None:
        return {
            "schema": SCHEMA,
            "status": "abstained",
            "input_submitted": False,
            "reason": "decision-not-ready",
        }
    if current_boundary_digest != decision.boundary_digest:
        return {
            "schema": SCHEMA,
            "status": "abstained",
            "input_submitted": False,
            "reason": "boundary-advanced-before-dispatch",
        }
    try:
        raw = dispatcher(decision.candidate)
    except Exception as error:
        return {
            "schema": SCHEMA,
            "status": "rejected",
            "input_submitted": False,
            "reason": f"dispatcher-error:{type(error).__name__}: {error}",
        }
    result = dict(raw) if isinstance(raw, Mapping) else {}
    submitted = result.get("input_submitted") is True
    accepted = result.get("accepted") is True or submitted
    return {
        "schema": SCHEMA,
        "status": "dispatched" if accepted and submitted else "rejected",
        "input_submitted": bool(accepted and submitted),
        "action_id": decision.action_id,
        "candidate_index": decision.candidate_index,
        "dispatch": result,
    }


__all__ = [
    "ExactBCCanaryDecision",
    "ExactBCCanaryPolicy",
    "SCHEMA",
    "dispatch_exact_bc_canary_action",
    "select_exact_bc_canary_action",
]
