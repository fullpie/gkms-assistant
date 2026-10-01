"""Bind the game's Plan1 primary input pool to one hashed native observation."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from .audition_local_save_state import AuditionLocalSaveStateEvidence, parse_local_save_exam_state
from .nia_plan1_native_sidecar import Plan1NativeDrinkSlot, Plan1NativeLegalCandidate
from .runtime_native_legal_inputs import RuntimeNativeLegalInputs, parse_runtime_native_legal_inputs
from .training_artifact_io import canonical_json_bytes
from .unified_legal_action_snapshot import UnifiedLegalActionSnapshot

NATIVE_SELECTOR_POLICY = "native-card-choice-value-v1"


@dataclass(frozen=True)
class Plan1NativeLegalBinding:
    pool: RuntimeNativeLegalInputs
    snapshot: UnifiedLegalActionSnapshot

    def require_current(self, evidence):
        if evidence.digest() != self.snapshot.boundary_digest or evidence.source_sha256 != self.pool.exam_save_sha256:
            raise ValueError("plan1-native-pool-evidence-digest-mismatch")
        if evidence.session_transition_id != "native-sequence:" + self.pool.sequence_key:
            raise ValueError("plan1-native-pool-sequence-mismatch")
        state = evidence.state
        expected_context = hashlib.sha256(canonical_json_bytes({
            "session_generation": self.pool.session_generation, "sequence_id": self.pool.sequence_key,
            "setting_id": state.setting_id, "character_id": state.character_id, "step_type": state.step_type_value,
        })).hexdigest()
        if evidence.step_context_digest != expected_context:
            raise ValueError("plan1-native-pool-session-context-mismatch")

    def to_dict(self):
        return self.pool.to_dict()


def bind_plan1_native_legal_pool(evidence, native_snapshot, *, session_generation):
    """Never derive legality from a simulator, a Master effect list or inventory."""
    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("native primary pool requires native state evidence")
    if not evidence.session_transition_id.startswith("native-sequence:"):
        raise ValueError("plan1-native-pool-sequence-binding-unavailable")
    pool = parse_runtime_native_legal_inputs(native_snapshot, session_generation=session_generation,
        expected_sequence_id=evidence.session_transition_id.removeprefix("native-sequence:"))
    raw = native_snapshot["exam_save"]
    if pool.exam_save_sha256 != evidence.source_sha256 or parse_local_save_exam_state(raw) != evidence.state:
        raise ValueError("plan1-native-pool-does-not-match-observed-raw-state")
    if raw.get("planType") != 2 or raw.get("mainEffectType") not in {2, 10}:
        raise ValueError("plan1-native-pool-flow-unavailable")
    effect = {2: "ProduceExamEffectType_ExamParameterBuff", 10: "ProduceExamEffectType_ExamLessonBuff"}[raw["mainEffectType"]]
    candidates = []
    for action in pool.actions:
        if action.kind == "play":
            slot = action.target["slot"]
            card = evidence.state.zones.hand[slot]
            if card.guid != action.target["card_guid"]:
                raise ValueError("plan1-native-play-guid-binding-mismatch")
            candidates.append({"kind": "play", "action_id": f"PLAY:{card.guid}", "card_guid": card.guid,
                               "card_id": card.card_id, "hand_slot": slot, "upgrade": card.effective_upgrade})
        elif action.kind == "drink":
            slot = action.target["slot"]
            # Reuse the established canonical identity, including distinct
            # same-ID slots. Reading identity does not compile drink effects.
            observed = Plan1NativeDrinkSlot.from_value(raw["drinkList"][slot], slot)
            if observed.drink_id != action.target["drink_id"]:
                raise ValueError("plan1-native-drink-slot-binding-mismatch")
            candidates.append(Plan1NativeLegalCandidate.drink(observed).to_dict())
        elif action.kind == "end_turn":
            candidates.append({"kind": "end_turn", "action_id": "END_TURN"})
    blockers = () if pool.complete else ("native-primary-pool-incomplete", *(
        f"native-validation-unknown:{row['kind']}:{row['slot']}:{row['reason']}" for row in pool.unknown_checks))
    snapshot = UnifiedLegalActionSnapshot(flow=(raw["produceId"], "ProducePlanType_Plan1", effect),
        boundary_digest=evidence.digest(), candidates=tuple(candidates), complete=pool.complete, blockers=blockers,
        authority="game-native-primary-input-validators-v1", source="same-native-read_snapshot-primary-inputs")
    binding = Plan1NativeLegalBinding(pool, snapshot)
    binding.require_current(evidence)
    return binding
