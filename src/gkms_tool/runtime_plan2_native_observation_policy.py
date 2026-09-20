"""Native Plan2 NN first, then an optional drink value hint; no input owner."""
from __future__ import annotations

from collections.abc import Mapping

from .runtime_plan2_native_primary_policy import select_runtime_plan2_native_primary
from .runtime_native_observation_policy import (
    RULE_SOURCE, SECONDARY_POLICY, RuntimeNativeObservationDecision,
    RuntimeNativeObservationAction as RuntimePlan2ObservationAction,
)


class RuntimePlan2ObservationDecision(RuntimeNativeObservationDecision):
    __slots__ = ()
    metadata_schema = "gkms.runtime-plan2-native-observation-policy.v1"


class RuntimePlan2NativeObservationPolicy:
    def __init__(self, bc_bundle, *, offline_rl=None, bc_policy=None, drink_policy=None):
        self.bc_bundle, self.offline_rl, self.bc_policy = bc_bundle, offline_rl, bc_policy
        self.drink_policy = drink_policy
        self.last_drink_decision = None

    def __call__(self, evidence):
        """Explicit evidence-only offline/legacy API; live loop uses observation."""
        if not callable(self.drink_policy):
            raise TypeError("this policy requires a full native observation")
        result = self.drink_policy(evidence)
        self.last_drink_decision = self.drink_policy.last_drink_decision
        return result

    def __getattr__(self, name):
        if self.drink_policy is None:
            raise AttributeError(name)
        return getattr(self.drink_policy, name)

    def choose_observation(self, observed):
        self.last_drink_decision = None
        actor = select_runtime_plan2_native_primary(observed, bc_bundle=self.bc_bundle,
            offline_rl=self.offline_rl, bc_policy=self.bc_policy)
        if not actor.ready:
            return RuntimePlan2ObservationDecision(actor, None)
        action = RuntimePlan2ObservationAction(actor.action.action_id, actor.action.native_input,
            None if actor.action.kind == "end_turn" else SECONDARY_POLICY)
        if self.drink_policy is None or actor.action.kind != "play":
            return RuntimePlan2ObservationDecision(actor, action)
        applied = False
        try:
            comparison = self.drink_policy.evaluate(observed.evidence, actor.action, native_pool=actor.native_pool)
            if comparison is not None and not isinstance(comparison, Mapping):
                raise ValueError("drink hint is not a report")
            self.last_drink_decision = comparison
            candidate = None if comparison is None else comparison.get("candidate")
            if candidate is not None:
                scope = comparison.get("scope") or {}
                pool = actor.native_pool
                if (comparison.get("blockers") or comparison.get("boundary_digest") != actor.boundary_digest
                        or scope.get("projection_complete") is not True or scope.get("coverage_blockers")
                        or comparison.get("native_revision") != pool.revision
                        or comparison.get("native_sequence_id") != pool.sequence_key
                        or comparison.get("native_session_generation") != pool.session_generation):
                    raise ValueError("drink hint lacks matching native boundary and strict local mechanics")
                if (not isinstance(candidate, Mapping) or candidate.get("kind") != "drink"
                        or candidate.get("selected_card_guid")):
                    raise ValueError("primary drink hint cannot bind a simulated secondary selection")
                native = pool.lookup("drink", slot=candidate.get("slot_index"), drink_id=candidate.get("drink_id"))
                if native is None or comparison.get("native_candidate") != native.to_dict():
                    raise ValueError("drink hint differs from the current native primary target")
                # Remap the original NN primary ID by physical slot/ID. The
                # simulator's localsave-instance token never becomes input.
                matches = [row for row in actor.learned_snapshot.candidates if row["kind"] == "drink"
                    and row["slot_index"] == native.target["slot"] and row["drink_id"] == native.target["drink_id"]]
                if len(matches) != 1:
                    raise ValueError("drink hint has no unique NN primary identity")
                action = RuntimePlan2ObservationAction(matches[0]["action_id"], native, SECONDARY_POLICY)
                applied = True
        except Exception as error:
            # The value layer is optional. It may not erase a proven native NN
            # decision, alter its probability or establish another input owner.
            self.last_drink_decision = {"candidate": None,
                "blockers": [f"drink-hint-unavailable:{type(error).__name__}:{error}"]}
        return RuntimePlan2ObservationDecision(actor, action, self.last_drink_decision, applied)
