"""Release-pinned shared RL inference over the existing portable Master codec.

The public artifact contains final network tensors and content identities only.
Training checkpoints, RAW samples, optimizer state and developer proofs remain
in the development workspace. Numerical model and projection methods are reused.
"""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from .application_paths import app_root, public_installation
from .portable_model_assets import (PortableFeatureEncoder, PortableMasterCatalog,
    configured_portable_assets, _require, _tensor_identity)
from .training_artifact_io import sha256_file

SCHEMA = "gkms.portable-shared-actor.v1"
VARIANT = "rl_shared_iql"
LABEL = "RL_v1"
MODEL_SHA256 = "9ca3b3dcdcebf8797fe9d7a3b8f1b63830eeb977bce6983fa5615a9344c9d1bf"


def public_reference_material_policy(assets):
    from .runtime_live_exam_input import validate_public_reference_material_policy
    value = assets.manifest.get('reference_material_policy')
    _require(isinstance(value, dict), 'Release reference-material policy is missing')
    policy = {**deepcopy(value), 'model_manifest_sha256': assets.reference['sha256']}
    return validate_public_reference_material_policy(policy, checkpoint_sha256=MODEL_SHA256,
        reference_master_hash=assets.manifest['master_hash'], model_manifest_sha256=assets.reference['sha256'])


def actor_descriptor(assets):
    assets.validate_unchanged()
    model = assets.manifest["models"].get(VARIANT)
    _require(isinstance(model, dict) and model.get("original_model_sha256") == MODEL_SHA256,
        "Public RL source checkpoint differs from the release model")
    qualification = assets.read(assets.manifest["files"]["actor_qualification"])
    _require(qualification.get("schema") == "gkms.public-shared-actor-qualification.v1"
        and qualification.get("original_model_sha256") == MODEL_SHA256
        and qualification.get("fixed_weights") is True
        and qualification.get("trained") is True
        and qualification.get("validation_rows") == 62249
        and qualification.get("outer_validation_rows") == 17944
        and qualification.get("initial_validation_histories") == 763
        and qualification.get("evaluation_complete") is True
        and qualification.get("actor_weights_unchanged") is True
        and qualification.get("artifacts_stable_after_evaluation") is True
        and qualification.get("training_updates") == 4000
        and qualification.get("input_parity_rows", 0) >= 44
        and qualification.get("public_activation_authorized") is True
        and qualification.get("full_coverage") is False,
        "Public RL qualification does not bind the completed fixed actor")
    result = {"id": VARIANT, "label": LABEL, "model_label": LABEL,
        "model_sha256": MODEL_SHA256, "artifact_sha256": model["artifact"]["sha256"],
        "model": assets.physical_reference(model["artifact"]),
        "available": True, "artifact_available": True, "live_ready": True,
        "can_request_preflight": True, "reason": None, "start_block_reason": None,
        "research_fallback": False, "private_only": False, "portable_actor": True,
        "fixed_weights": True, "qualification": qualification,
        "descriptor_reference": deepcopy(assets.reference),
        "portable_assets_reference": deepcopy(assets.reference),
        "training_coverage": deepcopy(qualification["scope"]),
        "unsupported_route_behavior": "stop-with-reason"}
    result["specification"] = {"checkpoint": deepcopy(qualification["original_checkpoint_identity"]),
        "dataset_identity": qualification["dataset_identity"], "device": "cpu"}
    from .portable_loadout_assets import portable_initial_descriptor
    result["initial_loadout"] = portable_initial_descriptor(assets)
    result['public_reference_material_policy'] = public_reference_material_policy(assets)
    return result


def load_configured_actor_descriptor(*, project_root=None, refresh=False):
    root = app_root() if project_root is None else Path(project_root)
    if public_installation(root):
        assets = configured_portable_assets(project_root=root)
        _require(assets is not None, "The public RL inference package is missing")
        return actor_descriptor(assets)
    from .rl.private_actor_assets import load_private_actor_descriptor
    return load_private_actor_descriptor(project_root=root, refresh=refresh)


