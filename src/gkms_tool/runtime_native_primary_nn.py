"""Plan-neutral NN inference over this DLL observation's primary inputs.

No card/drink effect compiler, horizon bootstrap, transition or input executor
is used. Secondary selections remain a later native boundary. Bundles/selectors
are supplied by the caller; this module never loads or changes an active model.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib

from .audition_local_save_state import AuditionLocalSaveStateEvidence, parse_local_save_exam_state
from .canonical_training_labels import EFFECT_BY_NATIVE_VALUE, KNOWN_READINESS_V5_FLOWS, PLAN_BY_NATIVE_VALUE, STAGE_BY_NATIVE_VALUE
from .exact_bc_live_canary import ExactBCCanaryPolicy
from .exact_exam_state_projection import project_exact_exam_native_state
from .learned_policy_selector import LearnedPolicyDecision, select_plan_neutral_learned_action
from .runtime_native_legal_inputs import NativePrimaryInput, RuntimeNativeLegalInputs, parse_runtime_native_legal_inputs
from .training_artifact_io import canonical_json_bytes
from .unified_legal_action_snapshot import UnifiedLegalActionSnapshot

SCHEMA = "gkms.runtime-native-primary-nn.v1"
AUTHORITY = "dll-native-primary-input-validator-v1"


@dataclass(frozen=True, slots=True)
class NativePrimaryNNScope:
    plan_type: str
    main_effect_types: tuple[str, ...]
    result_schema: str
    identity_blocker: str
    produce_ids: tuple[str, ...] | None = None
    native_plan_value: int | None = None
    native_main_effect_values: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeNativePrimaryAction:
    action_id: str
    native_input: NativePrimaryInput

    @property
    def kind(self):
        return self.native_input.kind

    @property
    def card_guid(self):
        return self.native_input.target.get("card_guid")

    @property
    def slot_index(self):
        return self.native_input.target.get("slot")

    @property
    def drink_id(self):
        return self.native_input.target.get("drink_id")

    @property
    def selected_card_guid(self):
        # Unknown is not an automatic/no-selector assertion.
        return None

    def to_dict(self):
        return {"action_id": self.action_id, **self.native_input.to_dict(),
            "card_guid": self.card_guid, "slot_index": self.slot_index, "drink_id": self.drink_id,
            "selected_card_guid": None}


@dataclass(frozen=True, slots=True)
class RuntimeNativePrimaryResult:
    native_pool: RuntimeNativeLegalInputs | None = None
    learned_snapshot: UnifiedLegalActionSnapshot | None = None
    decision: LearnedPolicyDecision | None = None
    action: RuntimeNativePrimaryAction | None = None
    flow: str | None = None
    stage: str | None = None
    boundary_digest: str | None = None
    blockers: tuple[str, ...] = ()
    schema: str = SCHEMA

    @property
    def ready(self):
        return self.action is not None and self.decision is not None and self.decision.ready and not self.blockers

    @property
    def best_action(self):
        return self.action

    @property
    def probability(self):
        return None if self.decision is None else self.decision.probability

    @property
    def source(self):
        return None if self.decision is None else self.decision.source

    def to_dict(self):
        # Do not serialize the legacy snapshot's generic "full_rl_policy_ready"
        # convenience flag as a promotion claim for this primary-only view.
        inference_view = None if self.learned_snapshot is None else {
            "authority": self.learned_snapshot.authority, "source": self.learned_snapshot.source,
            "flow": self.learned_snapshot.flow_id, "boundary_digest": self.learned_snapshot.boundary_digest,
            "primary_mask_complete": self.learned_snapshot.complete,
            "candidates": [dict(row) for row in self.learned_snapshot.candidates],
            "secondary_selection_complete": False, "training_promotion_allowed": False}
        return {"schema": self.schema, "ready": self.ready, "flow": self.flow, "stage": self.stage,
            "boundary_digest": self.boundary_digest, "source": self.source, "probability": self.probability,
            "action": None if self.action is None else self.action.to_dict(),
            "decision": None if self.decision is None else self.decision.to_dict(),
            "legal_primary_inputs": None if self.native_pool is None else self.native_pool.to_dict(),
            "learned_primary_view": inference_view,
            "blockers": list(self.blockers), "candidate_scope": "primary-inputs-only",
            "secondary_selection_complete": False, "forecast_available": False,
            "training_promotion_allowed": False, "simulator_compilation_used": False,
            "owns_input": False}


def select_runtime_native_primary_nn(observation, *, scope: NativePrimaryNNScope, bc_bundle=None, offline_rl=None,
                                       bc_policy: ExactBCCanaryPolicy | None = None,
                                       primary_selector=None) -> RuntimeNativePrimaryResult:
    """Select one real RL/BC primary action from a same-source RuntimeExamOutcome.

    The native snapshot, raw state, evidence and session generation must all be
    carried by the caller's one read_snapshot outcome. A partial *native*
    validation pool is preserved but not scored by the existing NN selector,
    whose probability contract requires a complete current candidate mask.
    Missing simulator semantics never enter this admission decision.
    """
    pool = learned = decision = None
    flow = stage = boundary = None

    def blocked(*reasons):
        return RuntimeNativePrimaryResult(pool, learned, decision, None, flow, stage, boundary, tuple(reasons), schema=scope.result_schema)

    try:
        native = getattr(observation, "native_snapshot", None)
        raw = getattr(observation, "raw_state", None)
        evidence = getattr(observation, "evidence", None)
        generation = getattr(observation, "native_session_generation", None)
        if not isinstance(native, Mapping) or not isinstance(raw, Mapping) or not isinstance(evidence, AuditionLocalSaveStateEvidence):
            return blocked("same-native-snapshot-raw-and-typed-evidence-required")
        pool = parse_runtime_native_legal_inputs(native, session_generation=generation)
        raw_bytes = canonical_json_bytes(raw)
        if (hashlib.sha256(raw_bytes).hexdigest() != pool.exam_save_sha256
                or evidence.source_sha256 != pool.exam_save_sha256 or evidence.source_size != len(raw_bytes)):
            return blocked("native-pool-raw-evidence-content-binding-mismatch")
        if evidence.session_transition_id != "native-sequence:" + pool.sequence_key:
            return blocked("native-pool-evidence-sequence-mismatch")
        expected_context = hashlib.sha256(canonical_json_bytes({
            "session_generation": pool.session_generation, "sequence_id": pool.sequence_key,
            "setting_id": evidence.state.setting_id, "character_id": evidence.state.character_id,
            "step_type": evidence.state.step_type_value,
        })).hexdigest()
        if evidence.step_context_digest != expected_context:
            return blocked("native-pool-evidence-session-context-mismatch")
        # Reuse the gateway's same structural parser solely for source
        # consistency. This performs no effect compilation or simulation.
        if parse_local_save_exam_state(raw) != evidence.state:
            return blocked("native-pool-raw-typed-state-mismatch")
        # Raw native fields are the model input. Typed evidence provides its
        # original frozen boundary identity, never a simulated replacement.
        if tuple(card.guid for card in evidence.state.zones.hand) != tuple(row["card_guid"] for row in pool.hand):
            return blocked("native-pool-typed-hand-binding-mismatch")
        opaque = evidence.state.root_runtime.opaque_fields.to_value() if evidence.state.root_runtime is not None else {}
        if opaque.get("drinkList") != raw.get("drinkList"):
            return blocked("native-pool-typed-drink-binding-mismatch")
        boundary = evidence.digest()
        state_before = project_exact_exam_native_state(raw)
        plan_value = state_before.get("planType")
        effect_value = state_before.get("mainEffectType", state_before.get("displayMainEffectType"))
        step = state_before.get("stepType")
        produce_id = state_before.get("produceId")
        plan = PLAN_BY_NATIVE_VALUE.get(plan_value) if type(plan_value) is int else plan_value
        effect = EFFECT_BY_NATIVE_VALUE.get(effect_value) if type(effect_value) is int else effect_value
        stage = STAGE_BY_NATIVE_VALUE.get(step) if type(step) is int else None
        if (not isinstance(produce_id, str) or not produce_id or plan != scope.plan_type
                or effect not in scope.main_effect_types
                or (scope.produce_ids is not None and produce_id not in scope.produce_ids)
                or (scope.native_plan_value is not None and (type(plan_value) is not int or plan_value != scope.native_plan_value))
                or (scope.native_main_effect_values is not None and (type(effect_value) is not int or effect_value not in scope.native_main_effect_values))
                or stage is None or type(state_before.get("examType")) is not int or state_before["examType"] != 1):
            return blocked(scope.identity_blocker)
        flow = "|".join((produce_id, plan, effect))
        if evidence.state.step_type_value != step:
            return blocked("native-typed-stage-mismatch")
        if not pool.boundary_ready:
            return blocked("native-primary-input-boundary-not-ready")
        if not pool.complete:
            return blocked("native-primary-validation-partial; NN-needs-complete-primary-mask")
        candidates, binding_order = [], []
        for value in pool.actions:
            target = value.target
            if value.kind == "play":
                slot = target["slot"]
                data = raw["handList"][slot].get("_cardData")
                if (not isinstance(data, Mapping) or not isinstance(data.get("_id"), str) or not data["_id"]
                        or type(data.get("_upgradeCount")) is not int or data["_upgradeCount"] < 0):
                    return blocked("native-model-card-identity-or-upgrade-unavailable")
                candidates.append({"kind": "play", "card_guid": target["card_guid"], "slot": slot,
                    "card_id": data["_id"], "upgrade": data["_upgradeCount"]})
            elif value.kind == "drink":
                # Slot+ID belongs to this native observation. No synthetic
                # LocalSave instance token or unobserved selected GUID is used.
                candidates.append({"kind": "drink", "slot_index": target["slot"], "drink_id": target["drink_id"]})
            else:
                candidates.append({"kind": "end_turn", "action_id": "END_TURN"})
            binding_order.append(value)
        # This is an inference-only adapter over a complete native primary
        # mask. It does not claim the old simulator enumerator's authority or
        # completeness of future secondary choices/training transitions.
        learned = UnifiedLegalActionSnapshot(flow=tuple(flow.split("|")), boundary_digest=boundary,
            candidates=tuple(candidates), authority=AUTHORITY, complete=True,
            source="dll-read_snapshot-native-primary-inputs; inference-only-secondary-unresolved")
        bindings = dict(zip(learned.action_ids, binding_order))
        policy = bc_policy if bc_policy is not None else ExactBCCanaryPolicy(enabled=True,
            allowed_flows=tuple(sorted(KNOWN_READINESS_V5_FLOWS)), minimum_probability=0.0, minimum_margin=0.0)
        if primary_selector is None:
            decision = select_plan_neutral_learned_action(state_before=state_before, snapshot=learned, stage=stage,
                offline_rl=offline_rl, bc_bundle=bc_bundle, bc_policy=policy)
        else:
            # Alternate model representations share the exact same observed
            # input admission and result binding; they do not own a dispatcher.
            decision = primary_selector(state_before=state_before, snapshot=learned, stage=stage)
        if not decision.ready or decision.action_id is None:
            return blocked(*(decision.blockers or ("native-primary-NN-no-decision",)))
        if (decision.boundary_digest != boundary or decision.flow != flow or decision.stage != stage
                or decision.action_id not in bindings or not isinstance(decision.candidate, Mapping)):
            return blocked("NN-decision-does-not-bind-current-native-primary-mask")
        chosen = learned.candidates[learned.action_ids.index(decision.action_id)]
        if dict(chosen) != dict(decision.candidate):
            return blocked("NN-candidate-identity-differs-from-native-primary-view")
        action = RuntimeNativePrimaryAction(decision.action_id, bindings[decision.action_id])
        return RuntimeNativePrimaryResult(pool, learned, decision, action, flow, stage, boundary, schema=scope.result_schema)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return blocked(f"native-primary-NN-boundary-unavailable:{type(error).__name__}:{error}")
