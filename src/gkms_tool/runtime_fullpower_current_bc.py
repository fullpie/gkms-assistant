"""Explicit experimental FullPower teacher BC on the shared native input loop."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Mapping

from .fullpower_current_state_bc import MODEL_SCHEMA, encoder_hashes, score_encoded_candidates
from .fullpower_decision_features import FEATURE_SCHEMA, encode_native_decision
from .fullpower_decision_observation import OBSERVATION_SCHEMA
from .learned_policy_selector import LearnedPolicyDecision
from .runtime_native_primary_nn import NativePrimaryNNScope, select_runtime_native_primary_nn
from .runtime_native_observation_policy import RuntimeNativeObservationAction, RuntimeNativeObservationDecision, SECONDARY_POLICY
from .training_artifact_io import sha256_file

TRIAL_SCHEMA = "gkms.fullpower-current-bc-trial.v1"


@dataclass(frozen=True, slots=True)
class RuntimeFullPowerCurrentDecision(RuntimeNativeObservationDecision):
    artifact_sources: Mapping | None = None
    feature_report: Mapping | None = None
    metadata_schema: ClassVar[str] = "gkms.runtime-fullpower-current-bc-decision.v1"

    @property
    def metadata(self):
        return {**RuntimeNativeObservationDecision.metadata.fget(self),
            "actual_model_artifact": None if self.artifact_sources is None else dict(self.artifact_sources),
            "current_features": self.feature_report, "secondary_policy": SECONDARY_POLICY}


def _path(root, value):
    value = Path(value)
    return value.resolve() if value.is_absolute() else (root / value).resolve()


class RuntimeFullPowerCurrentBCPolicy:
    """No implicit old model fallback, RL alias, game read or game input."""

    def __init__(self, declaration, *, project_root, produce_id, idol_card_id):
        from .master_db import DEFAULT_DATABASE
        from .passive_catalog import DEFAULT_MASTER_DIR
        import numpy as np
        if (declaration.get("schema") != TRIAL_SCHEMA or declaration.get("enabled") is not True
                or declaration.get("experimental") is not True or declaration.get("production_validated") is not False
                or declaration.get("supervision_authority") != "simulator-teacher"
                or declaration.get("feature_schema") != FEATURE_SCHEMA or declaration.get("observation_schema") != OBSERVATION_SCHEMA):
            raise ValueError("FullPower current-state policy needs an explicit experimental teacher declaration")
        root = Path(project_root).resolve()
        self.model = _path(root, declaration["model_path"])
        report_path = _path(root, declaration["report_path"])
        if (sha256_file(self.model) != declaration["model_sha256"]
                or sha256_file(report_path) != declaration["report_sha256"]):
            raise ValueError("FullPower trial artifact hash mismatch")
        report = json.loads(report_path.read_text(encoding="utf8"))
        with np.load(self.model, allow_pickle=False) as archive:
            metadata = json.loads(str(archive["metadata_json"].item()))
        if (report.get("schema") != "gkms.fullpower-current-bc-report.v1" or report.get("metadata") != metadata
                or report.get("model_sha256") != declaration["model_sha256"] or metadata.get("schema") != MODEL_SCHEMA
                or metadata.get("kind") != "fullpower-current-candidate-pointer"
                or metadata.get("feature_schema") != FEATURE_SCHEMA or metadata.get("observation_schema") != OBSERVATION_SCHEMA
                or metadata.get("encoder_sha256") != encoder_hashes()):
            raise ValueError("FullPower trial report/model/feature implementation mismatch")
        if produce_id not in declaration.get("produce_ids", []):
            raise ValueError("FullPower trial does not include this mode")
        self.stages = tuple(declaration.get("trial_stages", ()))
        if not self.stages or any(stage not in {"Mid1", "Mid2", "Final"} for stage in self.stages):
            raise ValueError("FullPower trial needs explicit audition-stage scope")
        self.policy_id = declaration["policy_id"]
        if not isinstance(self.policy_id, str) or not self.policy_id:
            raise ValueError("FullPower trial policy ID is missing")
        self.artifact_sources = {"role": "fullpower_current_state_trial", "policy_id": self.policy_id,
            "model_kind": metadata["kind"], "model_path": str(self.model), "model_sha256": declaration["model_sha256"],
            "report_path": str(report_path), "report_sha256": declaration["report_sha256"],
            "feature_schema": FEATURE_SCHEMA, "observation_schema": OBSERVATION_SCHEMA,
            "supervision_authority": "simulator-teacher", "experimental": True, "production_validated": False,
            "prepared_manifest_sha256": metadata.get("prepared_manifest_sha256")}
        self.model_route = {"status": "native-current-bc-experimental", "available": True,
            "learned_model_used": True, "allow_offline_rl": False, "experimental": True,
            "production_validated": False, "live_pass_verified": False, "predicts_exam_score": False,
            "trial_stages": list(self.stages), "artifact_sources": dict(self.artifact_sources)}
        self.produce_id, self.idol_card_id = produce_id, idol_card_id
        self.database = _path(root, declaration.get("database", str(DEFAULT_DATABASE)))
        self.master_dir = _path(root, declaration.get("master_dir", str(DEFAULT_MASTER_DIR)))
        self.scope = NativePrimaryNNScope("ProducePlanType_Plan3", ("ProduceExamEffectType_ExamFullPower",),
            "gkms.fullpower-current-native-primary.v1", "FullPower-current-policy-mode-or-flow-mismatch",
            (produce_id,), native_plan_value=4, native_main_effect_values=(47,))
        self.last_features = None

    def _select(self, *, state_before, snapshot, stage):
        if sha256_file(self.model) != self.artifact_sources["model_sha256"]:
            raise ValueError("FullPower trial model changed during this run")
        if state_before.get("idolCardId") != self.idol_card_id or stage not in self.stages:
            raise ValueError("FullPower trial idol or stage differs from the observed game")
        scope = {"produce_id": self.produce_id, "plan_type": "ProducePlanType_Plan3",
            "exam_effect_type": "ProduceExamEffectType_ExamFullPower", "native_step_value": state_before["stepType"],
            "step_type": {"Mid1": "ProduceStepType_AuditionMid1", "Mid2": "ProduceStepType_AuditionMid2",
                          "Final": "ProduceStepType_AuditionFinal"}[stage],
            "idol_card_id": self.idol_card_id, "character_id": state_before["characterId"],
            "is_battle": True, "flow": snapshot.flow_id}
        encoded = encode_native_decision(state_before, decision_scope=scope, legal_candidates=snapshot.candidates,
                                        database=self.database, master_dir=self.master_dir)
        probabilities = score_encoded_candidates(self.model, encoded)
        index = max(range(len(probabilities)), key=lambda i: (probabilities[i], -i))
        self.last_features = {"feature_schema": encoded.feature_schema, "observation_schema": encoded.observation_schema,
            "missing_fields": list(encoded.missing_fields), "gaps": list(encoded.gaps),
            "probabilities": list(probabilities), "candidate_keys": list(encoded.candidate_keys),
            "training_authority": "simulator-teacher", "experimental": True, "predicts_exam_score": False}
        return LearnedPolicyDecision(status="ready", flow=snapshot.flow_id, stage=stage,
            boundary_digest=snapshot.boundary_digest, action_id=snapshot.action_ids[index],
            source="behavior_cloning", probability=probabilities[index], candidate=snapshot.candidates[index],
            policy_id=self.policy_id)

    def choose_observation(self, observed):
        self.last_features = None
        actor = select_runtime_native_primary_nn(observed, scope=self.scope, primary_selector=self._select)
        action = None if not actor.ready else RuntimeNativeObservationAction(actor.action.action_id,
            actor.action.native_input, None if actor.action.kind == "end_turn" else SECONDARY_POLICY)
        return RuntimeFullPowerCurrentDecision(actor, action,
            artifact_sources=self.artifact_sources, feature_report=self.last_features)
