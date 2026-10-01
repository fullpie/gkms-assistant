"""Shared actor callback for the existing isolated PC policy executor.

This adapter performs CPU inference only. The existing policy controller owns
native input, completion and cleanup. It does not register a GUI/live policy,
restore a trainer, create transitions, or use expert actions as predictions.
"""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import numpy as np
import torch

from ..native_policy_features import OwnedNativePolicySource
from ..native_secondary_input import prepare_native_secondary_input
from ..observed_empty_collections import PC_SOURCE
from ..runtime_optional_parent_features import RuntimeOptionalParentCardFeatures
from ..training_artifact_io import sha256_file
from . import expert_projection, features, networks, offline_policy
from .actor_handoff import OBJECTIVE_ID, validate_actor_metadata
from .contracts import ContractError, DecisionKind, FrozenJSON, State, digest, finite, integer, sha256
from .expert_projection import ExpertSemanticProjector, _scope
from .features import FeatureSchema, encode_batch
from .game_projection import feature_schema, project_prepared_information
from .native_observation import inspect_native_decision
from .semantic_entity_encoding import ENTITY_FEATURE_NAMES

FACTORY = "gkms_tool.rl.native_actor_policy:SharedNativeActorPolicy"
POLICY_KIND = "shared-offline-IQL"
CHECKPOINT_SCHEMA = "gkms.rl.shared-offline-training-checkpoint.v1"
SOURCE_NAMES = frozenset({"offline_dataset.py", "offline_policy.py", "features.py", "networks.py", "train_offline_policy.py"})


def _check_reference(reference):
    if (type(reference) is not dict or not {"path", "sha256"} <= set(reference) <= {"path", "sha256", "bytes"}
            or type(reference.get("path")) is not str):
        raise ContractError("explicit hash-bound inference reference required")
    path = Path(reference["path"])
    sha256(reference["sha256"], "inference reference")
    if sha256_file(path) != reference["sha256"] or ("bytes" in reference and path.stat().st_size != reference["bytes"]):
        raise ContractError("inference source/checkpoint bytes differ: " + path.name)
    return path


