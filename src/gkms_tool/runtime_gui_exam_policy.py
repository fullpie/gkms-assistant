"""Explicit two-model DLL policy bridge over the existing native executor.

No game calls, automatic registry promotion, replay-owner impersonation, or
fallback to a different learned model. The host retains transaction ownership.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import shared_bc_features as shared
from .behavior_cloning import _outer_forward
from .gui_exam_models import get_gui_exam_policy
from .integrated_exam_bc_features import IntegratedExamFeatureEncoder
from .integrated_exam_bc_model import MAIN, SECONDARY, SecondaryConstraints, make_batch, forward
from .integrated_exam_bc_policy import load_integrated_exam_bc_model
from .master_db import get_idol_profile
from .native_primary_bc_training import load_native_primary_bc_model
from .observed_empty_collections import PC_SOURCE, _semantic_view
from .observed_empty_collections import NATIVE_METADATA_SHA256
from .runtime_live_exam_input import (
    RuntimeNativeSecondaryInput, VerifiedLiveMasterCatalog, prepare_live_primary, prepare_live_secondary,
    prepare_live_secondary_feature_input,
)
from .runtime_loader_compatibility import load_runtime_loader_compatibility
from .live_pc_consumer_contract import validate_live_pc_consumer_identity
from .runtime_master_source import load_runtime_master_source
from .runtime_master_features import RuntimeMasterFeatureEncoder
from .training_artifact_io import canonical_json_bytes, sha256_file

SECONDARY_POLICY_ID = "gui-exam-bc-v1"
LOADER_COMPATIBILITY_REFERENCE = {
    "path": str(Path(__file__).resolve().parents[2] / "var/research/post_update_live_20260919/runtime_loader_compat_v2/compatibility.json"),
    "sha256": "bc8b0260fa7bb0e2dd29ce7330ee630137e6ea93065093fe2523c80cad86d174"}
RUNTIME_MASTER_SOURCE_REFERENCE = {
    "path": str(Path(__file__).resolve().parents[2] / "var/research/public_delivery_20260920/master_12b_current_audit_v1/inference_source_receipt.json"),
    "sha256": "d11c0f8c2316e697f40c6a6c82db0bfcc87d1b26b1fe6a1d1c3a22c9dbab81f1"}


@dataclass(frozen=True)
class RuntimeGuiExamAction:
    kind: str
    slot_index: int | None = None
    card_guid: str | None = None
    drink_id: str | None = None
    selected_card_guid: str | None = None
    secondary_policy: str = SECONDARY_POLICY_ID


@dataclass(frozen=True)
class RuntimeGuiExamBlocker:
    code: str
    detail: str


@dataclass(frozen=True)
class RuntimeGuiExamDecision:
    best_action: RuntimeGuiExamAction | None
    blockers: tuple[RuntimeGuiExamBlocker, ...]
    policy_source: Mapping
    policy_legal_action_ids: tuple[str, ...] = ()
    metadata: Mapping | None = None


def _scope(produce_id, idol_card_id, variant_id):
    from .local_flow_trial import resolve_live_flow_scope
    idol = get_idol_profile(idol_card_id)
    if idol is None:
        raise ValueError("Selected idol profile is unavailable")
    scope = resolve_live_flow_scope(variant_id=variant_id, produce_id=produce_id, idol_card_id=idol_card_id,
        plan_type=idol.plan_type, exam_effect_type=idol.exam_effect_type)
    return idol, scope


def preflight_live_exam_policy(variant_id, *, produce_id, idol_card_id, native_capabilities, execution_master=None):
    """Read-only start check; absent current Master evidence stays explicit."""
    blockers = []
    descriptor = get_gui_exam_policy(variant_id, refresh=True)
    try:
        _scope(produce_id, idol_card_id, variant_id)
        if descriptor.get("artifact_available") is not True:
            raise ValueError(descriptor.get("diagnostic") or descriptor.get("reason") or "Model artifact unavailable")
        required = {"exam.model_observation.v1", "exam.continuation"}
        if not required.issubset(set(native_capabilities or ())):
            raise ValueError("Current DLL lacks complete model observation/continuation capabilities")
        if execution_master is None:
            raise ValueError("Current native MasterManager observation is not yet available")
        from .portable_model_assets import load_portable_model_runtime
        portable = load_portable_model_runtime(variant_id)
        catalog = portable.catalog if portable is not None else VerifiedLiveMasterCatalog(descriptor["specification"]["contract_set"])
        binding = catalog.resolve(execution_master, produce_id)
    except (OSError, ValueError, TypeError, KeyError) as error:
        blockers.append(str(error)); binding = None
    return {"variant_id": variant_id, "model_sha256": descriptor.get("model_sha256"),
        "artifact_available": descriptor.get("artifact_available", False),
        "current_master_bound": binding is not None, "ready": not blockers, "blockers": blockers,
        "live_workflow_verified": False, "input_submitted": False,
        "master_binding": None if binding is None else binding["provenance"]}


class RuntimeGuiExamPolicy:
    def __init__(self, variant_id, *, produce_id, idol_card_id, run_id):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("A stable cultivation run ID is required for an explicit live model")
        self.idol, self.flow_scope = _scope(produce_id, idol_card_id, variant_id)
        self.produce_id, self.idol_card_id, self.run_id = produce_id, idol_card_id, run_id
        self.variant_id = variant_id
        descriptor = get_gui_exam_policy(variant_id, refresh=True)
        if descriptor.get("artifact_available") is not True:
            raise ValueError(descriptor.get("diagnostic") or descriptor.get("reason") or "Model artifact unavailable")
        from .portable_model_assets import load_portable_model_runtime
        portable = load_portable_model_runtime(variant_id)
        self.portable_manifest_reference = None
        original_model_sha256 = descriptor["model_sha256"]
        loader_receipt_sha256 = LOADER_COMPATIBILITY_REFERENCE["sha256"]
        if portable is not None:
            for name in ("loader_compatibility", "source_resolver", "runtime_source", "encoder",
                         "runtime_model_compatibility", "catalog", "model_reference", "parameters", "metadata"):
                setattr(self, name, getattr(portable, name))
            self.portable_manifest_reference = deepcopy(portable.manifest_reference)
            original_model_sha256 = portable.original_model_sha256
            loader_receipt_sha256 = portable.runtime_model_compatibility["source_loader_receipt_sha256"]
        else:
            specification = descriptor["specification"]
            self.loader_compatibility = load_runtime_loader_compatibility(LOADER_COMPATIBILITY_REFERENCE)
            self.source_resolver = self.loader_compatibility.make_source_resolver()
            self.runtime_source = load_runtime_master_source(RUNTIME_MASTER_SOURCE_REFERENCE)
            self.encoder = RuntimeMasterFeatureEncoder(specification["contract_set"], specification["original_shared_encoder"],
                runtime_source=self.runtime_source,
                source_resolver=self.source_resolver, loader_compatibility=self.loader_compatibility)
            self.runtime_model_compatibility = self.loader_compatibility.validate_runtime_contract(self.encoder.contract)
            self.catalog = VerifiedLiveMasterCatalog(specification["contract_set"], runtime_encoder=self.encoder)
            self.model_reference = deepcopy(specification["model"])
            if variant_id == "baseline":
                self.parameters, self.metadata = load_native_primary_bc_model(self.model_reference,
                    expected_contract_set_sha256=self.encoder.contract_set["contract_set_sha256"])
                if canonical_json_bytes(self.metadata["contract_set"]) != canonical_json_bytes(self.encoder.contract_set):
                    raise ValueError("Primary model and live shared encoder contract set differ")
            elif variant_id == "integrated":
                self.parameters, self.metadata = load_integrated_exam_bc_model(self.model_reference,
                    expected_feature_contract=self.loader_compatibility.trained_feature_contract)
            else:
                raise ValueError("Explicit baseline or integrated model required")
        if sha256_file(Path(self.model_reference["path"])) != self.model_reference["sha256"]:
            raise ValueError("Model changed while loading the live policy")
        self._model_stat = self._stat()
        from . import live_feature_presence
        self._feature_view_path = Path(live_feature_presence.__file__).resolve()
        self._feature_view_reference = {"schema": live_feature_presence.SCHEMA,
            "path": str(self._feature_view_path), "sha256": sha256_file(self._feature_view_path)}
        view_stat = self._feature_view_path.stat()
        self._feature_view_stat = view_stat.st_size, view_stat.st_mtime_ns, view_stat.st_ctime_ns
        from . import live_phase_counter_view
        phase_path = Path(live_phase_counter_view.__file__).resolve()
        phase_stat = phase_path.stat()
        self._feature_view_dependencies = ((phase_path, (phase_stat.st_size, phase_stat.st_mtime_ns, phase_stat.st_ctime_ns)),)
        self._feature_view_reference["dependencies"] = [{"schema": live_phase_counter_view.SCHEMA,
            "path": str(phase_path), "sha256": sha256_file(phase_path)}]
        self._session_generation = None
        self._observed_master_identity = None
        # Stable model/run identity is persisted with pending transactions.
        # Dynamic source/state details belong to each decision, never this ID.
        self._binding = {"loaded": True, "variant_id": variant_id, "label": descriptor["label"],
            "model_sha256": original_model_sha256, "run_id": run_id,
            "artifact_sha256": self.model_reference["sha256"],
            "portable_manifest_sha256": None if self.portable_manifest_reference is None else self.portable_manifest_reference["sha256"],
            "flow_scope": self.flow_scope.binding,
            "runtime_loader_compatibility_sha256": loader_receipt_sha256,
            "live_feature_view": deepcopy(self._feature_view_reference),
            "runtime_master_package": self.encoder.runtime_master_binding(
                self.runtime_source.execution_master_version, produce_id)["runtime_package_binding"]}
        self.last_secondary_decision = None

    @property
    def runtime_policy_binding(self):
        return deepcopy(self._binding)

    def preflight_model_context(self, payload):
        """Bind the current home-screen Master before AP or cultivation input."""
        if not isinstance(payload, Mapping) or payload.get("schema") != "gkms.live-exam-model-preflight.v1":
            raise ValueError("Actual current DLL model preflight observation required")
        engine = payload.get("engine_identity", {})
        consumer = validate_live_pc_consumer_identity(engine)
        binding = self.validate_source_execution_master(payload.get("execution_master"))
        if self._stat() != self._model_stat:
            raise ValueError("Model changed during before-AP preflight")
        return {"ready": True, "runtime_policy_binding": self.runtime_policy_binding,
            "pc_consumer_compatibility": consumer,
            "master_binding": binding["provenance"], "source_master_hash": binding["source_master_hash"],
            "model_contract_sha256": binding["contract"]["contract_sha256"],
            "input_submitted": False, "live_workflow_verified": False}

    def _stat(self):
        stat = Path(self.model_reference["path"]).stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def validate_source_execution_master(self, master):
        """Bind actual observed or preserved-pending Master before any input."""
        self.flow_scope.validate_unchanged()
        self.loader_compatibility.validate_unchanged()
        self.source_resolver.validate_unchanged()
        view_stat = self._feature_view_path.stat()
        if (view_stat.st_size, view_stat.st_mtime_ns, view_stat.st_ctime_ns) != self._feature_view_stat:
            raise ValueError("Live raw-to-feature interpretation changed during this policy run")
        for dependency_path, dependency_stamp in self._feature_view_dependencies:
            dependency_stat = dependency_path.stat()
            if (dependency_stat.st_size, dependency_stat.st_mtime_ns, dependency_stat.st_ctime_ns) != dependency_stamp:
                raise ValueError("Live phase-counter interpretation changed during this policy run")
        bound = self.catalog.resolve(master, self.produce_id)
        actual_package = bound.get("runtime_package_binding")
        if actual_package is not None and actual_package != self._binding["runtime_master_package"]:
            raise ValueError("Actual runtime Master package differs from the policy's pinned source package")
        identity = master["execution_master_version"], master.get("execution_master_hash")
        if self._observed_master_identity is not None and identity != self._observed_master_identity:
            raise ValueError("Actual Master changed after this policy's observed source binding")
        self._observed_master_identity = identity
        return bound

    def _validate(self, prepared):
        self.loader_compatibility.validate_unchanged()
        self.source_resolver.validate_unchanged()
        self.catalog.validate_unchanged()
        if self._stat() != self._model_stat:
            raise ValueError("Loaded model artifact changed during this run")
        if prepared.state_before["produceId"] != self.produce_id:
            raise ValueError("Live model mode differs from the explicitly selected cultivation mode")
        if prepared.state_before.get("characterId") != self.idol.character_id:
            raise ValueError("Original exam character differs from the selected cultivation idol")
        expected_flow = "|".join((self.produce_id, self.idol.plan_type, self.idol.exam_effect_type))
        if prepared.observation.get("flow") != expected_flow:
            raise ValueError("Actual native flow differs from the selected model run scope")
        generation = prepared.observation["session_generation"]
        if self._session_generation is not None and generation != self._session_generation:
            raise ValueError("DLL session changed; rebuild the explicit model owner at a safe boundary")
        bound = self.validate_source_execution_master(prepared.observation["execution_master"])
        self._session_generation = generation
        return bound

    def _provenance(self, decision_type, binding=None):
        return {"kind": "primary-BC+legacy-secondary" if self.variant_id == "baseline" else "integrated-exam-BC",
            **self.runtime_policy_binding, "model": deepcopy(self.model_reference), "decision_type": decision_type,
            "shared_feature_contract_sha256": self.encoder.contract["contract_sha256"],
            "trained_feature_contract_sha256": self.loader_compatibility.trained_feature_contract["contract_sha256"],
            "runtime_model_compatibility": deepcopy(self.runtime_model_compatibility),
            "source_kind": "current-DLL", "teacher_action_used": False, "automatic_formal_model_activation": False,
            "legacy_rule_fallback": self.variant_id == "baseline" and decision_type == "secondary",
            "master_binding": None if binding is None else binding["provenance"],
            "actual_source_master_hash": None if binding is None else binding["source_master_hash"],
            "actual_runtime_package_binding": None if binding is None else deepcopy(binding.get("runtime_package_binding")),
            "score_prediction_available": False, "live_workflow_verified": False}

    def _primary_features(self, prepared, bound):
        from .live_feature_presence import prepare_live_feature_view
        features, contract = self.encoder._route(bound["source_master_hash"], self.produce_id)
        empty = contract["observed_empty_collection_contract"]
        projection = prepare_live_feature_view(prepared.state_before, evidence=prepared.feature_evidence, empty_contract=empty)
        value = shared.encode_primary_feature_view(raw_state=prepared.state_before, state_before=projection.state,
            legal_candidates=prepared.legal_candidates, flow=prepared.observation["flow"], stage=prepared.observation["stage"],
            native_mask_complete=True, candidate_scope=shared.PRIMARY_SCOPE, features=features, contract=contract)
        gaps = [*value.coverage["contract_gaps"], *value.coverage["semantic_gap_tokens"]]
        if not value.coverage["representation_complete"] or gaps:
            raise ValueError("Required primary live representation gaps: " + ";".join(gaps[:12]))
        context, candidates = value.pointer_indices(context_buckets=self.encoder.layout["context_buckets"],
            candidate_buckets=self.encoder.layout["candidate_buckets"])
        return make_batch([context], [candidates], [MAIN]), {"feature_sha256": value.feature_sha256,
            "raw_state_sha256": value.state_sha256, "candidate_bindings": list(value.candidate_bindings),
            "presence_view_ledger": projection.ledger, "feature_view_provenance": projection.provenance,
            "original_primary_contract_sha256": contract["contract_sha256"]}

    def _probabilities(self, batch):
        if self.variant_id == "integrated":
            return forward(self.parameters, batch).probabilities[0]
        if batch.decision_types.tolist() != [MAIN]:
            raise ValueError("Primary-only model cannot score a secondary decision")
        probabilities, _ = _outer_forward(self.parameters, batch.context_indices, batch.context_mask,
            batch.candidate_indices, batch.candidate_token_mask, batch.candidate_mask, cache=False)
        return probabilities[0]

    def choose_observation(self, observed):
        try:
            prepared = prepare_live_primary(observed, flow_scope=self.flow_scope)
            actual_idol = (observed.native_context or {}).get("idol_card_id")
            if actual_idol != self.idol_card_id or (observed.native_context or {}).get("exam_config_bound") is not True:
                raise ValueError("Current native cultivation idol differs from the selected model run")
            bound = self._validate(prepared)
            batch, detail = self._primary_features(prepared, bound)
            probabilities = self._probabilities(batch)
            index = int(np.argmax(probabilities))
            selected = prepared.original_actions[index]
            target = selected["target"]
            action = RuntimeGuiExamAction(selected["kind"], target.get("slot"), target.get("card_guid"), target.get("drink_id"))
            self._validate(prepared)
            return RuntimeGuiExamDecision(action, (), self._provenance("main", bound),
                tuple(row["command"] + ":" + str(row["target"].get("slot", "")) for row in prepared.original_actions),
                {**detail, "selected_candidate": index, "probabilities": probabilities.tolist(),
                    "observation": prepared.observation, "new_observation_training_qualified": False})
        except (OSError, ValueError, TypeError, KeyError) as error:
            return RuntimeGuiExamDecision(None, (RuntimeGuiExamBlocker("live-model-input-unqualified", str(error)),),
                self._provenance("main"), metadata={"input_submitted": False, "fallback_used": False})

    def choose_secondary(self, snapshot, *, session_generation, pending):
        try:
            if pending.get("model_policy_binding") != self.runtime_policy_binding:
                raise ValueError("Pending transaction model binding differs from the loaded model")
            if not isinstance(pending.get("source_execution_master"), Mapping):
                raise ValueError("Pending model transaction lacks its actual original Master observation")
            self.validate_source_execution_master(pending["source_execution_master"])
            prepared = prepare_live_secondary(snapshot, session_generation=session_generation, pending=pending,
                flow_scope=self.flow_scope)
            bound = self._validate(prepared)
            if prepared.gaps:
                raise ValueError("Required secondary live input gaps: " + ";".join(g.code + ":" + g.path for g in prepared.gaps))
            if self.variant_id == "baseline":
                from .runtime_exam_continuation_policy import choose_native_exam_continuation
                target, reason = choose_native_exam_continuation(snapshot,
                    parent_card_id=pending.get("source_card_id"), parent_card_upgrade=pending.get("source_card_upgrade"),
                    parent_drink_id=pending.get("drink_id"), database=Path(bound["source_database"]["path"]))
                detail = {**reason, "policy_source": self._provenance("secondary", bound),
                    "trained_secondary_model_used": False, "baseline_legacy_rule_explicit": True}
            else:
                target, detail = self._integrated_secondary(snapshot, prepared, bound)
            self._validate(prepared)
            self.last_secondary_decision = deepcopy(detail)
            return target, detail
        except (OSError, ValueError, TypeError, KeyError) as error:
            detail = {"status": "unavailable", "reason": str(error), "policy_source": self._provenance("secondary"),
                "input_submitted": False, "fallback_used": False}
            self.last_secondary_decision = deepcopy(detail)
            return None, detail

    def _integrated_secondary(self, snapshot, prepared, bound):
        from .live_feature_presence import prepare_live_feature_view
        _, contract = self.encoder._route(bound["source_master_hash"], self.produce_id)
        projection = prepare_live_feature_view(prepared.state_before, evidence=prepared.feature_evidence,
            current_context=prepared.current_context, empty_contract=contract["observed_empty_collection_contract"])
        prepared = prepare_live_secondary_feature_input(prepared, projection)
        constraints = SecondaryConstraints.from_native_input(prepared)
        prefix = prepared.selected_ordinals
        constraints.validate_prefix(prefix)
        metadata = {"source_kind": "current-DLL", "run_id": self.run_id,
            "source_decision_id": "live-selector:" + str(prepared.observation["pending_request_id"]),
            "observation": prepared.observation, "original_raw_state_sha256": projection.provenance["raw_state_sha256"],
            "feature_view_provenance": projection.provenance, "presence_view_ledger": projection.ledger}
        encoded = self.encoder._secondary(prepared, bound["source_master_hash"], metadata, prefix, "derived-policy-decoder-prefix")
        if encoded.bypass_response is not None:
            ordinal, probabilities = constraints.offered_count, []
        else:
            probabilities = self._probabilities(encoded.to_pointer_batch()).tolist()
            ordinal = encoded.candidate_ordinals[int(np.argmax(probabilities))]
        stop = ordinal == constraints.offered_count
        names = ("card_choice.confirm",) if stop else ("card_choice.select", "card_choice.reveal")
        index = None if stop else prepared.ui_indices[ordinal]
        target = _actual_secondary_target(snapshot, names, index, prepared)
        return target, {"status": "ready" if target is not None else "waiting", "reason":
            "integrated BC confirms the native ordered selection" if stop else "integrated BC selects the next native offered ordinal",
            "policy_source": self._provenance("secondary", bound), "trained_secondary_model_used": not constraints.complete(prefix),
            "selected_native_ordinal": None if stop else ordinal, "selected_ui_index": index, "stop_selected": stop,
            "ordered_prefix": list(prefix), "candidate_ordinals": list(encoded.candidate_ordinals), "probabilities": probabilities,
            "feature_metadata": dict(encoded.metadata), "legacy_rule_fallback": False}


def _actual_secondary_target(snapshot, names, index, prepared):
    """Return only a unique current DLL target; never synthesize UI commands."""
    actions, ui = snapshot.get("legal_actions"), snapshot.get("ui_state", {})
    if not isinstance(actions, list):
        raise ValueError("Current native secondary legal targets are missing")
    for name in names:
        matches = [a for a in actions if a.get("action_id") == name
            and (index is None or a.get("target", {}).get("index") == index)]
        if len(matches) > 1:
            raise ValueError("Ambiguous current native secondary targets")
        if not matches:
            continue
        target = matches[0].get("target", {})
        if (target.get("action_id") != name or target.get("exam_continuation") is not True
                or target.get("parent_context") != snapshot.get("parent_context")):
            raise ValueError("Current secondary target does not bind its owned selector")
        if index is None:
            if (ui.get("valid_count") is not True or ui.get("confirm_enabled") is not True
                    or target.get("selected_count") != len(prepared.selected_ordinals)):
                raise ValueError("Native confirmation does not match the observed selection")
        else:
            rows = [r for r in ui.get("candidates", []) if r.get("index") == index]
            if len(rows) != 1 or rows[0].get("selected") is not False or rows[0].get("restricted") is not False:
                raise ValueError("Native offered choice is selected, restricted or unbound")
            for key in ("card_id", "upgrade", "card_guid", "instance_key"):
                if key in target and target[key] != rows[0].get(key):
                    raise ValueError("Current secondary target card identity differs")
            if target.get("selected_before") is not False:
                raise ValueError("Current secondary target has stale selection state")
        return deepcopy(target)
    return None


def build_live_exam_policy(variant_id, *, produce_id, idol_card_id, run_id):
    return RuntimeGuiExamPolicy(variant_id, produce_id=produce_id, idol_card_id=idol_card_id, run_id=run_id)


__all__ = ["build_live_exam_policy", "preflight_live_exam_policy", "RuntimeGuiExamPolicy",
    "RuntimeGuiExamAction", "RuntimeGuiExamDecision", "SECONDARY_POLICY_ID"]