def load_actor_parameters(assets):
    """Construct the unchanged Torch actor and verify every exported tensor."""
    import torch
    from .rl.actor_handoff import validate_actor_metadata
    from .rl.features import FeatureSchema
    from .rl.game_projection import feature_schema
    from .rl.networks import NetworkConfig
    from .rl.offline_policy import OfflinePolicyNet, OBJECTIVE_ID, SCORE_SCALE, GAMMA
    model = assets.manifest["models"][VARIANT]
    metadata = assets.read(assets.manifest["files"]["actor_metadata"])
    _require(metadata.get("schema") == SCHEMA
        and metadata.get("original_model_sha256") == MODEL_SHA256
        and metadata.get("objective_id") == OBJECTIVE_ID
        and metadata.get("score_scale") == SCORE_SCALE and metadata.get("gamma") == GAMMA,
        "Public RL objective or original model differs")
    schema = FeatureSchema.from_dict(metadata["feature_schema"])
    actor_metadata = validate_actor_metadata(metadata["model_metadata"]).unpack()
    from .rl.observed_outer_policy import ObservedOuterPolicyNet, MODEL_KIND
    _require(actor_metadata["model_kind"] == MODEL_KIND
        and schema == feature_schema(max_entities=schema.max_entities)
        and schema.identity == actor_metadata["original_exam_schema_sha256"],
        "Public RL feature schema differs from trained representation")
    with torch.random.fork_rng(devices=[]):
        actor = ObservedOuterPolicyNet(schema, NetworkConfig(**actor_metadata["network"]),
            candidate_dim=actor_metadata["candidate_dim"])
    _require(actor.checkpoint_metadata() == actor_metadata,
        "Public shared initial/outer/exam actor metadata differs")
    expected = actor.state_dict()
    reference = assets.physical_reference(model["artifact"])
    with np.load(reference["path"], allow_pickle=False) as packed:
        _require(set(packed.files) == set(expected) == set(model["tensors"]),
            "Public RL tensor names differ from the fixed network")
        arrays = {name: packed[name].copy() for name in packed.files}
    _require(all(_tensor_identity(value) == model["tensors"][name]
        and tuple(value.shape) == tuple(expected[name].shape)
        and torch.from_numpy(value).dtype == expected[name].dtype and np.isfinite(value).all()
        for name, value in arrays.items()), "Public RL tensor bytes/layout differ")
    actor.load_state_dict({name: torch.from_numpy(value) for name, value in arrays.items()}, strict=True)
    actor.eval().requires_grad_(False)
    assets.validate_unchanged()
    return actor, {**metadata, "feature_schema": schema, "model_feature_schema": actor.schema,
        "model_metadata": actor_metadata}


def initialize_portable_actor_policy(policy, descriptor, *, produce_id, idol_card_id, run_id):
    """Portable constructor only; the same live owner and decision methods run."""
    from .runtime_gui_exam_policy import _scope
    from .rl.actor_handoff import ActorBinding
    from .rl.contracts import FrozenJSON, digest
    from . import live_feature_presence, live_phase_counter_view
    assets = configured_portable_assets()
    _require(assets is not None and descriptor == actor_descriptor(assets),
        "Public actor descriptor changed before construction")
    policy.idol, policy.flow_scope = _scope(produce_id, idol_card_id, VARIANT)
    policy.produce_id, policy.idol_card_id, policy.run_id = produce_id, idol_card_id, run_id
    policy.variant_id = VARIANT
    policy.reference_trial = None
    policy._reference_preflight_owner = None
    policy.public_reference_material_policy = public_reference_material_policy(assets)
    policy._public_reference_preflight_owner = None
    policy._public_reference_observed_engine_identity = None
    policy._public_reference_observed_generation = None
    policy.model, policy.metadata = load_actor_parameters(assets)
    policy.model_reference = assets.physical_reference(assets.manifest["models"][VARIANT]["artifact"])
    policy.encoder = PortableFeatureEncoder(assets)
    policy.catalog = PortableMasterCatalog(policy.encoder,
        reference_policy=policy.public_reference_material_policy,
        runtime_owner=policy._public_reference_owner, idol_card_id=idol_card_id)
    policy.loader_compatibility = policy.source_resolver = assets
    policy.runtime_source = SimpleNamespace(execution_master_version=assets.manifest["execution_master_version"])
    policy.training_projection_id = policy.metadata["training_projection_id"]
    policy.runtime_base_projection_id = policy.metadata["runtime_base_projection_id"]
    projection_id = digest({"schema": "gkms.portable-actor-projection.v1",
        "manifest_sha256": assets.reference["sha256"],
        "feature_schema_sha256": policy.metadata["feature_schema"].identity,
        "numeric_sources": assets.manifest["numeric_sources"]})
    policy.projector = SimpleNamespace(schema=policy.metadata["feature_schema"], identity=projection_id,
        compatibility=assets)
    policy.loader_relocation = {"kind": "release-portable-inference", "manifest": deepcopy(assets.reference),
        "original_numeric_functions_unchanged": True, "raw_research_required": False}
    policy.projection_equivalence_reference = assets.physical_reference(assets.manifest["files"]["actor_qualification"])
    policy.runtime_model_compatibility = {"schema": "gkms.portable-actor-runtime-binding.v1",
        "original_model_sha256": MODEL_SHA256, "portable_manifest_sha256": assets.reference["sha256"],
        "training_admitted": False}
    policy._model_stat = policy._stat()
    policy._feature_view_path = Path(live_feature_presence.__file__).resolve()
    policy._feature_view_stat = policy._file_stamp(policy._feature_view_path)
    dependency = Path(live_phase_counter_view.__file__).resolve()
    policy._feature_view_dependencies = ((dependency, policy._file_stamp(dependency)),)
    policy._feature_view_reference = {"schema": live_feature_presence.SCHEMA,
        "path": str(policy._feature_view_path), "sha256": sha256_file(policy._feature_view_path),
        "dependencies": [{"schema": live_phase_counter_view.SCHEMA, "path": str(dependency),
            "sha256": sha256_file(dependency)}]}
    implementation = Path(__import__(policy.__module__, fromlist=['x']).__file__).resolve()
    policy._implementation_reference = {"path": str(implementation), "sha256": sha256_file(implementation)}
    policy._actor_watches = (*tuple(assets._watched.items()),
        (implementation, policy._file_stamp(implementation)),
        (Path(__file__).resolve(), policy._file_stamp(Path(__file__).resolve())))
    policy.actor_binding = ActorBinding(MODEL_SHA256, run_id, FrozenJSON.of(policy.metadata["model_metadata"]),
        FrozenJSON.of({"dataset_identity": policy.metadata["dataset_identity"],
            "training_projection_id": policy.training_projection_id,
            "portable_manifest": deepcopy(assets.reference)}))
    policy._binding = {"loaded": True, **policy.actor_binding.policy_identity, "label": descriptor["label"],
        "artifact_sha256": policy.model_reference["sha256"], "flow_scope": policy.flow_scope.binding,
        "runtime_loader_compatibility_sha256": assets.manifest["source_loader_receipt_sha256"],
        "portable_manifest_sha256": assets.reference["sha256"], "fixed_weights": True, "private_only": False,
        "live_feature_view": deepcopy(policy._feature_view_reference),
        "live_actor_implementation": deepcopy(policy._implementation_reference),
        "runtime_master_package": policy.encoder.runtime_master_binding(
            policy.runtime_source.execution_master_version, produce_id)["runtime_package_binding"]}
    policy._binding['public_reference_material_policy'] = deepcopy(policy.public_reference_material_policy)
    policy._binding.update(source_equivalence_verified=False, reference_lookup_only=True, training_admitted=False)
    policy._session_generation = policy._observed_master_identity = None
    # The portable features inherit the same optional-parent numeric visitor.
    # Recreating its private constructor would unnecessarily open research proofs.
    policy._semantic_features = {(assets.manifest["master_hash"], mode): policy.encoder.features
        for mode in assets.manifest["produce_ids"]}
    policy.qualification = deepcopy(descriptor["qualification"])
    policy.last_secondary_decision = None