def _stamp(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def prepare_actor_projector(sources, *, runtime_loader_compatibility=None,
                            runtime_io_equivalence=None, max_entities=256,lazy_materials=False):
    """Verify a locator or reviewed additive-I/O bridge without relabeling training.

    ExpertSemanticProjector keeps its actual runtime identity. The separately
    computed training identity uses the unchanged original source references and
    the exact imported implementation bytes, then must match a checkpoint source
    binding. Locator proofs change only runtime_sources/helpers paths. The
    separate additive-I/O proof reuses the existing exact AST verifier; its
    factory and receipt produce a distinct, explicitly recorded runtime ID.
    """
    if type(sources) is not dict or set(sources) != {"contract_set", "original_shared_encoder", "loader_compatibility"}:
        raise ContractError("original three semantic source references required")
    runtime_sources = deepcopy(sources)
    relocation = None
    def construct(factory):
        if lazy_materials:
            from devtools.rl.actor_materials import construct_lazy_projector
            return construct_lazy_projector(factory,runtime_sources,max_entities=max_entities)
        return factory(runtime_sources,max_entities=max_entities)
    if runtime_io_equivalence is not None and runtime_loader_compatibility is not None:
        raise ContractError("Choose one explicit runtime source bridge")
    if runtime_io_equivalence is not None:
        # Reuse the existing, narrowly reviewed append-only I/O proof. The
        # original projector/source identity remains the checkpoint identity;
        # the runtime projector reports its distinct factory/proof identity.
        from devtools.rl.import_qualified_io_equivalence import IOEquivalentExpertProjector
        proof = json.loads(_check_reference(runtime_io_equivalence).read_bytes())
        parent = proof.get("parent_compatibility", {})
        original = sources["loader_compatibility"]
        if (parent.get("sha256") != original["sha256"]
                or Path(parent.get("path", "")).resolve() != Path(original["path"]).resolve()):
            raise ContractError("Runtime I/O proof belongs to another original projection loader")
        _check_reference(original)
        runtime_sources["loader_compatibility"] = deepcopy(runtime_io_equivalence)
        projector = construct(IOEquivalentExpertProjector)
        relocation = {"original_receipt": deepcopy(original), "runtime_receipt": deepcopy(runtime_io_equivalence),
            "kind": "reviewed-additive-IO-equivalence", "original_numeric_functions_unchanged": True}
    if runtime_loader_compatibility is not None:
        original = json.loads(_check_reference(sources["loader_compatibility"]).read_bytes())
        relocated = json.loads(_check_reference(runtime_loader_compatibility).read_bytes())
        normalized = deepcopy(relocated)
        if type(original) is not dict or type(relocated) is not dict:
            raise ContractError("original and relocated loader proof objects required")
        source_dir = Path(expert_projection.__file__).resolve().parent.parent
        for family in ("runtime_sources", "helpers"):
            before, after = original.get(family), relocated.get(family)
            if type(before) is not dict or type(after) is not dict or set(before) != set(after):
                raise ContractError("relocated loader dependency sets differ")
            for name, reference in after.items():
                if (Path(name).name != name or type(before[name]) is not dict
                        or "path" not in before[name]
                        or _check_reference(reference).resolve() != source_dir / name):
                    raise ContractError("relocated loader must bind actual imported source directory")
                normalized[family][name]["path"] = before[name]["path"]
        if FrozenJSON.of(normalized) != FrozenJSON.of(original):
            raise ContractError("loader proof differs beyond source/helper path relocation")
        runtime_sources["loader_compatibility"] = deepcopy(runtime_loader_compatibility)
        relocation = {"original_receipt": deepcopy(sources["loader_compatibility"]),
            "runtime_receipt": deepcopy(runtime_loader_compatibility), "only_source_and_helper_paths_changed": True}
    if runtime_io_equivalence is None:
        projector = construct(ExpertSemanticProjector)
    directory = Path(expert_projection.__file__).resolve().parent
    implementation = {name: sha256_file(directory / name) for name in
        ("expert_projection.py", "game_projection.py", "native_information_view.py", "semantic_entity_encoding.py")}
    training_identity = digest({"schema": expert_projection.VERSION, "features": asdict(projector.schema),
        "sources": sources, "implementation": implementation})
    if runtime_loader_compatibility is None and runtime_io_equivalence is None and training_identity != projector.identity:
        raise ContractError("shared projection identity formula differs from trained implementation")
    return projector, training_identity, relocation


def load_actor_checkpoint(checkpoint, *, dataset_identity, training_source_code, observed_source_code=None):
    """Load only final actor weights from a pinned safe training checkpoint."""
    sha256(dataset_identity, "trained dataset identity")
    if type(training_source_code) is not dict or set(training_source_code) != SOURCE_NAMES:
        raise ContractError("all five original training implementation references required")
    expected_code = {}
    for name, reference in training_source_code.items():
        _check_reference(reference)
        expected_code[name] = reference["sha256"]
    for module in (offline_policy, features, networks):
        if sha256_file(Path(module.__file__)) != expected_code[Path(module.__file__).name]:
            raise ContractError("runtime actor implementation differs from trained source")
    path = _check_reference(checkpoint)
    saved = torch.load(path, map_location="cpu", weights_only=True)
    _check_reference(checkpoint)
    if (type(saved) is not dict or saved.get("schema") != CHECKPOINT_SCHEMA
            or saved.get("objective_id") != OBJECTIVE_ID or saved.get("shadow_only") is not True
            or saved.get("activation_allowed") is not False or saved.get("phase") != "complete"
            or saved.get("dataset_identity") != dataset_identity or saved.get("source_code") != expected_code):
        raise ContractError("completed source-bound shared actor checkpoint required")
    if finite(saved.get("gamma"), "checkpoint gamma") != 1 or finite(saved.get("score_scale"), "score scale") != 10000:
        raise ContractError("checkpoint objective scale differs")
    config = saved.get("config")
    if type(config) is not dict:
        raise ContractError("original completed training schedule required")
    il_steps = integer(config.get("il_steps"), "scheduled IL updates")
    iql_steps = integer(config.get("iql_steps"), "scheduled IQL updates", 1)
    if (integer(saved.get("step"), "completed step") != il_steps + iql_steps
            or integer(saved.get("il_updates"), "completed IL updates") != il_steps
            or integer(saved.get("iql_updates"), "completed IQL updates") != iql_steps):
        raise ContractError("checkpoint update counts do not complete the original schedule")
    metadata = validate_actor_metadata(saved.get("model_metadata")).unpack()
    schema = FeatureSchema.from_dict(saved.get("feature_schema", {}))
    observed=metadata['model_kind']=='gkms.rl.shared-observed-outer-policy.v2'
    expected_schema=metadata.get('original_exam_schema_sha256',metadata['feature_schema_sha256'])
    if (schema.identity != expected_schema
            or schema != feature_schema(max_entities=schema.max_entities)
            or metadata["candidate_dim"] != len(ENTITY_FEATURE_NAMES)):
        raise ContractError("checkpoint schema/candidate codec differs from shared projection")
    bindings = saved.get("source_bindings")
    if type(bindings) is not dict or not bindings or any(digest(value) != key for key, value in bindings.items()):
        raise ContractError("checkpoint original source binding identities differ")
    if observed:
        import importlib
        from .observed_outer_policy import (ObservedOuterPolicyNet,observed_source_module_names,
            observed_inference_module_names)
        expected_observed=saved.get('observed_outer',{}).get('implementation')
        include_fine=isinstance(expected_observed,dict)and 'gkms_tool.rl.observed_outer_fine_projection'in expected_observed
        if include_fine:
            adapter=observed_source_code.get('gkms_tool.rl.observed_outer_fine_projection')if isinstance(observed_source_code,dict)else None
            if not adapter:raise ContractError('Fine inference dependency list needs its pinned adapter')
            _check_reference(adapter)
            import importlib.util
            origin=importlib.util.find_spec('gkms_tool.rl.observed_outer_fine_projection').origin
            if sha256_file(Path(origin))!=adapter['sha256']:raise ContractError('Fine source declaration differs before import')
        required_observed=set(observed_source_module_names(include_fine=include_fine))
        inference_modules=observed_inference_module_names(include_fine=include_fine)
        if (not isinstance(observed_source_code,dict)or not isinstance(expected_observed,dict)
                or set(observed_source_code)!=required_observed
                or {name:ref.get('sha256')for name,ref in observed_source_code.items()}!=expected_observed):
            raise ContractError('Complete explicit observed actor implementation inventory required')
        for name,ref in observed_source_code.items():
            _check_reference(ref)
            if name in inference_modules and sha256_file(Path(importlib.import_module(name).__file__))!=ref['sha256']:
                raise ContractError('Observed actor imported implementation differs: '+name)
        with torch.random.fork_rng(devices=[]):
            model=ObservedOuterPolicyNet(schema,networks.NetworkConfig(**metadata['network']),candidate_dim=metadata['candidate_dim'])
        if (saved.get('model_feature_schema')!=asdict(model.schema)or model.checkpoint_metadata()!=metadata
                or saved['observed_outer'].get('objective_id')!=model.checkpoint_metadata()['outer_objective']['id']):
            raise ContractError('Observed actor expanded schema/head metadata differs')
    else:
        if observed_source_code is not None or saved.get('observed_outer')is not None:
            raise ContractError('Original v1 actor must not borrow an observed-model source inventory')
        with torch.random.fork_rng(devices=[]):
            model = offline_policy.OfflinePolicyNet(schema, networks.NetworkConfig(**metadata["network"]),
                candidate_dim=metadata["candidate_dim"])
    expected = model.state_dict(); weights = saved.get("model")
    if (not isinstance(weights, dict) or set(weights) != set(expected)
            or any(not isinstance(weights[name], torch.Tensor) or weights[name].shape != value.shape
                or weights[name].dtype != value.dtype or not torch.isfinite(weights[name]).all()
                for name, value in expected.items())):
        raise ContractError("final actor tensor layout or finite weights differ")
    model.load_state_dict(weights, strict=True)
    model.eval().requires_grad_(False)
    return model, {"feature_schema": schema, "model_metadata": metadata, "source_bindings": deepcopy(bindings),
        "dataset_identity": dataset_identity, "checkpoint_step": saved["step"], "source_code": expected_code,
        **({'model_feature_schema':model.schema,'observed_source_code':deepcopy(observed_source_code)}if observed else{})}


def build_actor_batch(state, candidates, constraints, schema):
    """Label-free inference batch with the same candidate columns as collate."""
    if (not isinstance(state, State) or not isinstance(schema, FeatureSchema)
            or type(candidates) not in (list, tuple) or len(candidates) > 4096 or type(constraints) is not dict):
        raise ContractError("bounded projected actor input required")
    names = {name: i for i, name in enumerate(ENTITY_FEATURE_NAMES)}
    width = len(candidates)
    values = np.zeros((1, width, len(names)), dtype=np.float32)
    types = np.zeros((1, width), dtype=np.int64)
    for index, candidate in enumerate(candidates):
        if type(candidate) is not dict or set(candidate) != {"type", "values", "target"}:
            raise ContractError("original projected candidate shape differs")
        kind = integer(candidate["type"], "candidate action family", 1)
        if kind not in ((1, 2, 3) if state.kind is DecisionKind.MAIN else (4,)):
            raise ContractError("candidate family differs from actor decision type")
        if type(candidate["values"]) is not dict or set(candidate["values"]) - names.keys():
            raise ContractError("candidate semantic columns differ from trained codec")
        for name, value in candidate["values"].items():
            values[0, index, names[name]] = finite(value, "candidate feature")
        types[0, index] = kind
    if state.kind not in (DecisionKind.MAIN, DecisionKind.SECONDARY) or not np.isfinite(values).all():
        raise ContractError("actor kind or float32 candidate values invalid")
    if constraints.get("offered_count") != width:
        raise ContractError("candidate denominator differs from observed constraints")
    low = integer(constraints.get("pick_min"), "native minimum")
    high = integer(constraints.get("pick_max"), "native maximum")
    mode = constraints.get("selection_mode")
    if state.kind is DecisionKind.MAIN:
        if not width or (low, high, mode) != (1, 1, "main"):
            raise ContractError("main actor requires one action from a complete nonempty pool")
    else:
        from ..integrated_exam_bc_model import SecondaryConstraints
        SecondaryConstraints(low, high, width, mode)
    return offline_policy.OfflinePolicyBatch(encode_batch([state], schema), torch.from_numpy(values), torch.from_numpy(types),
        torch.ones((1, width), dtype=torch.bool), torch.tensor([0 if state.kind is DecisionKind.MAIN else 1]),
        torch.tensor([low]), torch.tensor([high]), torch.tensor([mode == "forced-empty-response"]))


class SharedNativeActorPolicy:
    """One owned source-exact native callback; no rule/BC fallback."""

    def __init__(self, specification, *, engine_identity):
        required = {"checkpoint", "dataset_identity", "semantic_sources", "training_source_code", "device"}
        if (type(specification) is not dict or not required <= set(specification)
                or set(specification) - required - {"runtime_loader_compatibility", "runtime_io_equivalence", "runtime_master_source", "projection_equivalence", "observed_source_code"}
                or specification["device"] != "cpu"):
            raise ContractError("explicit CPU shared actor specification required")
        self.specification = FrozenJSON.of(specification)
        self.source = OwnedNativePolicySource(engine_identity)
        if self.source.execution_master_policy != "source-exact" or self.source.master != self.source.source_master:
            raise ContractError("shared actor first evaluation requires original source-exact Master")
        self.model, self.metadata = load_actor_checkpoint(specification["checkpoint"],
            dataset_identity=specification["dataset_identity"], training_source_code=specification["training_source_code"],
            **({'observed_source_code':specification['observed_source_code']}if 'observed_source_code'in specification else{}))
        self.projector, self.training_projection_id, self.loader_relocation = prepare_actor_projector(
            specification["semantic_sources"], runtime_loader_compatibility=specification.get("runtime_loader_compatibility"),
            runtime_io_equivalence=specification.get("runtime_io_equivalence"),
            max_entities=self.metadata["feature_schema"].max_entities,
            lazy_materials=bool(specification.get('runtime_io_equivalence')))
        if self.projector.schema != self.metadata["feature_schema"]:
            raise ContractError("owned native projection differs from trained feature schema")
        self.runtime_base_projection_id = self.training_projection_id
        self.projection_equivalence_reference = None
        equivalence_watches = ()
        if specification.get("projection_equivalence") is not None:
            reference = specification["projection_equivalence"]
            receipt = json.loads(_check_reference(reference).read_bytes())
            if receipt.get("source_master_hash") == self.source.master:
                from .actor_projection_equivalence import bind_projection_equivalence
                proved, equivalence_watches = bind_projection_equivalence(reference,
                    checkpoint_sha256=specification["checkpoint"]["sha256"], dataset_identity=self.metadata["dataset_identity"],
                    source_master_hash=self.source.master, feature_schema_sha256=self.projector.schema.identity,
                    runtime_base_projection_id=self.runtime_base_projection_id, source_bindings=self.metadata["source_bindings"],
                    runtime_master_source=specification.get("runtime_master_source", {}))
                self.training_projection_id = proved["projection_id"]
                self.projection_equivalence_reference = deepcopy(reference)
        matches = [value for value in self.metadata["source_bindings"].values()
            if value.get("source_kind") == PC_SOURCE and value.get("source_master_hash") == self.source.master
            and value.get("engine_id") == self.source.native_core and value.get("metadata_sha256") == self.source.native_metadata
            and value.get("projection_id") == self.training_projection_id
            and value.get("feature_schema_sha256") == self.projector.schema.identity
            and value.get("objective_id") == OBJECTIVE_ID and value.get("label_family") == "expert-off-policy"]
        if len(matches) != 1:
            raise ContractError("no unique trained original-PC source/projection binding for native actor")
        self.training_source_binding = FrozenJSON.of(matches[0])
        self.runtime_encoder = None
        if specification.get("runtime_master_source") is not None:
            from ..runtime_master_source import load_runtime_master_source
            from ..runtime_master_features import RuntimeMasterFeatureEncoder
            runtime_source = load_runtime_master_source(specification["runtime_master_source"])
            self.runtime_encoder = RuntimeMasterFeatureEncoder(
                specification["semantic_sources"]["contract_set"],
                specification["semantic_sources"]["original_shared_encoder"], runtime_source=runtime_source,
                source_resolver=(getattr(self.projector,'material_source_resolver',None)
                    or self.projector.compatibility.make_source_resolver()),
                loader_compatibility=self.projector.compatibility,historical_encoder=self.projector.encoder)
        routing = self.runtime_encoder or self.projector.encoder
        base, self.contract = routing._route(self.source.master, self.source.produce)
        self.features = RuntimeOptionalParentCardFeatures(base.package,
            active_status_contract=self.contract.get("observed_active_status_contract"))
        package = self.features.package.contract
        if (package["native_core_sha256"] != self.source.native_core
                or package["native_metadata_sha256"] != self.source.native_metadata):
            raise ContractError("semantic package differs from owned native engine")
        refs = [specification["checkpoint"], *specification["training_source_code"].values(),
                *specification["semantic_sources"].values()]
        refs.extend(specification.get('observed_source_code',{}).values())
        if specification.get("runtime_loader_compatibility") is not None:
            refs.append(specification["runtime_loader_compatibility"])
        if specification.get("runtime_io_equivalence") is not None:
            refs.append(specification["runtime_io_equivalence"])
        if specification.get("runtime_master_source") is not None:
            refs.append(specification["runtime_master_source"])
        if specification.get("projection_equivalence") is not None:
            refs.append(specification["projection_equivalence"])
        refs.extend(getattr(self.projector,'allocation_execution',{}).get('sources',()))
        self._watches = (*tuple((path, _stamp(path)) for path in (_check_reference(ref) for ref in refs)), *equivalence_watches)

    def _guard(self, observation, kind):
        self.source.validate_owner(observation, kind)
        if FrozenJSON.of(observation.get("engine_identity")) != FrozenJSON.of(self.source.identity):
            raise ContractError("native callback engine identity differs from initialized actor")
        if any(_stamp(path) != stamp for path, stamp in self._watches):
            raise ContractError("pinned actor checkpoint or source changed during evaluation")
        self.projector.compatibility.validate_unchanged()
        if getattr(self, "runtime_encoder", None) is not None:
            self.runtime_encoder.validate_runtime_source()

    def project(self, observation):
        """Return public state/candidates without labels or a training transition."""
        checked = inspect_native_decision(observation, expected_observation_sha256=digest(observation))
        self._guard(observation, checked.kind)
        primary = self.source.primary(observation) if checked.kind == "main" else None
        secondary = prepare_native_secondary_input(observation) if checked.kind == "secondary" else None
        raw = observation["snapshot"]["state"]
        if raw.get("produceId") != self.source.produce:
            raise ContractError("actor observation mode differs from source initialization")
        information, detail = project_prepared_information(raw, self.features, self.contract, self.projector.schema,
            primary=primary, secondary=secondary, source_kind=PC_SOURCE)
        scope = _scope(raw, self.source.identity["run_id"], self.source.identity["source_reference"]["sha256"])
        model_binding = digest({"checkpoint": self.specification.unpack()["checkpoint"]["sha256"],
            "trained_source": self.training_source_binding.sha256, "owner": self.source.identity})
        state = State(scope, "owned-PC:" + str(self.source.identity["worker_pid"]), checked.identity,
            DecisionKind.MAIN if checked.kind == "main" else DecisionKind.SECONDARY, information, model_binding)
        self._guard(observation, checked.kind)
        return checked, state, detail

    def decide(self, observation):
        before = digest(observation)
        checked, state, detail = self.project(observation)
        batch = build_actor_batch(state, detail["candidates"], detail["constraints"], self.projector.schema)
        with torch.inference_mode():
            response = self.model.greedy_actions(batch)
        length = int(response.lengths[0])
        columns = response.indices[0, :length].tolist()
        if any(type(i) is not int or not 0 <= i < len(detail["candidates"]) for i in columns):
            raise ContractError("actor returned padding or an unknown native candidate")
        if checked.kind == "main":
            if len(columns) != 1:
                raise ContractError("native main actor must return exactly one candidate")
            action = deepcopy(detail["candidates"][columns[0]]["target"])
        else:
            action = {"type": 4, "indexes": [detail["candidates"][i]["target"]["offered_ordinal"] for i in columns]}
        checked.validate_action(action)
        if digest(observation) != before:
            raise ContractError("actor mutated native observation")
        self._guard(observation, checked.kind)
        forced = detail["constraints"]["selection_mode"] == "forced-empty-response"
        return {"action": action, "policy_source": {"kind": POLICY_KIND, "objective_id": OBJECTIVE_ID,
            "checkpoint": self.specification.unpack()["checkpoint"], "dataset_identity": self.metadata["dataset_identity"],
            "checkpoint_step": self.metadata["checkpoint_step"], "feature_schema_sha256": self.projector.schema.identity,
            "projection_id": self.training_projection_id, "runtime_projection_id": self.projector.identity,
            "runtime_base_projection_id": getattr(self, "runtime_base_projection_id", self.training_projection_id),
            "runtime_material_reuse":self.runtime_encoder.historical_material_reuse if self.runtime_encoder is not None else None,
            "allocation_execution":deepcopy(getattr(self.projector,'allocation_execution',None)),
            "projection_equivalence": deepcopy(getattr(self, "projection_equivalence_reference", None)),
            "loader_locator_relocation": deepcopy(self.loader_relocation), "source_master_hash": self.source.master,
            "trained_source_binding_sha256": self.training_source_binding.sha256, "decision_type": checked.kind,
            "learned_model_used": not forced, "native_forced_response": forced,
            "teacher_action_used": False, "legacy_rule_fallback": False, "shadow_only": True,
            "automatic_formal_model_activation": False, "score_prediction_available": False},
            "diagnostics": {"observation_sha256": checked.identity, "public_information_sha256": state.information.sha256,
                "selected_candidate_columns": columns, "ordered_secondary_is_one_native_response": True}}


__all__ = ["SharedNativeActorPolicy", "load_actor_checkpoint", "build_actor_batch", "prepare_actor_projector", "FACTORY", "POLICY_KIND"]
