"""Plan2 adapter from a settled native boundary to one Exact BC action.

This module does not click the game and does not own fallback policy.  It
reuses the existing Plan2 root enumerator, projects the same native state used
for training, and binds the selected candidate back to the exact typed action
that Maa's existing executor accepts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .canonical_training_labels import (
    EFFECT_BY_NATIVE_VALUE,
    PLAN_BY_NATIVE_VALUE,
    STAGE_BY_NATIVE_VALUE,
)
from .exact_bc_live_canary import (
    ExactBCCanaryDecision,
    ExactBCCanaryPolicy,
    ExactBCPolicyBundle,
    select_exact_bc_canary_action,
)
from .exact_exam_state_projection import (
    project_exact_exam_native_state,
    project_plan2_horizon_native_state,
)
from .learned_policy_selector import (
    OfflineRLSelector,
    select_plan_neutral_learned_action,
)
from .plan2_native_exam_save_orchestrator import (
    Plan2NativeExamSaveDecision,
    with_plan2_policy_action,
)
from .plan2_native_horizon import (
    Plan2NativeAction,
    Plan2NativeBlocker,
    Plan2NativeDrinkAction,
    Plan2NativeHorizonState,
    Plan2NativeOfflineAction,
    Plan2NativeTransition,
)
from .unified_legal_action_snapshot import (
    UnifiedLegalActionSnapshot,
    build_unified_legal_action_snapshot,
)
from .runtime_canonical_json import canonical_sha256


SCHEMA: Final = "gkms.plan2-exact-bc-canary-bridge.v1"


@dataclass(frozen=True, slots=True)
class Plan2ExactBCCanarySelection:
    planner_decision: object | None
    canary_decision: ExactBCCanaryDecision
    snapshot: UnifiedLegalActionSnapshot | None
    state_before: Mapping[str, object] | None
    action: Plan2NativeOfflineAction | None
    selection_source: str | None = None
    blockers: tuple[str, ...] = ()
    schema: str = SCHEMA

    @property
    def ready(self) -> bool:
        return bool(
            self.canary_decision.ready
            and self.snapshot is not None
            and self.snapshot.complete
            and self.action is not None
            and not self.blockers
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "ready": self.ready,
            "action_id": (
                None if self.action is None else self.action.action_id
            ),
            "selection_source": self.selection_source,
            "blockers": list(self.blockers),
            "canary": self.canary_decision.to_dict(),
            "snapshot": (
                None if self.snapshot is None else self.snapshot.to_dict()
            ),
        }


def _bundle_id(bundle: object) -> str | None:
    value = getattr(bundle, "bundle_id", None)
    return value if isinstance(value, str) and value else None


def _blocked(
    *,
    bundle: object,
    flow: str,
    stage: str,
    boundary_digest: str,
    blockers: Sequence[str],
) -> ExactBCCanaryDecision:
    return ExactBCCanaryDecision(
        status="blocked",
        flow=flow,
        stage=stage,
        boundary_digest=boundary_digest,
        bundle_id=_bundle_id(bundle),
        blockers=tuple(dict.fromkeys(value for value in blockers if value)),
    )


def _stage(evidence: object) -> str | None:
    state = getattr(evidence, "state", None)
    value = getattr(state, "step_type_value", None)
    return STAGE_BY_NATIVE_VALUE.get(value) if isinstance(value, int) else None


def _digest(evidence: object) -> str:
    digest = getattr(evidence, "digest", None)
    if not callable(digest):
        return "unidentified-boundary"
    try:
        value = digest()
    except (TypeError, ValueError, RuntimeError):
        return "unidentified-boundary"
    return value if isinstance(value, str) and value else "unidentified-boundary"


def _flow(snapshot: UnifiedLegalActionSnapshot | None) -> str:
    return "unknown|unknown|unknown" if snapshot is None else snapshot.flow_id


def _typed_state(evidence: object) -> Mapping[str, Any]:
    state = getattr(evidence, "state", None)
    to_dict = getattr(state, "to_dict", None)
    if not callable(to_dict):
        raise ValueError("settled evidence state has no typed serialization")
    value = to_dict()
    if not isinstance(value, Mapping):
        raise ValueError("settled evidence state serialization is invalid")
    return value


def _physical_identity(evidence: object) -> tuple[str, str] | None:
    """Read flow/stage identity without enumerating or scoring an action."""

    stage = _stage(evidence)
    if stage is None:
        return None
    try:
        native = project_exact_exam_native_state(_typed_state(evidence))
    except (TypeError, ValueError):
        return None
    produce_id = native.get("produceId")
    raw_plan = native.get("planType")
    raw_effect = native.get(
        "mainEffectType", native.get("displayMainEffectType")
    )
    plan = (
        PLAN_BY_NATIVE_VALUE.get(raw_plan)
        if isinstance(raw_plan, int) and not isinstance(raw_plan, bool)
        else raw_plan
    )
    effect = (
        EFFECT_BY_NATIVE_VALUE.get(raw_effect)
        if isinstance(raw_effect, int) and not isinstance(raw_effect, bool)
        else raw_effect
    )
    if not all(
        isinstance(value, str) and value
        for value in (produce_id, plan, effect)
    ):
        return None
    return f"{produce_id}|{plan}|{effect}", stage


def select_plan2_exact_bc_canary(
    *,
    evidence: object,
    orchestrator: Callable[[object], object],
    bundle: ExactBCPolicyBundle,
    policy: ExactBCCanaryPolicy = ExactBCCanaryPolicy(),
    offline_rl: OfflineRLSelector | None = None,
) -> Plan2ExactBCCanarySelection:
    """Bind one model choice to the exact Plan2 typed root action.

    The function is deliberately selection-only.  Production composition can
    pass ``selection.action`` to the existing Maa action executor; RL can use
    the same snapshot and action identity without creating another click
    path.
    """

    if not callable(orchestrator):
        raise TypeError("Plan2 canary orchestrator must be callable")
    if not isinstance(policy, ExactBCCanaryPolicy):
        raise TypeError("Plan2 canary policy must be ExactBCCanaryPolicy")
    stage = _stage(evidence) or "unknown"
    boundary_digest = _digest(evidence)

    try:
        planner_decision = orchestrator(evidence)
    except Exception as error:
        decision = _blocked(
            bundle=bundle,
            flow="unknown|unknown|unknown",
            stage=stage,
            boundary_digest=boundary_digest,
            blockers=(f"plan2-orchestrator-failed:{type(error).__name__}",),
        )
        return Plan2ExactBCCanarySelection(
            None, decision, None, None, None, None, decision.blockers
        )

    provider = getattr(orchestrator, "plan2_native_candidate_provider", None)
    if not callable(provider):
        decision = _blocked(
            bundle=bundle,
            flow="unknown|unknown|unknown",
            stage=stage,
            boundary_digest=boundary_digest,
            blockers=("plan2-native-candidate-provider-unbound",),
        )
        return Plan2ExactBCCanarySelection(
            planner_decision, decision, None, None, None, None, decision.blockers
        )
    try:
        actions = provider(planner_decision, evidence)
        typed_actions = tuple(actions) if actions is not None else ()
    except Exception as error:
        typed_actions = ()
        provider_error = f"plan2-native-candidates-failed:{type(error).__name__}"
    else:
        provider_error = ""
    if not typed_actions:
        decision = _blocked(
            bundle=bundle,
            flow="unknown|unknown|unknown",
            stage=stage,
            boundary_digest=boundary_digest,
            blockers=(provider_error or "plan2-native-candidates-unavailable",),
        )
        return Plan2ExactBCCanarySelection(
            planner_decision, decision, None, None, None, None, decision.blockers
        )
    if any(
        not isinstance(value, (Plan2NativeAction, Plan2NativeDrinkAction))
        for value in typed_actions
    ):
        decision = _blocked(
            bundle=bundle,
            flow="unknown|unknown|unknown",
            stage=stage,
            boundary_digest=boundary_digest,
            blockers=("plan2-native-candidate-type-invalid",),
        )
        return Plan2ExactBCCanarySelection(
            planner_decision, decision, None, None, None, None, decision.blockers
        )

    class _Enumeration:
        complete = True
        blockers: tuple[str, ...] = ()
        candidates = typed_actions

    try:
        snapshot = build_unified_legal_action_snapshot(
            evidence,
            native_enumerator=lambda _source: _Enumeration(),
        )
    except Exception as error:
        snapshot = None
        snapshot_error = f"unified-snapshot-failed:{type(error).__name__}"
    else:
        snapshot_error = ""
    if snapshot is None or not snapshot.complete:
        blockers = (
            (snapshot_error,)
            if snapshot is None
            else tuple(snapshot.blockers) or ("unified-snapshot-incomplete",)
        )
        decision = _blocked(
            bundle=bundle,
            flow=_flow(snapshot),
            stage=stage,
            boundary_digest=boundary_digest,
            blockers=blockers,
        )
        return Plan2ExactBCCanarySelection(
            planner_decision, decision, snapshot, None, None, None, decision.blockers
        )
    if stage == "unknown":
        decision = _blocked(
            bundle=bundle,
            flow=snapshot.flow_id,
            stage=stage,
            boundary_digest=snapshot.boundary_digest,
            blockers=("canonical-stage-unavailable",),
        )
        return Plan2ExactBCCanarySelection(
            planner_decision, decision, snapshot, None, None, None, decision.blockers
        )
    try:
        state_before = project_exact_exam_native_state(_typed_state(evidence))
    except Exception as error:
        decision = _blocked(
            bundle=bundle,
            flow=snapshot.flow_id,
            stage=stage,
            boundary_digest=snapshot.boundary_digest,
            blockers=(f"exact-state-projection-failed:{type(error).__name__}",),
        )
        return Plan2ExactBCCanarySelection(
            planner_decision, decision, snapshot, None, None, None, decision.blockers
        )
    selection_source: str | None = None
    try:
        exact_component = bundle.component("exact_exam_policy")
    except Exception:
        exact_component = {}
    router_contract_active = bool(
        isinstance(exact_component, Mapping)
        and exact_component.get("enabled") is True
        and exact_component.get("live_apply_allowed") is True
    )
    if offline_rl is None and not router_contract_active:
        # Preserve the original standalone/non-learned canary contract for
        # injected legacy bundles that predate explicit Router gates.
        decision = select_exact_bc_canary_action(
            bundle=bundle,
            state_before=state_before,
            snapshot=snapshot,
            stage=stage,
            policy=policy,
        )
        if decision.ready:
            selection_source = "behavior_cloning"
    else:
        # Every active PolicyBundle path, including BC-only composition, goes
        # through the one shared RL -> BC selector and therefore through the
        # Exam Policy Router.  There is no second formal BC selection path.
        learned = select_plan_neutral_learned_action(
            state_before=state_before,
            snapshot=snapshot,
            stage=stage,
            offline_rl=offline_rl,
            bc_bundle=bundle,
            bc_policy=policy,
        )
        if learned.ready and learned.action_id is not None:
            index = snapshot.action_ids.index(learned.action_id)
            decision = ExactBCCanaryDecision(
                status="ready",
                flow=snapshot.flow_id,
                stage=stage,
                boundary_digest=snapshot.boundary_digest,
                bundle_id=learned.policy_id,
                action_id=learned.action_id,
                candidate_index=index,
                candidate=learned.candidate,
                probability=learned.probability,
                margin=None,
            )
            selection_source = learned.source
        else:
            decision = _blocked(
                bundle=bundle,
                flow=snapshot.flow_id,
                stage=stage,
                boundary_digest=snapshot.boundary_digest,
                blockers=learned.blockers,
            )
    if not decision.ready or decision.candidate_index is None:
        return Plan2ExactBCCanarySelection(
            planner_decision,
            decision,
            snapshot,
            state_before,
            None,
            selection_source,
            decision.blockers,
        )
    index = decision.candidate_index
    if not 0 <= index < len(typed_actions):
        blocker = "bc-candidate-index-out-of-range"
    elif typed_actions[index].action_id != decision.action_id:
        blocker = "bc-typed-action-identity-mismatch"
    else:
        blocker = ""
    if blocker:
        blocked = _blocked(
            bundle=bundle,
            flow=snapshot.flow_id,
            stage=stage,
            boundary_digest=snapshot.boundary_digest,
            blockers=(blocker,),
        )
        return Plan2ExactBCCanarySelection(
            planner_decision,
            blocked,
            snapshot,
            state_before,
            None,
            selection_source,
            blocked.blockers,
        )
    return Plan2ExactBCCanarySelection(
        planner_decision,
        decision,
        snapshot,
        state_before,
        typed_actions[index],
        selection_source,
    )


def bind_plan2_exact_bc_canary_orchestrator(
    orchestrator: Callable[[object], object],
    *,
    bundle: ExactBCPolicyBundle,
    policy: ExactBCCanaryPolicy,
    offline_rl: OfflineRLSelector | None = None,
) -> Callable[[object], Plan2NativeExamSaveDecision]:
    """Replace only Plan2 action choice; keep native legality and transition.

    A blocked model produces an explicit no-decision.  The wrapper never
    falls through to the planner's action and never calls Maa's own strategy.
    """

    if not callable(orchestrator):
        raise TypeError("Plan2 base orchestrator must be callable")
    if not isinstance(policy, ExactBCCanaryPolicy):
        raise TypeError("Plan2 canary policy must be ExactBCCanaryPolicy")
    catalog_provider = getattr(
        orchestrator, "plan2_native_catalog_provider", None
    )
    transitioner = getattr(orchestrator, "plan2_native_transitioner", None)
    logical_base = getattr(orchestrator, "logical_horizon_orchestrator", None)
    boundary_provider = getattr(orchestrator, "plan2_native_boundary_provider", None)
    horizon_boundary_provider = getattr(
        orchestrator, "plan2_native_horizon_boundary_provider", None
    )
    if (
        not callable(catalog_provider)
        or not callable(transitioner)
        or not callable(logical_base)
        or not callable(boundary_provider)
        or not callable(horizon_boundary_provider)
    ):
        raise TypeError(
            "Plan2 orchestrator must expose logical/catalog/transition providers"
        )
    policy_source = "learned-chain:" + (_bundle_id(bundle) or "unknown-bundle")
    bound_identity: list[tuple[str, str]] = []

    def bind_retained_evidence_identity(evidence: object) -> bool:
        """Bind restart-safe flow/stage from current physical evidence only."""

        identity = _physical_identity(evidence)
        if identity is None:
            return False
        if not bound_identity:
            bound_identity.append(identity)
            return True
        return bound_identity[0] == identity

    def no_decision(
        base: Plan2NativeExamSaveDecision,
        *codes: str,
    ) -> Plan2NativeExamSaveDecision:
        detail = ",".join(dict.fromkeys(value for value in codes if value))
        return with_plan2_policy_action(
            base,
            source=policy_source,
            action=None,
            predicted_after_state=None,
            probability=None,
            blockers=(
                Plan2NativeBlocker(
                    "learned-policy-no-decision",
                    detail or "exact-bc-abstained",
                ),
            ),
        )

    def plan(evidence: object) -> Plan2NativeExamSaveDecision:
        identity_consistent = bind_retained_evidence_identity(evidence)
        try:
            boundary = boundary_provider(evidence)
            base = boundary.base_decision
        except Exception as error:
            raise RuntimeError(
                f"Plan2 learned boundary failed:{type(error).__name__}:{error}"
            ) from error
        if not isinstance(base, Plan2NativeExamSaveDecision):
            raise TypeError("Plan2 boundary provider returned an invalid decision")
        if not identity_consistent:
            return no_decision(base, "learned-physical-identity-unavailable")
        if base.terminal:
            return with_plan2_policy_action(
                base,
                source=policy_source,
                action=None,
                predicted_after_state=base.root_state,
            )
        root = getattr(boundary, "root", None)
        actions = tuple(getattr(boundary, "actions", ()))
        if root is None or not actions or getattr(boundary, "blockers", ()):
            return no_decision(base, "plan2-policy-root-unavailable")
        try:
            state_before = project_exact_exam_native_state(_typed_state(evidence))

            class _Enumeration:
                complete = True
                blockers: tuple[str, ...] = ()
                candidates = actions

            snapshot = build_unified_legal_action_snapshot(
                evidence,
                native_enumerator=lambda _source: _Enumeration(),
                source_label="plan2-learned-planner-free-boundary",
            )
            stage = _stage(evidence) or "unknown"
            learned = select_plan_neutral_learned_action(
                state_before=state_before,
                snapshot=snapshot,
                stage=stage,
                offline_rl=offline_rl,
                bc_bundle=bundle,
                bc_policy=policy,
            )
        except Exception as error:
            return no_decision(
                base,
                f"learned-selection-failed:{type(error).__name__}",
            )
        if not learned.ready or learned.action_id is None:
            return no_decision(base, *learned.blockers)
        try:
            index = snapshot.action_ids.index(learned.action_id)
        except ValueError:
            return no_decision(base, "learned-action-not-legal")
        action = actions[index]
        if action.action_id != learned.action_id:
            return no_decision(base, "learned-action-identity-mismatch")
        current_identity = (snapshot.flow_id, stage)
        if bound_identity and bound_identity[0] != current_identity:
            return no_decision(base, "learned-physical-identity-changed")
        try:
            transition = transitioner(root, action, boundary.catalog)
        except Exception as error:
            return no_decision(
                base,
                f"plan2-policy-transition-failed:{type(error).__name__}",
            )
        if not isinstance(transition, Plan2NativeTransition):
            return no_decision(base, "plan2-policy-transition-contract")
        if not transition.supported or transition.after is None:
            return no_decision(
                base,
                *(value.code for value in transition.blockers),
                "plan2-policy-transition-unsupported",
            )
        return with_plan2_policy_action(
            base,
            source=f"{learned.source}:{learned.policy_id or 'unknown-policy'}",
            action=action,
            predicted_after_state=transition.after,
            probability=learned.probability,
            legal_action_ids=snapshot.action_ids,
        )

    def plan_logical(
        root: Plan2NativeHorizonState,
    ) -> Plan2NativeExamSaveDecision:
        """Continue RL→BC choice from one replay-proven retained horizon."""

        if not isinstance(root, Plan2NativeHorizonState):
            raise TypeError("Plan2 retained logical root must be typed")
        boundary = horizon_boundary_provider(root)
        base = boundary.base_decision
        if not isinstance(base, Plan2NativeExamSaveDecision):
            raise TypeError("Plan2 logical orchestrator returned an invalid decision")
        if base.terminal:
            return with_plan2_policy_action(
                base,
                source=policy_source,
                action=None,
                predicted_after_state=base.root_state,
            )
        if not bound_identity:
            return no_decision(base, "learned-logical-flow-unbound")
        flow, stage = bound_identity[0]
        typed_actions = tuple(getattr(boundary, "actions", ()))
        if not typed_actions or any(
            not isinstance(value, (Plan2NativeAction, Plan2NativeDrinkAction))
            for value in typed_actions
        ):
            return no_decision(base, "plan2-native-candidates-unavailable")
        predictive_root = getattr(boundary, "root", None)
        if predictive_root is None:
            return no_decision(base, "plan2-policy-root-unavailable")
        try:
            state_before = project_plan2_horizon_native_state(
                predictive_root,
                flow=flow,
                stage=stage,
            )

            class _LogicalSource:
                state = root

                @staticmethod
                def digest() -> str:
                    return canonical_sha256(state_before)

            class _Enumeration:
                complete = True
                blockers: tuple[str, ...] = ()
                candidates = typed_actions

            snapshot = build_unified_legal_action_snapshot(
                _LogicalSource(),
                flow=flow,
                native_enumerator=lambda _source: _Enumeration(),
                source_label="retained-plan2-logical-horizon",
            )
            learned = select_plan_neutral_learned_action(
                state_before=state_before,
                snapshot=snapshot,
                stage=stage,
                offline_rl=offline_rl,
                bc_bundle=bundle,
                bc_policy=policy,
            )
        except Exception as error:
            return no_decision(
                base,
                "learned-logical-selection-failed:"
                f"{type(error).__name__}:{error}",
            )
        if not learned.ready or learned.action_id is None:
            return no_decision(base, *learned.blockers)
        try:
            index = snapshot.action_ids.index(learned.action_id)
        except ValueError:
            return no_decision(base, "learned-logical-action-not-legal")
        action = typed_actions[index]
        if action.action_id != learned.action_id:
            return no_decision(base, "learned-logical-action-identity-mismatch")
        try:
            catalog = catalog_provider()
            transition = transitioner(predictive_root, action, catalog)
        except Exception as error:
            return no_decision(
                base,
                f"plan2-policy-transition-failed:{type(error).__name__}",
            )
        if not isinstance(transition, Plan2NativeTransition):
            return no_decision(base, "plan2-policy-transition-contract")
        if not transition.supported or transition.after is None:
            return no_decision(
                base,
                *(value.code for value in transition.blockers),
                "plan2-policy-transition-unsupported",
            )
        return with_plan2_policy_action(
            base,
            source=f"{learned.source}:{learned.policy_id or 'unknown-policy'}",
            action=action,
            predicted_after_state=transition.after,
            probability=learned.probability,
            legal_action_ids=snapshot.action_ids,
        )

    # Keep the existing candidate collector and pure Master providers on the
    # learned wrapper.  No controller or dispatcher is added here.
    for name in (
        "plan2_native_candidate_provider",
        "plan2_native_catalog_provider",
        "plan2_native_transitioner",
        "plan2_native_boundary_provider",
        "plan2_native_horizon_boundary_provider",
        "nia_inner_transition_collector",
        "nia_legal_decision_collector",
        "nia_inner_transition_collector_error",
        "nia_inner_transition_candidate_provider",
        "nia_training_run_binding_id",
    ):
        if hasattr(orchestrator, name):
            setattr(plan, name, getattr(orchestrator, name))
    setattr(plan, "exact_bc_policy_source", policy_source)
    setattr(plan, "logical_horizon_orchestrator", plan_logical)
    setattr(
        plan,
        "bind_retained_evidence_identity",
        bind_retained_evidence_identity,
    )
    return plan


__all__ = [
    "Plan2ExactBCCanarySelection",
    "SCHEMA",
    "bind_plan2_exact_bc_canary_orchestrator",
    "select_plan2_exact_bc_canary",
]
