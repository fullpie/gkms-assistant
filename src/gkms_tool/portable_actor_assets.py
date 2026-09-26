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
LABEL = "共用離線 RL（第4階段）"
MODEL_SHA256 = "961dbeae592f891f6da7185c8ee051d2d199cc0f362c5910c48073a71317fab1"


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
        and qualification.get("validation_rows") == 62310
        and qualification.get("training_updates") == 4000
        and qualification.get("input_parity_rows", 0) >= 44
        and qualification.get("public_activation_authorized") is True
        and qualification.get("full_coverage") is False,
        "Public RL qualification does not bind the completed fixed actor")
    return {"id": VARIANT, "label": LABEL, "model_label": LABEL,
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
    _require(schema == feature_schema(max_entities=schema.max_entities)
        and schema.identity == actor_metadata["feature_schema_sha256"],
        "Public RL feature schema differs from trained representation")
    with torch.random.fork_rng(devices=[]):
        actor = OfflinePolicyNet(schema, NetworkConfig(**actor_metadata["network"]),
            candidate_dim=actor_metadata["candidate_dim"])
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
    return actor, {**metadata, "feature_schema": schema, "model_metadata": actor_metadata}


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
    policy.model, policy.metadata = load_actor_parameters(assets)
    policy.model_reference = assets.physical_reference(assets.manifest["models"][VARIANT]["artifact"])
    policy.encoder = PortableFeatureEncoder(assets)
    policy.catalog = PortableMasterCatalog(policy.encoder)
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
    policy._session_generation = policy._observed_master_identity = None
    # The portable features inherit the same optional-parent numeric visitor.
    # Recreating its private constructor would unnecessarily open research proofs.
    policy._semantic_features = {(assets.manifest["master_hash"], mode): policy.encoder.features
        for mode in assets.manifest["produce_ids"]}
    policy.qualification = deepcopy(descriptor["qualification"])
    policy.last_secondary_decision = None


__all__ = ["actor_descriptor", "load_actor_parameters", "load_configured_actor_descriptor",
    "initialize_portable_actor_policy", "SCHEMA", "VARIANT", "MODEL_SHA256"]