class PortableObservedOuterPolicyFactory:
    """Bind the original observed actor and history owner to release assets."""
    observed_actor = True

    def __init__(self, assets, *, progress_callback=None):
        import threading
        self.assets = assets
        self.descriptor = actor_descriptor(assets)
        self.progress = progress_callback
        self._run = None
        self._policy = None
        self._lock = threading.RLock()

    def __call__(self, *, run_id, produce_id, idol_card_id, cancelled=lambda: False):
        from .portable_model_assets import PortableCardFeatures
        from .portable_outer_assets import asset_directory, verify_assets
        from .rl.observed_outer_fine_projection import FineOuterSemanticSource
        from .runtime_observed_outer_policy import RuntimeObservedOuterPolicy
        with self._lock:
            self.assets.validate_unchanged()
            scope = run_id, produce_id, idol_card_id
            _require(all(isinstance(x, str) and x for x in scope) and callable(cancelled),
                "Portable outer actor requires the original run/idol/mode owner")
            _require(self._run in (None, scope), "A fixed outer actor cannot be reused across runs")
            if self._policy is not None:
                return self._policy
            _require(not cancelled(), "Cancelled before portable outer model loading")
            root = asset_directory()
            outer = verify_assets(root)
            _require(outer is not None and outer["master_hash"] == self.assets.manifest["master_hash"]
                and produce_id in outer["produce_ids"], "Public observed actor and outer material scope differs")
            source = root / "manifest.json"
            reference = {"path": str(source), "sha256": sha256_file(source), "bytes": source.stat().st_size}
            material = FineOuterSemanticSource(PortableCardFeatures(self.assets), reference,
                validate_materials=self.assets.validate_unchanged)
            model, metadata = load_actor_parameters(self.assets)
            versions = tuple((k, v._version, v.data_ptr(), str(v.device)) for k, v in model.state_dict().items())
            def validate(actual):
                self.assets.validate_unchanged()
                material.validate_unchanged()
                _require(actual is model and not model.training and not any(p.requires_grad for p in model.parameters())
                    and tuple((k, v._version, v.data_ptr(), str(v.device)) for k, v in model.state_dict().items()) == versions,
                    "Portable outer model or fixed tensor identities changed")
                return metadata
            _require(not cancelled(), "Cancelled after portable outer model loading")
            self._policy = RuntimeObservedOuterPolicy(model=model, semantic_features=material,
                validate_model=validate, run_id=run_id, produce_id=produce_id, idol_card_id=idol_card_id,
                checkpoint_sha256=MODEL_SHA256, cancelled=cancelled,
                public_reference_material_policy=public_reference_material_policy(self.assets))
            self._run = scope
            validate(model)
            return self._policy


def load_portable_outer_policy_factory(*, progress_callback=None):
    assets = configured_portable_assets()
    _require(assets is not None, "Public shared actor assets are required for cultivation")
    return PortableObservedOuterPolicyFactory(assets, progress_callback=progress_callback)


__all__ = ["actor_descriptor", "load_actor_parameters", "load_configured_actor_descriptor",
    "initialize_portable_actor_policy", "load_portable_outer_policy_factory", "SCHEMA", "VARIANT", "MODEL_SHA256"]
