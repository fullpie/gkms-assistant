"""Small, fail-closed discrete-IQL learner for frozen Exact Exam stages.

The module deliberately has no runtime or game-control dependency.  It binds a
frozen stage dataset to a training spec and an accepted Exact Exam BC anchor,
reuses the BC feature contract, and emits a shadow-only model/report.  Only
``state_before`` and the same state's ordered authoritative legal candidates
participate in inference features.  ``state_after`` is used solely to verify
the transition and obtain the next row; reward and terminal are targets only.
Q and V heads learn residuals around a fixed train flow/stage mean-return
prior; absolute values are reconstructed in training, evaluation, and artifact
inference.  The actor remains the accepted BC logits plus a learned residual.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import zipfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np

from .card_semantic_features import (
    SEMANTIC_ENCODING, copy_card_feature_contract, load_card_semantic_features,
    validate_card_feature_encoding,
)

from .behavior_cloning import (
    EXACT_REPORT_SCHEMA,
    MODEL_SCHEMA as BC_MODEL_SCHEMA,
    SPLIT_CODES,
    _Adam,
    _context_vector,
    _exact_candidate_context_semantics,
    _hash_index,
    _outer_forward,
    _pad_candidate_token_rows,
    _pad_token_rows,
    build_exact_main_action_context_tokens,
    exact_candidate_field_tokens,
    exact_candidate_semantics,
    load_model_artifact,
)
from .exact_policy_contract import (
    strict_exact_candidate_index,
    validate_exact_legal_candidates,
)
from .canonical_training_labels import (
    EFFECT_BY_NATIVE_VALUE,
    KNOWN_READINESS_V5_FLOWS,
    PLAN_BY_NATIVE_VALUE,
    STAGE_BY_NATIVE_VALUE,
)
from .training_artifact_io import atomic_write, canonical_json_bytes, sha256_file
from .training_spec import SPEC_V2_SCHEMA, SPEC_SCOPED_SCHEMA, build_scoped_training_spec, declared_training_scope


MODEL_SCHEMA: Final = "gkms.numpy-offline-rl-model.v2"
REPORT_SCHEMA: Final = "gkms.exact-main-action-offline-rl-report.v1"
MODEL_KIND: Final = "exact-main-action-discrete-iql-v1"
STAGE_SCHEMA: Final = "gkms.offline-rl-stage.v1"
DATASET_MANIFEST_SCHEMA: Final = "gkms.offline-rl-dataset-manifest.v1"
CANONICAL_MANIFEST_SCHEMA: Final = "gkms.canonical-training-label-manifest.v1"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_STAGES: Final = (
    PROJECT_ROOT
    / "var"
    / "offline_rl"
    / "exact_main_action_baseline_20260830_exact_v3_minimum_frozen"
    / "stages.jsonl"
)
DEFAULT_SPEC: Final = (
    PROJECT_ROOT
    / "var"
    / "training_specs"
    / "exact_v3_minimum"
    / "training_spec.json"
)
DEFAULT_ANCHOR_MODEL: Final = (
    PROJECT_ROOT
    / "var"
    / "models"
    / "behavior_cloning_exact_v3_minimum_v0"
    / "exact_main_action_bc_model.npz"
)
DEFAULT_ANCHOR_REPORT: Final = (
    PROJECT_ROOT
    / "var"
    / "models"
    / "behavior_cloning_exact_v3_minimum_v0"
    / "exact_main_action_bc_report.json"
)
DEFAULT_OUTPUT_ROOT: Final = (
    PROJECT_ROOT / "var" / "models" / "offline_rl_exact_v3_minimum_v0"
)
FROZEN_STAGES_SHA256: Final = (
    "bcbcb54cd28e1ee8dc9c446f3f37ef08b883e407c1068ff0c04011c740fe2196"
)
FROZEN_SPEC_SHA256: Final = (
    "375376b2def88526fcd5ff6e89c855bcf3e9e804fee494f69b947a3a6f6265b1"
)
FROZEN_ANCHOR_MODEL_SHA256: Final = (
    "72a9c20ecfe727155324104b7e6c4b34103c38cdafd44c81958059ec19ad0909"
)
FROZEN_ANCHOR_REPORT_SHA256: Final = (
    "bb106fca048e3bc770f5722acdfb39fed6329f6bb29f1f84dd1513d242e23fd7"
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read_object(path: Path) -> Mapping[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _strict_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _strict_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be a boolean")
    return value


def _positive_bucket_count(value: object, label: str) -> int:
    result = _strict_int(value, label, minimum=2)
    return result


def _resolve_locked_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"offline IQL training spec has no {label} path lock")
    path = Path(value)
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def _canonical_scope_value(value: object, mapping: Mapping[object, str]) -> object:
    if isinstance(value, bool):
        return value
    return mapping.get(value, value)


def _validated_training_flows(training_scope: Mapping[str, Any] | None) -> frozenset[str]:
    if training_scope is None:
        return KNOWN_READINESS_V5_FLOWS
    if not isinstance(training_scope, Mapping) or training_scope != declared_training_scope(training_scope.get("flows", [])):
        raise ValueError("exact IQL explicit training scope is invalid")
    return frozenset(training_scope["flows"])


def _validate_observation_scope(
    state_before: Mapping[str, Any], *, flow: str, stage: str, training_scope: Mapping[str, Any] | None = None,
) -> None:
    if stage not in {"Mid1", "Mid2", "Final"}:
        raise ValueError("exact IQL stage is outside Mid1/Mid2/Final")
    if flow not in _validated_training_flows(training_scope):
        raise ValueError("exact IQL flow is outside the frozen five-flow cohort")
    parts = flow.split("|")
    if len(parts) != 3 or any(not value for value in parts):
        raise ValueError("exact IQL flow identity is invalid")
    produce, plan, effect = parts
    raw_produce = state_before.get("produceId", state_before.get("produce_id"))
    raw_plan = state_before.get("planType", state_before.get("plan_type"))
    raw_effect = state_before.get(
        "mainEffectType", state_before.get("main_effect_type")
    )
    raw_stage = state_before.get("stepType", state_before.get("step_type_value"))
    actual_plan = _canonical_scope_value(raw_plan, PLAN_BY_NATIVE_VALUE)
    actual_effect = _canonical_scope_value(raw_effect, EFFECT_BY_NATIVE_VALUE)
    actual_stage = _canonical_scope_value(raw_stage, STAGE_BY_NATIVE_VALUE)
    if (raw_produce, actual_plan, actual_effect, actual_stage) != (
        produce,
        plan,
        effect,
        stage,
    ):
        raise ValueError("exact IQL flow/stage disagrees with native state identity")


def _split_counts(values: np.ndarray) -> dict[str, int]:
    return {
        name: int((values == code).sum()) for name, code in SPLIT_CODES.items()
    }


@dataclass(frozen=True, slots=True)
class EncodedExactObservation:
    """Unpadded BC-compatible features for one observable decision."""

    context_indices: tuple[int, ...]
    candidate_indices: tuple[tuple[int, ...], ...]
    candidate_semantics: tuple[str, ...]


def encode_exact_observation(
    *,
    state_before: Mapping[str, Any],
    flow: str,
    stage: str,
    legal_candidates: Sequence[object],
    context_buckets: int = 4096,
    candidate_buckets: int = 65536,
    semantic_features: Any | None = None,
    training_scope: Mapping[str, Any] | None = None,
) -> EncodedExactObservation:
    """Encode exactly the fields available at inference time.

    The narrow signature is intentional: future state, reward, terminal, and
    chosen action cannot accidentally enter this function.
    """

    context_buckets = _positive_bucket_count(
        context_buckets, "context_buckets"
    )
    candidate_buckets = _positive_bucket_count(
        candidate_buckets, "candidate_buckets"
    )
    _validate_observation_scope(state_before, flow=flow, stage=stage, training_scope=training_scope)
    validate_exact_legal_candidates(state_before, legal_candidates)
    semantics = exact_candidate_semantics(state_before, legal_candidates)
    if len(set(semantics)) != len(semantics):
        raise ValueError("exact IQL candidate semantics are not unique")
    fields = exact_candidate_field_tokens(state_before, legal_candidates, semantic_features=semantic_features)
    context_tokens = build_exact_main_action_context_tokens(
        state_before=state_before,
        flow=flow,
        stage=stage,
        candidate_semantics=_exact_candidate_context_semantics(fields),
        semantic_features=semantic_features,
    )
    context = tuple(_hash_index(value, context_buckets) for value in context_tokens)
    candidates = tuple(
        tuple(
            dict.fromkeys(
                _hash_index(
                    f"exact-candidate-field:{field}", candidate_buckets
                )
                for field in row
            )
        )
        for row in fields
    )
    if any(not row for row in candidates):
        raise ValueError("exact IQL candidate encoding is empty")
    if len(set(candidates)) != len(candidates):
        raise ValueError("exact IQL candidate hash collision")
    return EncodedExactObservation(context, candidates, semantics)


@dataclass(frozen=True, slots=True)
class IQLDataset:
    context_indices: np.ndarray
    context_mask: np.ndarray
    candidate_indices: np.ndarray
    candidate_token_mask: np.ndarray
    legal_mask: np.ndarray
    next_context_indices: np.ndarray
    next_context_mask: np.ndarray
    action_indices: np.ndarray
    rewards: np.ndarray
    dones: np.ndarray
    splits: np.ndarray
    weights: np.ndarray
    flow_ids: np.ndarray
    flow_names: tuple[str, ...]
    flows: tuple[str, ...]
    stages: tuple[str, ...]
    stage_ids: tuple[str, ...]
    trajectory_ids: tuple[str, ...]
    action_kinds: tuple[str, ...]
    stage_start: np.ndarray
    stage_returns: np.ndarray
    reward_divisor: float
    context_buckets: int
    candidate_buckets: int

    @property
    def size(self) -> int:
        return int(self.action_indices.shape[0])


def _validate_bc_anchor_arrays(
    parameters: Mapping[str, np.ndarray], metadata: Mapping[str, Any]
) -> None:
    required = {
        "context_embedding",
        "candidate_embedding",
        "w1",
        "b1",
        "w2",
        "b2",
        "w3",
        "b3",
    }
    if set(parameters) != required:
        raise ValueError("offline IQL BC anchor required arrays mismatch")
    if any(
        value.dtype.kind != "f" or not np.isfinite(value).all()
        for value in parameters.values()
    ):
        raise ValueError("offline IQL BC anchor arrays must be finite floats")
    context_buckets = _positive_bucket_count(
        metadata.get("context_buckets"), "anchor context_buckets"
    )
    candidate_buckets = _positive_bucket_count(
        metadata.get("candidate_buckets"), "anchor candidate_buckets"
    )
    context_embedding = parameters["context_embedding"]
    candidate_embedding = parameters["candidate_embedding"]
    if context_embedding.ndim != 2 or candidate_embedding.ndim != 2:
        raise ValueError("offline IQL BC anchor embeddings must be matrices")
    embedding_dim = context_embedding.shape[1]
    if (
        context_embedding.shape[0] != context_buckets
        or candidate_embedding.shape != (candidate_buckets, embedding_dim)
        or parameters["w1"].ndim != 2
        or parameters["w1"].shape[0] != embedding_dim * 3
    ):
        raise ValueError("offline IQL BC anchor embedding/trunk shape mismatch")
    hidden1 = parameters["w1"].shape[1]
    if parameters["b1"].shape != (hidden1,):
        raise ValueError("offline IQL BC anchor first hidden shape mismatch")
    if parameters["w2"].ndim != 2 or parameters["w2"].shape[0] != hidden1:
        raise ValueError("offline IQL BC anchor second hidden shape mismatch")
    hidden2 = parameters["w2"].shape[1]
    if (
        parameters["b2"].shape != (hidden2,)
        or parameters["w3"].shape != (hidden2,)
        or parameters["b3"].shape != (1,)
    ):
        raise ValueError("offline IQL BC anchor output shape mismatch")


def _validate_input_locks(
    *,
    stages_path: Path,
    spec_path: Path,
    anchor_model_path: Path,
    anchor_report_path: Path,
    enforce_frozen_hashes: bool,
) -> tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    dict[str, np.ndarray],
    Mapping[str, Any],
    Mapping[str, Any],
]:
    enforce_frozen_hashes = _strict_bool(
        enforce_frozen_hashes, "enforce_frozen_hashes"
    )
    spec = _read_object(spec_path)
    if spec.get("schema") not in {SPEC_V2_SCHEMA, SPEC_SCOPED_SCHEMA}:
        raise ValueError("offline IQL requires training spec v2 or scoped spec")
    if spec.get("schema") == SPEC_SCOPED_SCHEMA:
        scope = _mapping(spec.get("training_scope"))
        rebuilt = build_scoped_training_spec(exact_rl_root=Path(stages_path).parent,
            flows=scope.get("flows", []), enable_offline_rl=True)
        for key in ("training_scope", "dataset_lock", "inventory", "objectives"):
            if spec.get(key) != rebuilt[key]:
                raise ValueError(f"scoped IQL {key} no longer matches frozen evidence")
    stages_sha = sha256_file(stages_path)
    spec_sha = sha256_file(spec_path)
    anchor_model_sha = sha256_file(anchor_model_path)
    anchor_report_sha = sha256_file(anchor_report_path)
    # ``training_spec.v2`` below already locks the stage dataset, its dataset/
    # canonical/split manifests, and the accepted BC report/model pair by
    # content hash.  The former extra quartet of one v3 artifact's literal
    # SHA-256 values made every subsequently frozen cohort impossible to use
    # despite satisfying the same complete contract.  Retain whether the
    # legacy default profile happens to match as provenance, not as a second
    # cohort-specific admission gate.
    legacy_default_profile_match = (
        stages_sha == FROZEN_STAGES_SHA256
        and spec_sha == FROZEN_SPEC_SHA256
        and anchor_model_sha == FROZEN_ANCHOR_MODEL_SHA256
        and anchor_report_sha == FROZEN_ANCHOR_REPORT_SHA256
    )
    exact_lock = _mapping(_mapping(spec.get("dataset_lock")).get("exact_exam"))
    locked_stages = _resolve_locked_path(exact_lock.get("stages_path"), "stages")
    if locked_stages != Path(stages_path).resolve():
        raise ValueError("offline IQL stages path does not match the exact spec lock")
    if exact_lock.get("stages_sha256") != stages_sha:
        raise ValueError("offline IQL stages do not match the frozen training spec")
    if exact_lock.get("state_authority") != "frozen native runtime S,A,S'":
        raise ValueError("offline IQL state authority is not frozen native runtime")
    objective = _mapping(_mapping(spec.get("objectives")).get("offline_rl"))
    if objective.get("dataset_ready") is not True or objective.get("blockers") != []:
        raise ValueError("offline IQL objective is not dataset-ready")

    inventory = _mapping(_mapping(spec.get("inventory")).get("exact_exam"))
    manifest_path = _resolve_locked_path(
        exact_lock.get("manifest_path"), "dataset manifest"
    )
    canonical_manifest_path = _resolve_locked_path(
        exact_lock.get("canonical_labels_manifest_path"),
        "canonical labels manifest",
    )
    split_manifest_path = _resolve_locked_path(
        exact_lock.get("trajectory_split_manifest_path"),
        "trajectory split manifest",
    )
    locked_files = (
        (manifest_path, "manifest_sha256", "dataset manifest"),
        (
            canonical_manifest_path,
            "canonical_labels_manifest_sha256",
            "canonical labels manifest",
        ),
        (
            split_manifest_path,
            "trajectory_split_manifest_sha256",
            "trajectory split manifest",
        ),
    )
    locked_hashes: dict[str, str] = {}
    for path, sha_key, label in locked_files:
        if not path.is_file():
            raise FileNotFoundError(f"offline IQL {label} is missing")
        actual = sha256_file(path)
        if exact_lock.get(sha_key) != actual:
            raise ValueError(f"offline IQL {label} hash mismatch")
        locked_hashes[label] = actual

    dataset_manifest = _read_object(manifest_path)
    manifest_audit = _mapping(dataset_manifest.get("audit"))
    if not (
        dataset_manifest.get("schema") == DATASET_MANIFEST_SCHEMA
        and dataset_manifest.get("train_ready") is True
        and dataset_manifest.get("stages_sha256") == stages_sha
        and dataset_manifest.get("stage_count") == inventory.get("stage_count")
        and dataset_manifest.get("transition_count")
        == inventory.get("transition_count")
        and manifest_audit.get("train_ready") is True
        and manifest_audit.get("blockers") == []
    ):
        raise ValueError("offline IQL frozen dataset manifest contract mismatch")
    canonical_manifest = _read_object(canonical_manifest_path)
    if canonical_manifest.get("schema") != CANONICAL_MANIFEST_SCHEMA:
        raise ValueError("offline IQL canonical labels manifest schema mismatch")
    split_manifest = _read_object(split_manifest_path)
    trajectory_splits = split_manifest.get("trajectory_splits")
    if not (
        split_manifest.get("policy_version") == 1
        and isinstance(trajectory_splits, Mapping)
        and trajectory_splits
        and all(
            isinstance(key, str)
            and key
            and value in SPLIT_CODES
            for key, value in trajectory_splits.items()
        )
    ):
        raise ValueError("offline IQL trajectory split manifest contract mismatch")

    report = _read_object(anchor_report_path)
    if report.get("schema") != EXACT_REPORT_SCHEMA:
        raise ValueError("offline IQL anchor report schema mismatch")
    if _mapping(report.get("artifacts")).get("model_sha256") != anchor_model_sha:
        raise ValueError("offline IQL BC anchor model hash mismatch")
    report_dataset = _mapping(report.get("dataset"))
    if (
        report_dataset.get("stages_sha256") != stages_sha
        or report_dataset.get("spec_sha256") != spec_sha
    ):
        raise ValueError("offline IQL BC anchor is not bound to the frozen inputs")
    acceptance = _mapping(report.get("acceptance"))
    if not (
        acceptance.get("trained") is True
        and acceptance.get("shadow_ready") is True
        and acceptance.get("strict_legal_binding_rate") == 1.0
    ):
        raise ValueError("offline IQL BC anchor is not accepted for shadow")
    anchor_parameters, anchor_metadata, _extras = load_model_artifact(
        anchor_model_path
    )
    if not (
        anchor_metadata.get("schema") == BC_MODEL_SCHEMA
        and anchor_metadata.get("kind") == "exact-main-action-candidate-pointer"
        and anchor_metadata.get("input_contract")
        == "native-state-before+authoritative-ordered-legal-candidates-v1"
        and anchor_metadata.get("guid_policy") == "binding-only"
        and anchor_metadata.get("action_surface") == "main-action-v1"
    ):
        raise ValueError("offline IQL BC anchor model contract mismatch")
    _validate_bc_anchor_arrays(anchor_parameters, anchor_metadata)
    if spec.get("schema") == SPEC_SCOPED_SCHEMA:
        for source in (anchor_metadata, _mapping(report.get("model"))):
            if (source.get("training_scope") != spec["training_scope"]
                    or source.get("training_spec_sha256") != spec_sha or source.get("stages_sha256") != stages_sha):
                raise ValueError("scoped IQL BC anchor scope/input binding mismatch")
    anchor_contract = anchor_metadata.get("card_feature_contract")
    spec_contract = spec.get("card_feature_contract")
    if (anchor_contract is None) != (spec_contract is None) or (
        anchor_contract is not None and any(
            _mapping(anchor_contract).get(key) != _mapping(spec_contract).get(key)
            for key in ("schema", "features_sha256", "snapshot_fingerprint")
        )
    ):
        raise ValueError("offline IQL BC anchor and training spec card feature contracts differ")
    provenance: dict[str, object] = {
        "stages_sha256": stages_sha,
        "training_spec_sha256": spec_sha,
        "bc_anchor_model_sha256": anchor_model_sha,
        "bc_anchor_report_sha256": anchor_report_sha,
        "dataset_manifest_path": str(manifest_path),
        "dataset_manifest_sha256": locked_hashes["dataset manifest"],
        "canonical_labels_manifest_path": str(canonical_manifest_path),
        "canonical_labels_manifest_sha256": locked_hashes[
            "canonical labels manifest"
        ],
        "trajectory_split_manifest_path": str(split_manifest_path),
        "trajectory_split_manifest_sha256": locked_hashes[
            "trajectory split manifest"
        ],
        "trajectory_splits": dict(trajectory_splits),
        "production_frozen_hashes_enforced": enforce_frozen_hashes,
        "spec_manifest_hash_locks_enforced": True,
        "legacy_default_profile_match": legacy_default_profile_match,
    }
    if spec.get("schema") == SPEC_SCOPED_SCHEMA:
        provenance["training_scope"] = dict(spec["training_scope"])
    return spec, report, anchor_parameters, anchor_metadata, provenance


def load_iql_dataset(
    *,
    stages_path: Path = DEFAULT_STAGES,
    spec_path: Path = DEFAULT_SPEC,
    anchor_model_path: Path = DEFAULT_ANCHOR_MODEL,
    anchor_report_path: Path = DEFAULT_ANCHOR_REPORT,
    reward_divisor: float = 10_000.0,
    enforce_frozen_hashes: bool = True,
) -> tuple[IQLDataset, dict[str, object]]:
    """Load and revalidate the exact stage cohort used by discrete IQL."""

    stages_path = Path(stages_path)
    spec_path = Path(spec_path)
    anchor_model_path = Path(anchor_model_path)
    anchor_report_path = Path(anchor_report_path)
    enforce_frozen_hashes = _strict_bool(
        enforce_frozen_hashes, "enforce_frozen_hashes"
    )
    divisor = _number(reward_divisor, "reward_divisor")
    if divisor <= 0:
        raise ValueError("reward_divisor must be positive")
    spec, _report, _anchor, anchor_metadata, provenance = _validate_input_locks(
        stages_path=stages_path,
        spec_path=spec_path,
        anchor_model_path=anchor_model_path,
        anchor_report_path=anchor_report_path,
        enforce_frozen_hashes=enforce_frozen_hashes,
    )
    context_buckets = _positive_bucket_count(
        anchor_metadata.get("context_buckets"), "anchor context_buckets"
    )
    candidate_buckets = _positive_bucket_count(
        anchor_metadata.get("candidate_buckets"), "anchor candidate_buckets"
    )
    semantic_features = load_card_semantic_features(
        anchor_metadata.get("card_feature_contract"), relative_to=anchor_model_path.parent,
    )

    context_rows: list[tuple[int, ...]] = []
    candidate_rows: list[tuple[tuple[int, ...], ...]] = []
    next_context_rows: list[tuple[int, ...]] = []
    actions: list[int] = []
    rewards: list[float] = []
    dones: list[float] = []
    splits: list[int] = []
    flows: list[str] = []
    stages: list[str] = []
    stage_ids: list[str] = []
    trajectories: list[str] = []
    action_kinds: list[str] = []
    stage_start: list[bool] = []
    stage_returns: list[float] = []
    trajectory_splits: dict[str, int] = {}
    stage_count = 0
    source_ids: set[str] = set()
    candidate_counts: Counter[int] = Counter()
    seen_stage_ids: set[str] = set()
    seen_stage_keys: set[tuple[str, str, str]] = set()
    seen_transition_ids: set[tuple[str, int]] = set()
    seen_digest_pairs: set[tuple[str, str]] = set()
    observed_coverage_cells: set[tuple[str, str, str]] = set()
    encoded_split_membership: dict[str, set[str]] = {}
    source_split_membership: dict[str, set[str]] = {}

    with stages_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, Mapping) or row.get("schema") != STAGE_SCHEMA:
                raise ValueError(f"invalid offline IQL stage at line {line_number}")
            flow = row.get("flow")
            stage = row.get("stage")
            stage_id = row.get("stage_id")
            trajectory = row.get("trajectory_id")
            split_name = row.get("split")
            source_id = row.get("source_id")
            transition_rows = row.get("transitions")
            if not (
                isinstance(flow, str)
                and flow
                and isinstance(stage, str)
                and stage in {"Mid1", "Mid2", "Final"}
                and isinstance(stage_id, str)
                and stage_id
                and isinstance(trajectory, str)
                and trajectory
                and split_name in SPLIT_CODES
                and isinstance(source_id, str)
                and source_id
                and isinstance(transition_rows, list)
                and transition_rows
            ):
                raise ValueError(f"incomplete offline IQL stage at line {line_number}")
            if "simulator" in source_id.lower():
                raise ValueError("simulator rows are prohibited from offline IQL")
            stage_key = (trajectory, flow, stage)
            if stage_id in seen_stage_ids:
                raise ValueError("offline IQL dataset contains duplicate stage_id")
            if stage_key in seen_stage_keys:
                raise ValueError(
                    "offline IQL dataset contains duplicate trajectory/flow/stage"
                )
            seen_stage_ids.add(stage_id)
            seen_stage_keys.add(stage_key)
            observed_coverage_cells.add((flow, stage, str(split_name)))
            split_code = SPLIT_CODES[str(split_name)]
            previous_split = trajectory_splits.setdefault(trajectory, split_code)
            if previous_split != split_code:
                raise ValueError("one offline IQL trajectory crosses dataset splits")
            if row.get("transition_count") != len(transition_rows):
                raise ValueError("offline IQL stage transition count mismatch")
            stage_count += 1
            source_ids.add(source_id)
            source_split_membership.setdefault(source_id, set()).add(str(split_name))
            terminal_positions: list[int] = []
            stage_reward = 0.0
            previous_step: int | None = None
            for index, transition in enumerate(transition_rows):
                if not isinstance(transition, Mapping):
                    raise ValueError("offline IQL transition must be an object")
                state_before = transition.get("state_before")
                state_after = transition.get("state_after")
                action = transition.get("action")
                legal = transition.get("legal_candidates")
                terminal = transition.get("terminal")
                step = transition.get("step")
                if not (
                    isinstance(state_before, Mapping)
                    and isinstance(state_after, Mapping)
                    and isinstance(action, Mapping)
                    and isinstance(legal, list)
                    and legal
                    and type(terminal) is bool
                    and isinstance(step, int)
                    and not isinstance(step, bool)
                ):
                    raise ValueError("offline IQL transition fields are incomplete")
                if previous_step is not None and step != previous_step + 1:
                    raise ValueError("offline IQL stage has a step gap")
                previous_step = step
                transition_id = (stage_id, step)
                if transition_id in seen_transition_ids:
                    raise ValueError("offline IQL dataset contains duplicate step identity")
                seen_transition_ids.add(transition_id)
                before_digest = _digest(state_before)
                after_digest = _digest(state_after)
                if (
                    transition.get("before_sha256") != before_digest
                    or transition.get("after_sha256") != after_digest
                ):
                    raise ValueError("offline IQL transition state digest mismatch")
                digest_pair = (before_digest, after_digest)
                if digest_pair in seen_digest_pairs:
                    raise ValueError("offline IQL dataset contains duplicate transition digest")
                seen_digest_pairs.add(digest_pair)
                reward = _number(transition.get("reward"), "transition.reward")
                before_score = _number(state_before.get("score"), "state_before.score")
                after_score = _number(state_after.get("score"), "state_after.score")
                if reward != after_score - before_score:
                    raise ValueError("offline IQL reward is not the exact score delta")
                if reward < 0:
                    raise ValueError("offline IQL v0 does not accept negative score deltas")
                current = encode_exact_observation(
                    state_before=state_before,
                    flow=flow,
                    stage=stage,
                    legal_candidates=legal,
                    context_buckets=context_buckets,
                    candidate_buckets=candidate_buckets,
                    semantic_features=semantic_features,
                    training_scope=spec.get("training_scope"),
                )
                chosen = strict_exact_candidate_index(state_before, action, legal)
                candidate_counts[len(legal)] += 1
                encoded_identity = _digest(
                    {
                        "context_indices": list(current.context_indices),
                        "candidate_indices": [
                            list(value) for value in current.candidate_indices
                        ],
                    }
                )
                encoded_split_membership.setdefault(encoded_identity, set()).add(
                    str(split_name)
                )
                if terminal:
                    terminal_positions.append(index)
                    next_context: tuple[int, ...] = ()
                else:
                    if index + 1 >= len(transition_rows):
                        raise ValueError("offline IQL nonterminal transition has no next row")
                    following = transition_rows[index + 1]
                    if not isinstance(following, Mapping):
                        raise ValueError("offline IQL next transition must be an object")
                    next_before = following.get("state_before")
                    next_legal = following.get("legal_candidates")
                    if not (
                        isinstance(next_before, Mapping)
                        and isinstance(next_legal, list)
                        and next_legal
                    ):
                        raise ValueError("offline IQL next observation is incomplete")
                    if after_digest != _digest(next_before):
                        raise ValueError("offline IQL adjacent state discontinuity")
                    following_before_digest = following.get("before_sha256")
                    if following_before_digest != after_digest:
                        raise ValueError("offline IQL adjacent digest discontinuity")
                    next_context = encode_exact_observation(
                        state_before=next_before,
                        flow=flow,
                        stage=stage,
                        legal_candidates=next_legal,
                        context_buckets=context_buckets,
                        candidate_buckets=candidate_buckets,
                        semantic_features=semantic_features,
                        training_scope=spec.get("training_scope"),
                    ).context_indices
                context_rows.append(current.context_indices)
                candidate_rows.append(current.candidate_indices)
                next_context_rows.append(next_context)
                actions.append(chosen)
                rewards.append(reward / divisor)
                dones.append(float(terminal))
                splits.append(split_code)
                flows.append(flow)
                stages.append(stage)
                stage_ids.append(stage_id)
                trajectories.append(trajectory)
                action_kinds.append(
                    str(action.get("kind", action.get("action_type", "unknown")))
                )
                stage_start.append(index == 0)
                stage_returns.append(_number(row.get("return"), "stage.return"))
                stage_reward += reward
            if terminal_positions != [len(transition_rows) - 1]:
                raise ValueError("offline IQL terminal must occur exactly once at stage end")
            declared_return = _number(row.get("return"), "stage.return")
            if stage_reward != declared_return or row.get("terminal") is not True:
                raise ValueError("offline IQL stage return/terminal mismatch")

    if not actions:
        raise ValueError("offline IQL dataset is empty")
    locked_trajectory_splits = _mapping(provenance.get("trajectory_splits"))
    actual_trajectory_splits = {
        trajectory: next(
            name for name, code in SPLIT_CODES.items() if code == split_code
        )
        for trajectory, split_code in trajectory_splits.items()
    }
    if dict(locked_trajectory_splits) != actual_trajectory_splits:
        raise ValueError("offline IQL rows disagree with trajectory split manifest")
    split_values = np.asarray(splits, dtype=np.int8)
    actual_split_counts = _split_counts(split_values)
    inventory = _mapping(_mapping(spec.get("inventory")).get("exact_exam"))
    objective = _mapping(_mapping(spec.get("objectives")).get("offline_rl"))
    expected_splits = _mapping(inventory.get("split_transition_counts"))
    if (
        inventory.get("transition_count") != len(actions)
        or inventory.get("stage_count") != stage_count
        or dict(expected_splits) != actual_split_counts
        or objective.get("row_level_transitions") != len(actions)
        or _mapping(objective.get("split_counts")) != actual_split_counts
    ):
        raise ValueError("offline IQL cohort does not match the training spec")
    optional_inventory_checks = {
        "trajectory_count": len(trajectory_splits),
        "independent_source_count": len(source_ids),
        "terminal_count": int(sum(dones)),
    }
    for key, actual in optional_inventory_checks.items():
        if key in inventory and inventory.get(key) != actual:
            raise ValueError(f"offline IQL inventory {key} mismatch")
    expected_action_kinds = inventory.get("action_kind_counts")
    actual_action_kinds = dict(sorted(Counter(action_kinds).items()))
    if expected_action_kinds is not None and _mapping(expected_action_kinds) != (
        actual_action_kinds
    ):
        raise ValueError("offline IQL inventory action_kind_counts mismatch")
    if any(actual_split_counts[name] == 0 for name in SPLIT_CODES):
        raise ValueError("offline IQL requires non-empty train/validation/test splits")

    context_indices, context_mask = _pad_token_rows(context_rows)
    candidate_indices, candidate_token_mask, legal_mask = (
        _pad_candidate_token_rows(candidate_rows)
    )
    next_context_indices, next_context_mask = _pad_token_rows(next_context_rows)
    trajectory_counts = Counter(
        zip(splits, trajectories, strict=True)
    )
    flow_counts = Counter(zip(splits, flows, strict=True))
    weights = np.asarray(
        [
            1.0
            / trajectory_counts[(split, trajectory)]
            / math.sqrt(flow_counts[(split, flow)])
            for split, trajectory, flow in zip(
                splits, trajectories, flows, strict=True
            )
        ],
        dtype=np.float32,
    )
    train_mean = float(weights[split_values == SPLIT_CODES["train"]].mean())
    if train_mean <= 0:
        raise ValueError("offline IQL train weights are invalid")
    weights /= train_mean
    flow_names = tuple(sorted(set(flows)))
    flow_lookup = {value: index for index, value in enumerate(flow_names)}
    dataset = IQLDataset(
        context_indices=context_indices,
        context_mask=context_mask,
        candidate_indices=candidate_indices,
        candidate_token_mask=candidate_token_mask,
        legal_mask=legal_mask,
        next_context_indices=next_context_indices,
        next_context_mask=next_context_mask,
        action_indices=np.asarray(actions, dtype=np.int16),
        rewards=np.asarray(rewards, dtype=np.float32),
        dones=np.asarray(dones, dtype=np.float32),
        splits=split_values,
        weights=weights,
        flow_ids=np.asarray([flow_lookup[value] for value in flows], dtype=np.int16),
        flow_names=flow_names,
        flows=tuple(flows),
        stages=tuple(stages),
        stage_ids=tuple(stage_ids),
        trajectory_ids=tuple(trajectories),
        action_kinds=tuple(action_kinds),
        stage_start=np.asarray(stage_start, dtype=np.bool_),
        stage_returns=np.asarray(stage_returns, dtype=np.float32),
        reward_divisor=divisor,
        context_buckets=context_buckets,
        candidate_buckets=candidate_buckets,
    )
    expected_coverage_cells = {
        (flow, stage, split)
        for flow in flow_names
        for stage in ("Mid1", "Mid2", "Final")
        for split in ("train", "validation", "test")
    }
    missing_coverage_cells = sorted(expected_coverage_cells - observed_coverage_cells)
    encoded_cross_split = sorted(
        key for key, memberships in encoded_split_membership.items() if len(memberships) > 1
    )
    source_cross_split = sorted(
        key for key, memberships in source_split_membership.items() if len(memberships) > 1
    )
    if spec.get("schema") == SPEC_SCOPED_SCHEMA and (missing_coverage_cells or source_cross_split):
        raise ValueError("scoped IQL stage coverage or capture source separation failed")
    audit: dict[str, object] = {
        "stage_count": stage_count,
        "transition_count": dataset.size,
        "trajectory_count": len(trajectory_splits),
        "independent_source_count": len(source_ids),
        "split_counts": actual_split_counts,
        "terminal_count": int(dataset.dones.sum()),
        "nonterminal_next_before_count": int((dataset.dones == 0).sum()),
        "action_kind_counts": actual_action_kinds,
        "legal_candidate_count_distribution": {
            str(key): value for key, value in sorted(candidate_counts.items())
        },
        "strict_legal_binding_count": dataset.size,
        "candidate_semantic_collision_rows": 0,
        "candidate_hash_collision_rows": 0,
        "simulator_row_count": 0 if enforce_frozen_hashes else None,
        "simulator_exclusion_status": (
            "verified-frozen-exact-cohort"
            if enforce_frozen_hashes
            else "unverified-custom-cohort"
        ),
        "mode_scope": ["nia_pro"] if enforce_frozen_hashes else ["unverified"],
        "provenance": {
            key: value for key, value in provenance.items() if key != "trajectory_splits"
        },
        "cross_split_overlap": {
            "encoded_observation_candidate_overlap_count": len(encoded_cross_split),
            "encoded_observation_candidate_overlap_sha256": encoded_cross_split,
            "source_id_overlap_count": len(source_cross_split),
            "source_ids": source_cross_split,
            "source_id_overlap_is_leakage": spec.get("schema") == SPEC_SCOPED_SCHEMA,
        },
        "coverage": {
            "expected_flow_stage_split_cell_count": len(expected_coverage_cells),
            "observed_flow_stage_split_cell_count": len(observed_coverage_cells),
            "missing_flow_stage_split_cell_count": len(missing_coverage_cells),
            "missing_flow_stage_split_cells": [list(value) for value in missing_coverage_cells],
        },
        "inference_feature_contract": (
            "state-before+same-state-authoritative-ordered-legal-candidates-only"
        ),
        "next_feature_contract": "next-transition-state-before-after-exact-digest-link",
        "reward_divisor": divisor,
    }
    return dataset, audit


@dataclass(frozen=True, slots=True)
class FrozenBCFeatures:
    context: np.ndarray
    candidate: np.ndarray
    anchor_logits: np.ndarray
    anchor_probabilities: np.ndarray
    next_context: np.ndarray


def _frozen_bc_features(
    dataset: IQLDataset, anchor_parameters: Mapping[str, np.ndarray]
) -> FrozenBCFeatures:
    probabilities, cache = _outer_forward(
        anchor_parameters,
        dataset.context_indices,
        dataset.context_mask,
        dataset.candidate_indices,
        dataset.candidate_token_mask,
        dataset.legal_mask,
        cache=True,
    )
    assert cache is not None
    anchor_logits = (
        cache["h2"] @ anchor_parameters["w3"]
        + float(anchor_parameters["b3"][0])
    )
    anchor_logits = np.where(dataset.legal_mask > 0, anchor_logits, -1.0e9)
    next_context, _denominator = _context_vector(
        anchor_parameters["context_embedding"],
        dataset.next_context_indices,
        dataset.next_context_mask,
    )
    # Terminal rows have no next observation.  Padding index zero can carry a
    # trained embedding, so explicitly erase it even though the TD mask would
    # later remove it.
    next_context = next_context * (1.0 - dataset.dones[:, None])
    return FrozenBCFeatures(
        context=cache["context"].astype(np.float32),
        candidate=cache["h2"].astype(np.float32),
        anchor_logits=anchor_logits.astype(np.float32),
        anchor_probabilities=probabilities.astype(np.float32),
        next_context=next_context.astype(np.float32),
    )


def _masked_softmax(logits: np.ndarray, mask: np.ndarray) -> np.ndarray:
    if logits.shape != mask.shape or logits.ndim != 2:
        raise ValueError("masked softmax logits/mask shape mismatch")
    if not np.isfinite(logits).all() or not np.isfinite(mask).all():
        raise ValueError("masked softmax inputs must be finite")
    if not np.logical_or(mask == 0, mask == 1).all():
        raise ValueError("masked softmax mask must be binary")
    if np.any(mask.sum(axis=1) <= 0):
        raise ValueError("masked softmax row has no legal candidate")
    scores = np.where(mask > 0, logits, -1.0e9)
    maximum = scores.max(axis=1, keepdims=True)
    values = np.exp(scores - maximum) * mask
    return values / np.maximum(values.sum(axis=1, keepdims=True), 1.0e-12)


def _value_forward(
    parameters: Mapping[str, np.ndarray], values: np.ndarray
) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray]]:
    z = values @ parameters["v_w1"] + parameters["v_b1"]
    hidden = np.maximum(z, 0.0)
    output = hidden @ parameters["v_w2"] + float(parameters["v_b2"][0])
    return output, (z, hidden)


def _copy_parameters(
    parameters: Mapping[str, np.ndarray]
) -> dict[str, np.ndarray]:
    return {key: value.copy() for key, value in parameters.items()}


def _require_finite(label: str, *values: object) -> None:
    for value in values:
        if isinstance(value, Mapping):
            _require_finite(label, *value.values())
        elif isinstance(value, np.ndarray):
            if not np.isfinite(value).all():
                raise FloatingPointError(f"offline IQL {label} is non-finite")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(float(value)):
                raise FloatingPointError(f"offline IQL {label} is non-finite")


def _polyak_update(
    target: dict[str, np.ndarray],
    source: Mapping[str, np.ndarray],
    tau: float,
) -> None:
    tau = _number(tau, "target_tau")
    if not 0.0 < tau <= 1.0:
        raise ValueError("target_tau must be in (0, 1]")
    if set(target) != set(source):
        raise ValueError("Polyak parameter keys differ")
    for key in target:
        if target[key].shape != source[key].shape:
            raise ValueError("Polyak parameter shapes differ")
        target[key] *= 1.0 - tau
        target[key] += tau * source[key]
    _require_finite("Polyak target", target)


def _macro_accuracy(
    predictions: np.ndarray,
    targets: np.ndarray,
    flow_ids: np.ndarray,
    flow_names: Sequence[str],
) -> tuple[float, dict[str, float]]:
    per_flow: dict[str, float] = {}
    for index, name in enumerate(flow_names):
        mask = flow_ids == index
        if mask.any():
            per_flow[name] = float((predictions[mask] == targets[mask]).mean())
    return (
        float(np.mean(tuple(per_flow.values()))) if per_flow else 0.0,
        per_flow,
    )


@dataclass(frozen=True, slots=True)
class FlowStageReturnPrior:
    """Fixed train-only mean stage returns used as the Q/V intercept."""

    keys: tuple[tuple[str, str], ...]
    values: np.ndarray
    global_mean: float

    def raw_value(self, flow: str, stage: str) -> float:
        lookup = dict(zip(self.keys, self.values.tolist(), strict=True))
        return float(lookup.get((flow, stage), self.global_mean))


def _return_prior_payload(prior: FlowStageReturnPrior) -> dict[str, object]:
    return {
        "flow_stage_keys": [list(key) for key in prior.keys],
        "mean_returns_raw_score": [float(value) for value in prior.values],
        "global_mean_raw_score": float(prior.global_mean),
    }


def _train_return_prior(dataset: IQLDataset) -> FlowStageReturnPrior:
    train = (
        (dataset.splits == SPLIT_CODES["train"]) & dataset.stage_start
    )
    if not train.any():
        raise ValueError("offline IQL return prior has no train stage starts")
    grouped: dict[tuple[str, str], list[float]] = {}
    for index in np.flatnonzero(train).tolist():
        grouped.setdefault((dataset.flows[index], dataset.stages[index]), []).append(
            float(dataset.stage_returns[index])
        )
    keys = tuple(sorted(grouped))
    values = np.asarray(
        [np.mean(grouped[key]) for key in keys], dtype=np.float32
    )
    global_mean = float(np.float32(dataset.stage_returns[train].mean()))
    if not np.isfinite(values).all() or not math.isfinite(global_mean):
        raise FloatingPointError("offline IQL train return prior is non-finite")
    return FlowStageReturnPrior(keys, values, global_mean)


def _dataset_prior_scaled(
    dataset: IQLDataset,
    prior: FlowStageReturnPrior,
    indices: np.ndarray | None = None,
) -> np.ndarray:
    rows = (
        range(dataset.size)
        if indices is None
        else (int(index) for index in indices.tolist())
    )
    lookup = dict(zip(prior.keys, prior.values.tolist(), strict=True))
    values = np.asarray(
        [
            float(
                lookup.get(
                    (dataset.flows[index], dataset.stages[index]),
                    prior.global_mean,
                )
            )
            / dataset.reward_divisor
            for index in rows
        ],
        dtype=np.float32,
    )
    _require_finite("scaled flow/stage return prior", values)
    return values


def _evaluate(
    *,
    dataset: IQLDataset,
    frozen: FrozenBCFeatures,
    q_parameters: Mapping[str, np.ndarray],
    v_parameters: Mapping[str, np.ndarray],
    actor_parameters: Mapping[str, np.ndarray],
    split_name: str,
    expectile: float,
    return_prior: FlowStageReturnPrior,
) -> dict[str, object]:
    indices = np.flatnonzero(dataset.splits == SPLIT_CODES[split_name])
    context = frozen.context[indices]
    candidates = frozen.candidate[indices]
    legal = dataset.legal_mask[indices]
    action = dataset.action_indices[indices]
    weights = dataset.weights[indices]
    row = np.arange(indices.shape[0])
    selected = candidates[row, action]
    prior_scaled = _dataset_prior_scaled(dataset, return_prior, indices)
    q1_residual = (
        selected @ q_parameters["q1_w"] + float(q_parameters["q1_b"][0])
    )
    q2_residual = (
        selected @ q_parameters["q2_w"] + float(q_parameters["q2_b"][0])
    )
    q1 = prior_scaled + q1_residual
    q2 = prior_scaled + q2_residual
    qbar = np.minimum(q1, q2)
    value_residual, _cache = _value_forward(v_parameters, context)
    value = prior_scaled + value_residual
    next_value_residual, _next_cache = _value_forward(
        v_parameters, frozen.next_context[indices]
    )
    # Every transition remains inside one flow/stage.  On a nonterminal row
    # the next state's absolute V therefore has the same fixed intercept; on
    # a terminal row the TD mask removes it.
    next_value = prior_scaled + next_value_residual
    target = dataset.rewards[indices] + (
        1.0 - dataset.dones[indices]
    ) * next_value
    weight_sum = max(float(weights.sum()), 1.0e-12)
    td_mse = float(
        (
            weights
            * (0.5 * np.square(q1 - target) + 0.5 * np.square(q2 - target))
        ).sum()
        / weight_sum
    )
    residual = qbar - value
    expectile_weight = np.where(residual >= 0, expectile, 1.0 - expectile)
    value_loss = float(
        (weights * expectile_weight * np.square(residual)).sum() / weight_sum
    )
    anchor = frozen.anchor_probabilities[indices]
    logits = frozen.anchor_logits[indices] + (
        candidates @ actor_parameters["actor_w"]
        + float(actor_parameters["actor_b"][0])
    )
    probabilities = _masked_softmax(logits, legal)
    chosen_probability = probabilities[row, action]
    anchor_chosen = anchor[row, action]
    nll = float(
        (weights * -np.log(np.maximum(chosen_probability, 1.0e-12))).sum()
        / weight_sum
    )
    anchor_nll = float(
        (weights * -np.log(np.maximum(anchor_chosen, 1.0e-12))).sum()
        / weight_sum
    )
    kl_rows = (
        anchor
        * (
            np.log(np.maximum(anchor, 1.0e-12))
            - np.log(np.maximum(probabilities, 1.0e-12))
        )
        * legal
    ).sum(axis=1)
    predictions = probabilities.argmax(axis=1)
    anchor_predictions = anchor.argmax(axis=1)
    macro, per_flow = _macro_accuracy(
        predictions,
        action,
        dataset.flow_ids[indices],
        dataset.flow_names,
    )
    anchor_macro, anchor_per_flow = _macro_accuracy(
        anchor_predictions,
        action,
        dataset.flow_ids[indices],
        dataset.flow_names,
    )
    starts = dataset.stage_start[indices]
    if starts.any():
        start_indices = indices[starts]
        predicted_returns = value[starts] * dataset.reward_divisor
        predicted_chosen_q_returns = qbar[starts] * dataset.reward_divisor
        actual_returns = dataset.stage_returns[start_indices]
        value_mae = float(np.abs(predicted_returns - actual_returns).mean())
        chosen_q_mae = float(
            np.abs(predicted_chosen_q_returns - actual_returns).mean()
        )
        baseline_predictions = np.asarray(
            [
                return_prior.raw_value(
                    dataset.flows[index], dataset.stages[index]
                )
                for index in start_indices.tolist()
            ],
            dtype=np.float32,
        )
        baseline_mae = float(np.abs(baseline_predictions - actual_returns).mean())
    else:
        value_mae = None
        chosen_q_mae = None
        baseline_mae = None
    return {
        "split": split_name,
        "count": int(indices.shape[0]),
        "td_mse_scaled": td_mse,
        "expectile_loss_scaled": value_loss,
        "policy_nll": nll,
        "anchor_nll": anchor_nll,
        "top1": float((predictions == action).mean()),
        "anchor_top1": float((anchor_predictions == action).mean()),
        "macro_flow_top1": macro,
        "anchor_macro_flow_top1": anchor_macro,
        "per_flow_top1": per_flow,
        "anchor_per_flow_top1": anchor_per_flow,
        "mean_anchor_kl_nats": float(kl_rows.mean()),
        "p95_anchor_kl_nats": float(np.quantile(kl_rows, 0.95)),
        "stage_start_value_mae_raw_score": value_mae,
        "stage_start_chosen_q_mae_raw_score": chosen_q_mae,
        "train_flow_stage_mean_return_mae_raw_score": baseline_mae,
        "illegal_rate": 0.0,
        "finite": bool(
            np.isfinite(q1).all()
            and np.isfinite(q2).all()
            and np.isfinite(value).all()
            and np.isfinite(probabilities).all()
        ),
    }


@dataclass(frozen=True, slots=True)
class IQLTrainingResult:
    q_parameters: Mapping[str, np.ndarray]
    v_parameters: Mapping[str, np.ndarray]
    actor_parameters: Mapping[str, np.ndarray]
    return_prior: FlowStageReturnPrior
    metrics: Mapping[str, object]


def train_discrete_iql(
    dataset: IQLDataset,
    anchor_parameters: Mapping[str, np.ndarray],
    *,
    epochs: int = 50,
    batch_size: int = 64,
    seed: int = 20260829,
    qv_learning_rate: float = 3.0e-4,
    actor_learning_rate: float = 1.0e-4,
    expectile: float = 0.7,
    inverse_temperature: float = 3.0,
    advantage_clip: float = 100.0,
    anchor_kl_weight: float = 0.1,
    target_tau: float = 0.005,
    actor_warmup_epochs: int = 5,
    patience: int = 10,
    training_callback: Any | None = None,
) -> IQLTrainingResult:
    """Train small heads over a completely frozen accepted BC encoder."""

    epochs = _strict_int(epochs, "epochs", minimum=1)
    batch_size = _strict_int(batch_size, "batch_size", minimum=1)
    seed = _strict_int(seed, "seed", minimum=0)
    actor_warmup_epochs = _strict_int(
        actor_warmup_epochs, "actor_warmup_epochs", minimum=0
    )
    patience = _strict_int(patience, "patience", minimum=1)
    if actor_warmup_epochs >= epochs:
        raise ValueError("actor_warmup_epochs must be smaller than epochs")
    qv_learning_rate = _number(qv_learning_rate, "qv_learning_rate")
    actor_learning_rate = _number(actor_learning_rate, "actor_learning_rate")
    expectile = _number(expectile, "expectile")
    inverse_temperature = _number(inverse_temperature, "inverse_temperature")
    advantage_clip = _number(advantage_clip, "advantage_clip")
    anchor_kl_weight = _number(anchor_kl_weight, "anchor_kl_weight")
    target_tau = _number(target_tau, "target_tau")
    if not (0.5 < expectile < 1.0):
        raise ValueError("offline IQL expectile must be in (0.5, 1)")
    if any(
        value <= 0
        for value in (
            qv_learning_rate,
            actor_learning_rate,
            inverse_temperature,
            advantage_clip,
            target_tau,
        )
    ) or target_tau > 1.0 or anchor_kl_weight < 0:
        raise ValueError("offline IQL hyperparameters are invalid")
    frozen = _frozen_bc_features(dataset, anchor_parameters)
    rng = np.random.default_rng(seed)
    feature_dim = frozen.candidate.shape[2]
    context_dim = frozen.context.shape[1]
    hidden = 64
    # Q/V heads emit residuals.  Both absolute predictions start exactly at
    # the fixed train flow/stage mean: Q residuals are all-zero, while V keeps
    # a random hidden basis with a zero output layer so it is learnable without
    # changing the zero-residual baseline.
    q_parameters = {
        "q1_w": np.zeros(feature_dim, dtype=np.float32),
        "q1_b": np.zeros(1, dtype=np.float32),
        "q2_w": np.zeros(feature_dim, dtype=np.float32),
        "q2_b": np.zeros(1, dtype=np.float32),
    }
    scale = math.sqrt(2.0 / (context_dim + hidden))
    v_parameters = {
        "v_w1": rng.normal(0.0, scale, (context_dim, hidden)).astype(np.float32),
        "v_b1": np.zeros(hidden, dtype=np.float32),
        "v_w2": np.zeros(hidden, dtype=np.float32),
        "v_b2": np.zeros(1, dtype=np.float32),
    }
    actor_parameters = {
        "actor_w": np.zeros(feature_dim, dtype=np.float32),
        "actor_b": np.zeros(1, dtype=np.float32),
    }
    target_q = _copy_parameters(q_parameters)
    q_optimizer = _Adam(q_parameters, qv_learning_rate)
    v_optimizer = _Adam(v_parameters, qv_learning_rate)
    actor_optimizer = _Adam(actor_parameters, actor_learning_rate)
    train_indices = np.flatnonzero(dataset.splits == SPLIT_CODES["train"])
    return_prior = _train_return_prior(dataset)
    prior_scaled_all = _dataset_prior_scaled(dataset, return_prior)
    best_score = float("inf")
    best_epoch = 0
    stale = 0
    best = (
        _copy_parameters(q_parameters),
        _copy_parameters(v_parameters),
        _copy_parameters(actor_parameters),
    )
    history: list[dict[str, object]] = []
    actor_update_steps = 0

    for epoch in range(1, epochs + 1):
        if training_callback is not None:
            training_callback({"phase": "rl", "epoch": epoch, "epochs": epochs})
        epoch_value_losses: list[float] = []
        epoch_q_losses: list[float] = []
        epoch_actor_losses: list[float] = []
        for start in range(0, train_indices.shape[0], batch_size):
            # Recompute the permutation once per epoch at its first batch.
            if start == 0:
                shuffled = rng.permutation(train_indices)
            indices = shuffled[start : start + batch_size]
            row = np.arange(indices.shape[0])
            selected = frozen.candidate[indices][row, dataset.action_indices[indices]]
            weights = dataset.weights[indices]
            weight_sum = max(float(weights.sum()), 1.0e-12)

            prior_scaled = prior_scaled_all[indices]
            target_q1 = prior_scaled + (
                selected @ target_q["q1_w"] + float(target_q["q1_b"][0])
            )
            target_q2 = prior_scaled + (
                selected @ target_q["q2_w"] + float(target_q["q2_b"][0])
            )
            qbar = np.minimum(target_q1, target_q2)
            value_residuals, (value_z, value_hidden) = _value_forward(
                v_parameters, frozen.context[indices]
            )
            values = prior_scaled + value_residuals
            residual = qbar - values
            expectile_weight = np.where(
                residual >= 0, expectile, 1.0 - expectile
            ).astype(np.float32)
            dv = (
                -2.0 * weights * expectile_weight * residual / weight_sum
            ).astype(np.float32)
            value_loss = float(
                (weights * expectile_weight * np.square(residual)).sum()
                / weight_sum
            )
            value_gradients = {
                "v_w2": (value_hidden.T @ dv).astype(np.float32),
                "v_b2": np.asarray([dv.sum()], dtype=np.float32),
            }
            dhidden = dv[:, None] * v_parameters["v_w2"]
            dz = dhidden * (value_z > 0)
            value_gradients["v_w1"] = (
                frozen.context[indices].T @ dz
            ).astype(np.float32)
            value_gradients["v_b1"] = dz.sum(axis=0).astype(np.float32)
            _require_finite(
                "value batch", value_loss, values, residual, value_gradients
            )
            v_optimizer.update(v_parameters, value_gradients, clip_norm=5.0)
            _require_finite("value parameters", v_parameters)
            epoch_value_losses.append(value_loss)

            next_value_residuals, _cache = _value_forward(
                v_parameters, frozen.next_context[indices]
            )
            next_values = prior_scaled + next_value_residuals
            td_target = dataset.rewards[indices] + (
                1.0 - dataset.dones[indices]
            ) * next_values
            q1 = prior_scaled + (
                selected @ q_parameters["q1_w"] + float(q_parameters["q1_b"][0])
            )
            q2 = prior_scaled + (
                selected @ q_parameters["q2_w"] + float(q_parameters["q2_b"][0])
            )
            dq1 = (weights * (q1 - td_target) / weight_sum).astype(np.float32)
            dq2 = (weights * (q2 - td_target) / weight_sum).astype(np.float32)
            q_gradients = {
                "q1_w": (selected.T @ dq1).astype(np.float32),
                "q1_b": np.asarray([dq1.sum()], dtype=np.float32),
                "q2_w": (selected.T @ dq2).astype(np.float32),
                "q2_b": np.asarray([dq2.sum()], dtype=np.float32),
            }
            q_loss = float(
                (
                    weights
                    * (
                        0.5 * np.square(q1 - td_target)
                        + 0.5 * np.square(q2 - td_target)
                    )
                ).sum()
                / weight_sum
            )
            _require_finite(
                "Q batch", q_loss, td_target, q1, q2, q_gradients
            )
            q_optimizer.update(q_parameters, q_gradients, clip_norm=5.0)
            _require_finite("Q parameters", q_parameters)
            epoch_q_losses.append(q_loss)

            if epoch > actor_warmup_epochs:
                advantage = qbar - values
                awr = np.minimum(
                    np.exp(
                        np.clip(
                            inverse_temperature * advantage,
                            -30.0,
                            math.log(advantage_clip),
                        )
                    ),
                    advantage_clip,
                ).astype(np.float32)
                awr_mean = max(float((weights * awr).sum() / weight_sum), 1.0e-6)
                awr /= awr_mean
                candidates = frozen.candidate[indices]
                legal = dataset.legal_mask[indices]
                anchor = frozen.anchor_probabilities[indices]
                logits = frozen.anchor_logits[indices] + (
                    candidates @ actor_parameters["actor_w"]
                    + float(actor_parameters["actor_b"][0])
                )
                probabilities = _masked_softmax(logits, legal)
                one_hot = np.zeros_like(probabilities)
                one_hot[row, dataset.action_indices[indices]] = 1.0
                dlogits = (
                    probabilities * (awr[:, None] + anchor_kl_weight)
                    - awr[:, None] * one_hot
                    - anchor_kl_weight * anchor
                )
                dlogits *= (weights / weight_sum)[:, None]
                dlogits *= legal
                actor_gradients = {
                    "actor_w": np.einsum(
                        "bmh,bm->h", candidates, dlogits
                    ).astype(np.float32),
                    "actor_b": np.asarray([dlogits.sum()], dtype=np.float32),
                }
                chosen_probability = probabilities[
                    row, dataset.action_indices[indices]
                ]
                actor_kl = (
                    anchor
                    * (
                        np.log(np.maximum(anchor, 1.0e-12))
                        - np.log(np.maximum(probabilities, 1.0e-12))
                    )
                    * legal
                ).sum(axis=1)
                actor_loss = float(
                    (
                        weights
                        * (
                            awr
                            * -np.log(np.maximum(chosen_probability, 1.0e-12))
                            + anchor_kl_weight * actor_kl
                        )
                    ).sum()
                    / weight_sum
                )
                _require_finite(
                    "actor batch",
                    actor_loss,
                    advantage,
                    awr,
                    probabilities,
                    actor_gradients,
                )
                actor_optimizer.update(
                    actor_parameters, actor_gradients, clip_norm=5.0
                )
                _require_finite("actor parameters", actor_parameters)
                actor_update_steps += 1
                epoch_actor_losses.append(actor_loss)

            _polyak_update(target_q, q_parameters, target_tau)

        validation = _evaluate(
            dataset=dataset,
            frozen=frozen,
            q_parameters=q_parameters,
            v_parameters=v_parameters,
            actor_parameters=actor_parameters,
            split_name="validation",
            expectile=expectile,
            return_prior=return_prior,
        )
        _require_finite("validation metrics", validation)
        score = float(validation["td_mse_scaled"]) + float(
            validation["expectile_loss_scaled"]
        )
        epoch_value_loss = float(np.mean(epoch_value_losses))
        epoch_q_loss = float(np.mean(epoch_q_losses))
        epoch_actor_loss = (
            None
            if not epoch_actor_losses
            else float(np.mean(epoch_actor_losses))
        )
        _require_finite(
            "epoch",
            score,
            epoch_value_loss,
            epoch_q_loss,
            *(() if epoch_actor_loss is None else (epoch_actor_loss,)),
        )
        if validation.get("finite") is not True:
            raise FloatingPointError("offline IQL validation metrics are non-finite")
        history.append(
            {
                "epoch": epoch,
                "actor_checkpoint_eligible": epoch > actor_warmup_epochs,
                "train_value_loss_scaled": epoch_value_loss,
                "train_q_loss_scaled": epoch_q_loss,
                "train_actor_loss": epoch_actor_loss,
                "validation_selection_loss": score,
                "validation_td_mse_scaled": validation["td_mse_scaled"],
                "validation_expectile_loss_scaled": validation[
                    "expectile_loss_scaled"
                ],
                "validation_policy_nll": validation["policy_nll"],
            }
        )
        if epoch > actor_warmup_epochs and score + 1.0e-8 < best_score:
            best_score = score
            best_epoch = epoch
            best = (
                _copy_parameters(q_parameters),
                _copy_parameters(v_parameters),
                _copy_parameters(actor_parameters),
            )
            stale = 0
        elif epoch > actor_warmup_epochs:
            stale += 1
            if stale >= patience:
                break

    if best_epoch <= actor_warmup_epochs or actor_update_steps <= 0:
        raise RuntimeError("offline IQL produced no post-warmup actor checkpoint")
    q_parameters, v_parameters, actor_parameters = best
    actor_residual_l2 = float(np.linalg.norm(actor_parameters["actor_w"]))
    _require_finite(
        "selected checkpoint",
        q_parameters,
        v_parameters,
        actor_parameters,
        actor_residual_l2,
    )
    if actor_residual_l2 <= 1.0e-12:
        raise RuntimeError("offline IQL actor residual remained zero")
    metrics = {
        "best_epoch": best_epoch,
        "actor_warmup_epochs": actor_warmup_epochs,
        "actor_update_steps": actor_update_steps,
        "actor_residual_l2": actor_residual_l2,
        "history": history,
        "train": _evaluate(
            dataset=dataset,
            frozen=frozen,
            q_parameters=q_parameters,
            v_parameters=v_parameters,
            actor_parameters=actor_parameters,
            split_name="train",
            expectile=expectile,
            return_prior=return_prior,
        ),
        "validation": _evaluate(
            dataset=dataset,
            frozen=frozen,
            q_parameters=q_parameters,
            v_parameters=v_parameters,
            actor_parameters=actor_parameters,
            split_name="validation",
            expectile=expectile,
            return_prior=return_prior,
        ),
        "test": _evaluate(
            dataset=dataset,
            frozen=frozen,
            q_parameters=q_parameters,
            v_parameters=v_parameters,
            actor_parameters=actor_parameters,
            split_name="test",
            expectile=expectile,
            return_prior=return_prior,
        ),
    }
    _require_finite("selected metrics", metrics)
    if any(_mapping(metrics.get(name)).get("finite") is not True for name in SPLIT_CODES):
        raise FloatingPointError("offline IQL selected metrics are non-finite")
    return IQLTrainingResult(
        q_parameters=q_parameters,
        v_parameters=v_parameters,
        actor_parameters=actor_parameters,
        return_prior=return_prior,
        metrics=metrics,
    )


def _deterministic_npz_bytes(arrays: Mapping[str, np.ndarray]) -> bytes:
    """Create an NPZ with stable member ordering and timestamps."""

    output = io.BytesIO()
    with zipfile.ZipFile(
        output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for key in sorted(arrays):
            payload = io.BytesIO()
            np.lib.format.write_array(
                payload, np.asanyarray(arrays[key]), allow_pickle=False
            )
            info = zipfile.ZipInfo(f"{key}.npy", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload.getvalue(), compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()


def _atomic_write_artifact_pair(
    *,
    model_path: Path,
    model_bytes: bytes,
    report_path: Path,
    report_bytes: bytes,
) -> None:
    """Publish a prebuilt model/report pair and roll back a partial failure."""

    model_path = Path(model_path)
    report_path = Path(report_path)
    previous_model = model_path.read_bytes() if model_path.is_file() else None
    previous_report = report_path.read_bytes() if report_path.is_file() else None
    try:
        atomic_write(model_path, model_bytes)
        atomic_write(report_path, report_bytes)
    except Exception:
        try:
            if previous_model is None:
                model_path.unlink(missing_ok=True)
            else:
                atomic_write(model_path, previous_model)
            if previous_report is None:
                report_path.unlink(missing_ok=True)
            else:
                atomic_write(report_path, previous_report)
        finally:
            raise


def _model_arrays(
    *,
    anchor_parameters: Mapping[str, np.ndarray],
    result: IQLTrainingResult,
    metadata: Mapping[str, object],
) -> dict[str, np.ndarray]:
    arrays = {
        f"anchor_{key}": value.copy() for key, value in anchor_parameters.items()
    }
    arrays.update({key: value.copy() for key, value in result.q_parameters.items()})
    arrays.update({key: value.copy() for key, value in result.v_parameters.items()})
    arrays.update({key: value.copy() for key, value in result.actor_parameters.items()})
    arrays["return_prior_values"] = result.return_prior.values.copy()
    arrays["return_prior_global_mean"] = np.asarray(
        [result.return_prior.global_mean], dtype=np.float32
    )
    arrays["metadata_json"] = np.asarray(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True), dtype=np.str_
    )
    return arrays


def load_offline_rl_model(
    path: Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    path = Path(path)
    actual_sha256 = sha256_file(path)
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise ValueError("offline IQL expected model SHA-256 is invalid")
        if actual_sha256 != expected_sha256:
            raise ValueError("offline IQL model SHA-256 mismatch")
    with np.load(path, allow_pickle=False) as archive:
        if "metadata_json" not in archive.files:
            raise ValueError("offline IQL artifact has no metadata")
        metadata = json.loads(str(archive["metadata_json"].item()))
        if not isinstance(metadata, dict) or not (
            metadata.get("schema") == MODEL_SCHEMA
            and metadata.get("kind") == MODEL_KIND
        ):
            raise ValueError("offline IQL artifact metadata mismatch")
        arrays = {
            key: archive[key].copy()
            for key in archive.files
            if key != "metadata_json"
        }
    _validate_offline_rl_model_arrays(arrays, metadata)
    contract = metadata.get("card_feature_contract")
    validate_card_feature_encoding(contract, metadata.get("candidate_encoding"))
    features = load_card_semantic_features(contract, relative_to=path.parent)
    from .behavior_cloning import validate_current_state_feature_metadata
    validate_current_state_feature_metadata(features, metadata)
    return arrays, metadata


def _validate_offline_rl_model_arrays(
    arrays: Mapping[str, np.ndarray], metadata: Mapping[str, Any]
) -> None:
    if not (
        metadata.get("input_contract")
        == "native-state-before+authoritative-ordered-legal-candidates-v1"
        and metadata.get("action_surface") == "main-action-v1"
        and metadata.get("guid_policy") == "binding-only"
        and metadata.get("shadow_only") is True
        and metadata.get("runtime_promotion_allowed") is False
        and metadata.get("qv_output_contract")
        == "fixed-train-flow-stage-return-prior-plus-learned-residual-v1"
        and metadata.get("return_prior_training_split") == "train-only"
        and metadata.get("return_prior_units") == "raw-stage-return"
        and metadata.get("return_prior_fallback") == "train-global-mean"
    ):
        raise ValueError("offline IQL model inference contract mismatch")
    context_buckets = _positive_bucket_count(
        metadata.get("context_buckets"), "model context_buckets"
    )
    candidate_buckets = _positive_bucket_count(
        metadata.get("candidate_buckets"), "model candidate_buckets"
    )
    gamma = _number(metadata.get("gamma"), "model gamma")
    reward_divisor = _number(
        metadata.get("reward_divisor"), "model reward_divisor"
    )
    expectile = _number(metadata.get("expectile"), "model expectile")
    inverse_temperature = _number(
        metadata.get("inverse_temperature"), "model inverse_temperature"
    )
    advantage_clip = _number(
        metadata.get("advantage_clip"), "model advantage_clip"
    )
    anchor_kl_weight = _number(
        metadata.get("anchor_kl_weight"), "model anchor_kl_weight"
    )
    target_tau = _number(metadata.get("target_tau"), "model target_tau")
    qv_learning_rate = _number(
        metadata.get("qv_learning_rate"), "model qv_learning_rate"
    )
    actor_learning_rate = _number(
        metadata.get("actor_learning_rate"), "model actor_learning_rate"
    )
    max_epochs = _strict_int(metadata.get("max_epochs"), "model max_epochs", minimum=1)
    _strict_int(metadata.get("batch_size"), "model batch_size", minimum=1)
    _strict_int(metadata.get("seed"), "model seed", minimum=0)
    warmup = _strict_int(
        metadata.get("actor_warmup_epochs"),
        "model actor_warmup_epochs",
        minimum=0,
    )
    _strict_int(metadata.get("patience"), "model patience", minimum=1)
    if not (
        gamma == 1.0
        and reward_divisor > 0
        and 0.5 < expectile < 1.0
        and inverse_temperature > 0
        and advantage_clip > 0
        and anchor_kl_weight >= 0
        and 0 < target_tau <= 1
        and qv_learning_rate > 0
        and actor_learning_rate > 0
        and warmup < max_epochs
    ):
        raise ValueError("offline IQL model hyperparameter contract mismatch")
    required = {
        "anchor_context_embedding",
        "anchor_candidate_embedding",
        "anchor_w1",
        "anchor_b1",
        "anchor_w2",
        "anchor_b2",
        "anchor_w3",
        "anchor_b3",
        "q1_w",
        "q1_b",
        "q2_w",
        "q2_b",
        "v_w1",
        "v_b1",
        "v_w2",
        "v_b2",
        "actor_w",
        "actor_b",
        "return_prior_values",
        "return_prior_global_mean",
    }
    if set(arrays) != required:
        raise ValueError("offline IQL model required arrays mismatch")
    if any(
        not isinstance(value, np.ndarray)
        or value.dtype.kind != "f"
        or not np.isfinite(value).all()
        for value in arrays.values()
    ):
        raise ValueError("offline IQL model arrays must be finite floating arrays")
    raw_prior_keys = metadata.get("return_prior_flow_stage_keys")
    if not isinstance(raw_prior_keys, list) or not raw_prior_keys:
        raise ValueError("offline IQL return prior keys are missing")
    prior_keys: list[tuple[str, str]] = []
    for raw_key in raw_prior_keys:
        if not (
            isinstance(raw_key, list)
            and len(raw_key) == 2
            and isinstance(raw_key[0], str)
            and raw_key[0] in _validated_training_flows(metadata.get("training_scope"))
            and isinstance(raw_key[1], str)
            and raw_key[1] in {"Mid1", "Mid2", "Final"}
        ):
            raise ValueError("offline IQL return prior key is invalid")
        prior_keys.append((raw_key[0], raw_key[1]))
    if prior_keys != sorted(set(prior_keys)):
        raise ValueError("offline IQL return prior keys are not unique/sorted")
    prior_values = arrays["return_prior_values"]
    prior_global = arrays["return_prior_global_mean"]
    if (
        prior_values.shape != (len(prior_keys),)
        or prior_global.shape != (1,)
        or (prior_values < 0).any()
        or float(prior_global[0]) < 0
    ):
        raise ValueError("offline IQL return prior array shape/value mismatch")
    prior = FlowStageReturnPrior(
        tuple(prior_keys), prior_values, float(prior_global[0])
    )
    prior_sha256 = metadata.get("return_prior_sha256")
    if not (
        isinstance(prior_sha256, str)
        and len(prior_sha256) == 64
        and prior_sha256 == _digest(_return_prior_payload(prior))
    ):
        raise ValueError("offline IQL return prior metadata/array hash mismatch")
    context_embedding = arrays["anchor_context_embedding"]
    candidate_embedding = arrays["anchor_candidate_embedding"]
    if context_embedding.ndim != 2 or candidate_embedding.ndim != 2:
        raise ValueError("offline IQL anchor embeddings must be matrices")
    embedding_dim = context_embedding.shape[1]
    if (
        context_embedding.shape[0] != context_buckets
        or candidate_embedding.shape != (candidate_buckets, embedding_dim)
        or arrays["anchor_w1"].ndim != 2
        or arrays["anchor_w1"].shape[0] != embedding_dim * 3
    ):
        raise ValueError("offline IQL anchor embedding/trunk shape mismatch")
    hidden1 = arrays["anchor_w1"].shape[1]
    if arrays["anchor_b1"].shape != (hidden1,):
        raise ValueError("offline IQL anchor first hidden shape mismatch")
    if arrays["anchor_w2"].ndim != 2 or arrays["anchor_w2"].shape[0] != hidden1:
        raise ValueError("offline IQL anchor second hidden shape mismatch")
    hidden2 = arrays["anchor_w2"].shape[1]
    expected_hidden2 = (hidden2,)
    if (
        arrays["anchor_b2"].shape != expected_hidden2
        or arrays["anchor_w3"].shape != expected_hidden2
        or arrays["anchor_b3"].shape != (1,)
        or arrays["actor_w"].shape != expected_hidden2
        or arrays["actor_b"].shape != (1,)
        or arrays["q1_w"].shape != expected_hidden2
        or arrays["q2_w"].shape != expected_hidden2
        or arrays["q1_b"].shape != (1,)
        or arrays["q2_b"].shape != (1,)
    ):
        raise ValueError("offline IQL actor/Q shape mismatch")
    if arrays["v_w1"].ndim != 2 or arrays["v_w1"].shape[0] != embedding_dim:
        raise ValueError("offline IQL value input shape mismatch")
    value_hidden = arrays["v_w1"].shape[1]
    if (
        arrays["v_b1"].shape != (value_hidden,)
        or arrays["v_w2"].shape != (value_hidden,)
        or arrays["v_b2"].shape != (1,)
    ):
        raise ValueError("offline IQL value hidden shape mismatch")


def score_offline_rl_candidates(
    model_path: Path,
    *,
    state_before: Mapping[str, Any],
    flow: str,
    stage: str,
    legal_candidates: Sequence[object],
    expected_model_sha256: str | None = None,
    report_path: Path | None = None,
    expected_report_sha256: str | None = None,
) -> tuple[float, ...]:
    """Score only the caller-supplied authoritative candidates."""

    if report_path is None and expected_report_sha256 is not None:
        raise ValueError("expected_report_sha256 requires report_path")
    actual_model_sha256 = sha256_file(Path(model_path))
    arrays, metadata = load_offline_rl_model(
        model_path, expected_sha256=expected_model_sha256
    )
    if report_path is not None:
        report_path = Path(report_path)
        actual_report_sha256 = sha256_file(report_path)
        if expected_report_sha256 is not None and (
            not isinstance(expected_report_sha256, str)
            or len(expected_report_sha256) != 64
            or expected_report_sha256 != actual_report_sha256
        ):
            raise ValueError("offline IQL report SHA-256 mismatch")
        report = _read_object(report_path)
        if not (
            report.get("schema") == REPORT_SCHEMA
            and _mapping(report.get("artifacts")).get("model_sha256")
            == actual_model_sha256
        ):
            raise ValueError("offline IQL report/model binding mismatch")
    observation = encode_exact_observation(
        state_before=state_before,
        flow=flow,
        stage=stage,
        legal_candidates=legal_candidates,
        context_buckets=_positive_bucket_count(
            metadata.get("context_buckets"), "model context_buckets"
        ),
        candidate_buckets=_positive_bucket_count(
            metadata.get("candidate_buckets"), "model candidate_buckets"
        ),
        semantic_features=load_card_semantic_features(
            metadata.get("card_feature_contract"), relative_to=Path(model_path).parent,
        ),
        training_scope=metadata.get("training_scope"),
    )
    context_indices, context_mask = _pad_token_rows(
        [observation.context_indices]
    )
    candidate_indices, candidate_token_mask, legal_mask = (
        _pad_candidate_token_rows([observation.candidate_indices])
    )
    anchor = {
        key.removeprefix("anchor_"): value
        for key, value in arrays.items()
        if key.startswith("anchor_")
    }
    _anchor_probabilities, cache = _outer_forward(
        anchor,
        context_indices,
        context_mask,
        candidate_indices,
        candidate_token_mask,
        legal_mask,
        cache=True,
    )
    assert cache is not None
    anchor_logits = (
        cache["h2"] @ anchor["w3"] + float(anchor["b3"][0])
    )
    prior_keys = tuple(
        (str(raw[0]), str(raw[1]))
        for raw in metadata["return_prior_flow_stage_keys"]
    )
    return_prior = FlowStageReturnPrior(
        prior_keys,
        arrays["return_prior_values"],
        float(arrays["return_prior_global_mean"][0]),
    )
    prior_scaled = return_prior.raw_value(flow, stage) / float(
        metadata["reward_divisor"]
    )
    # Materialize the absolute critic/value predictions at inference too.  The
    # flow/stage prior is a common candidate offset, so it cancels from action
    # ordering and Q-V advantages; nevertheless resolving and adding it here is
    # part of the artifact contract and is kept hash-bound above.
    q1 = prior_scaled + (
        cache["h2"] @ arrays["q1_w"] + float(arrays["q1_b"][0])
    )
    q2 = prior_scaled + (
        cache["h2"] @ arrays["q2_w"] + float(arrays["q2_b"][0])
    )
    value_residual, _value_cache = _value_forward(
        {key: arrays[key] for key in ("v_w1", "v_b1", "v_w2", "v_b2")},
        cache["context"],
    )
    value = prior_scaled + value_residual
    _require_finite("artifact inference Q/V", q1, q2, value)
    logits = anchor_logits + (
        cache["h2"] @ arrays["actor_w"] + float(arrays["actor_b"][0])
    )
    probabilities = _masked_softmax(logits, legal_mask)
    return tuple(float(value) for value in probabilities[0])


def _acceptance(
    metrics: Mapping[str, object], loader_audit: Mapping[str, object]
) -> dict[str, object]:
    validation = _mapping(metrics.get("validation"))
    safety_blockers: list[str] = []
    iql_blockers: list[str] = []
    if validation.get("finite") is not True:
        safety_blockers.append("validation-nonfinite")
    if float(validation.get("illegal_rate", 1.0)) != 0.0:
        safety_blockers.append("validation-illegal-rate-nonzero")
    if float(validation.get("mean_anchor_kl_nats", float("inf"))) > 0.15:
        safety_blockers.append("validation-mean-anchor-kl-above-0.15")
    if float(validation.get("p95_anchor_kl_nats", float("inf"))) > 0.5:
        safety_blockers.append("validation-p95-anchor-kl-above-0.5")
    if float(validation.get("policy_nll", float("inf"))) > float(
        validation.get("anchor_nll", 0.0)
    ) + 0.10:
        safety_blockers.append("validation-nll-drop-vs-anchor-above-0.10")
    measured = _mapping(validation.get("per_flow_top1"))
    anchor = _mapping(validation.get("anchor_per_flow_top1"))
    maximum_drop = max(
        (
            float(anchor.get(flow, 0.0)) - float(value)
            for flow, value in measured.items()
        ),
        default=0.0,
    )
    if maximum_drop > 0.05:
        safety_blockers.append("validation-per-flow-top1-drop-above-5pp")
    # The logged trajectory return is a target for the chosen action's
    # Q(s,a), not for IQL's expectile V(s).  Keep V MAE as a diagnostic, but
    # gate the causal quantity that owns the observed action and return.
    chosen_q_mae = validation.get("stage_start_chosen_q_mae_raw_score")
    baseline_mae = validation.get("train_flow_stage_mean_return_mae_raw_score")
    if not (
        isinstance(chosen_q_mae, (int, float))
        and not isinstance(chosen_q_mae, bool)
        and math.isfinite(float(chosen_q_mae))
        and isinstance(baseline_mae, (int, float))
        and not isinstance(baseline_mae, bool)
        and math.isfinite(float(baseline_mae))
    ):
        iql_blockers.append("validation-stage-start-chosen-q-mae-unavailable")
    elif float(chosen_q_mae) > float(baseline_mae):
        iql_blockers.append(
            "validation-stage-start-chosen-q-mae-above-train-baseline"
        )
    best_epoch = metrics.get("best_epoch")
    warmup = metrics.get("actor_warmup_epochs")
    actor_steps = metrics.get("actor_update_steps")
    residual_l2 = metrics.get("actor_residual_l2")
    if not (
        type(best_epoch) is int
        and type(warmup) is int
        and best_epoch > warmup >= 0
    ):
        iql_blockers.append("no-post-warmup-checkpoint")
    if type(actor_steps) is not int or actor_steps <= 0:
        iql_blockers.append("actor-update-steps-zero")
    if not (
        isinstance(residual_l2, (int, float))
        and not isinstance(residual_l2, bool)
        and math.isfinite(float(residual_l2))
        and float(residual_l2) > 1.0e-12
    ):
        iql_blockers.append("actor-residual-zero-or-nonfinite")
    overlap = _mapping(loader_audit.get("cross_split_overlap"))
    if int(overlap.get("encoded_observation_candidate_overlap_count", -1)) != 0:
        iql_blockers.append("encoded-observation-candidate-cross-split-overlap")
    provenance = _mapping(loader_audit.get("provenance"))
    if provenance.get("production_frozen_hashes_enforced") is not True:
        safety_blockers.append("input-provenance-unverified")
        iql_blockers.append("input-provenance-unverified")
    safe_to_shadow = not safety_blockers
    iql_validated = not iql_blockers
    return {
        "trained": True,
        "exact_cohort_only": True,
        "strict_legal_binding_rate": 1.0,
        "illegal_rate_zero": validation.get("illegal_rate") == 0.0,
        "maximum_validation_per_flow_top1_drop_pp": 100.0 * maximum_drop,
        "safe_to_shadow": safe_to_shadow,
        "safety_blockers": safety_blockers,
        "iql_validated": iql_validated,
        "iql_validation_blockers": iql_blockers,
        "shadow_ready": safe_to_shadow and iql_validated,
        "shadow_blockers": [*safety_blockers, *iql_blockers],
        "runtime_promotion_allowed": False,
        "causal_limitations": [
            "Q is identified only on logged main-action-v1 actions",
            "secondary-effect-card-select-v1 is parent-bound auxiliary evidence and is excluded",
            "the shadow scorer cannot replace or bypass the formal controller",
        ],
        "generalization_claimed": False,
    }


def train_discrete_iql_artifact(
    *,
    stages_path: Path = DEFAULT_STAGES,
    spec_path: Path = DEFAULT_SPEC,
    anchor_model_path: Path = DEFAULT_ANCHOR_MODEL,
    anchor_report_path: Path = DEFAULT_ANCHOR_REPORT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    epochs: int = 50,
    batch_size: int = 64,
    seed: int = 20260829,
    reward_divisor: float = 10_000.0,
    qv_learning_rate: float = 3.0e-4,
    actor_learning_rate: float = 1.0e-4,
    expectile: float = 0.7,
    inverse_temperature: float = 3.0,
    advantage_clip: float = 100.0,
    anchor_kl_weight: float = 0.1,
    target_tau: float = 0.005,
    actor_warmup_epochs: int = 5,
    patience: int = 10,
    enforce_frozen_hashes: bool = True,
    acknowledge_stale_learner_gate: bool = False,
    training_callback: Any | None = None,
) -> dict[str, object]:
    acknowledge_stale_learner_gate = _strict_bool(
        acknowledge_stale_learner_gate,
        "acknowledge_stale_learner_gate",
    )
    enforce_frozen_hashes = _strict_bool(
        enforce_frozen_hashes, "enforce_frozen_hashes"
    )
    epochs = _strict_int(epochs, "epochs", minimum=1)
    batch_size = _strict_int(batch_size, "batch_size", minimum=1)
    seed = _strict_int(seed, "seed", minimum=0)
    actor_warmup_epochs = _strict_int(
        actor_warmup_epochs, "actor_warmup_epochs", minimum=0
    )
    patience = _strict_int(patience, "patience", minimum=1)
    if actor_warmup_epochs >= epochs:
        raise ValueError("actor_warmup_epochs must be smaller than epochs")
    reward_divisor = _number(reward_divisor, "reward_divisor")
    qv_learning_rate = _number(qv_learning_rate, "qv_learning_rate")
    actor_learning_rate = _number(actor_learning_rate, "actor_learning_rate")
    expectile = _number(expectile, "expectile")
    inverse_temperature = _number(inverse_temperature, "inverse_temperature")
    advantage_clip = _number(advantage_clip, "advantage_clip")
    anchor_kl_weight = _number(anchor_kl_weight, "anchor_kl_weight")
    target_tau = _number(target_tau, "target_tau")
    spec_preview = _read_object(Path(spec_path))
    learner_gate = _mapping(
        _mapping(spec_preview.get("objectives")).get("offline_rl")
    ).get("learner_enabled")
    capability_warnings = []
    if learner_gate is False:
        if not acknowledge_stale_learner_gate:
            raise RuntimeError("offline IQL legacy training spec requires acknowledge_stale_learner_gate=True")
        capability_warnings.append(
            "training-spec-offline-rl-learner-enabled-false-is-stale-capability-status"
        )
    elif learner_gate is not True:
        raise ValueError("training spec offline_rl learner_enabled is invalid")
    dataset, loader_audit = load_iql_dataset(
        stages_path=stages_path,
        spec_path=spec_path,
        anchor_model_path=anchor_model_path,
        anchor_report_path=anchor_report_path,
        reward_divisor=reward_divisor,
        enforce_frozen_hashes=enforce_frozen_hashes,
    )
    # The loader already verified every expensive input hash.  Reload only the
    # small in-memory anchor arrays; do not rescan the 1.8 GiB stages file.
    anchor_parameters, anchor_metadata, _extras = load_model_artifact(
        Path(anchor_model_path)
    )
    verified_hashes = _mapping(_mapping(loader_audit.get("provenance")))
    result = train_discrete_iql(
        dataset,
        anchor_parameters,
        epochs=epochs,
        batch_size=batch_size,
        seed=seed,
        qv_learning_rate=qv_learning_rate,
        actor_learning_rate=actor_learning_rate,
        expectile=expectile,
        inverse_temperature=inverse_temperature,
        advantage_clip=advantage_clip,
        anchor_kl_weight=anchor_kl_weight,
        target_tau=target_tau,
        actor_warmup_epochs=actor_warmup_epochs,
        patience=patience,
        training_callback=training_callback,
    )
    output_root = Path(output_root)
    model_path = output_root / "exact_main_action_iql_model.npz"
    report_path = output_root / "exact_main_action_iql_report.json"
    model_feature_contract = copy_card_feature_contract(
        anchor_metadata.get("card_feature_contract"), output_root,
        relative_to=Path(anchor_model_path).parent,
    )
    return_prior_payload = _return_prior_payload(result.return_prior)
    metadata: dict[str, object] = {
        "schema": MODEL_SCHEMA,
        "kind": MODEL_KIND,
        "algorithm": "discrete-IQL",
        "action_surface": "main-action-v1",
        "input_contract": (
            "native-state-before+authoritative-ordered-legal-candidates-v1"
        ),
        "next_state_contract": "next-transition-state-before-only",
        "guid_policy": "binding-only",
        "context_buckets": dataset.context_buckets,
        "candidate_buckets": dataset.candidate_buckets,
        "candidate_encoding": load_card_semantic_features(model_feature_contract, relative_to=output_root).encoding if model_feature_contract is not None else "field-token-bag-v1",
        "frozen_bc_encoder": True,
        "actor": "frozen-bc-logits-plus-zero-init-linear-residual",
        "critic": (
            "fixed-train-flow-stage-return-prior-plus-"
            "twin-linear-residual-q-on-frozen-bc-h2"
        ),
        "value": (
            "fixed-train-flow-stage-return-prior-plus-context-mlp-64-residual"
        ),
        "qv_output_contract": (
            "fixed-train-flow-stage-return-prior-plus-learned-residual-v1"
        ),
        "return_prior_training_split": "train-only",
        "return_prior_units": "raw-stage-return",
        "return_prior_fallback": "train-global-mean",
        "return_prior_flow_stage_keys": return_prior_payload["flow_stage_keys"],
        "return_prior_sha256": _digest(return_prior_payload),
        "gamma": 1.0,
        "reward_divisor": reward_divisor,
        "expectile": expectile,
        "inverse_temperature": inverse_temperature,
        "advantage_clip": advantage_clip,
        "anchor_kl_weight": anchor_kl_weight,
        "target_tau": target_tau,
        "qv_learning_rate": qv_learning_rate,
        "actor_learning_rate": actor_learning_rate,
        "batch_size": batch_size,
        "max_epochs": epochs,
        "actor_warmup_epochs": actor_warmup_epochs,
        "patience": patience,
        "seed": seed,
        "stages_sha256": verified_hashes.get("stages_sha256"),
        "training_spec_sha256": verified_hashes.get("training_spec_sha256"),
        "bc_anchor_model_sha256": verified_hashes.get("bc_anchor_model_sha256"),
        "bc_anchor_report_sha256": verified_hashes.get("bc_anchor_report_sha256"),
        "bc_anchor_kind": anchor_metadata.get("kind"),
        "shadow_only": True,
        "default_enabled": False,
        "runtime_promotion_allowed": False,
    }
    if model_feature_contract is not None:
        metadata["card_feature_contract"] = model_feature_contract
    current_state_schema = getattr(load_card_semantic_features(
        model_feature_contract, relative_to=output_root), "current_state_schema", None)
    if current_state_schema is not None:
        metadata["current_state_feature_schema"] = current_state_schema
    if spec_preview.get("schema") == SPEC_SCOPED_SCHEMA:
        metadata["training_scope"] = dict(spec_preview["training_scope"])
    model_arrays = _model_arrays(
        anchor_parameters=anchor_parameters,
        result=result,
        metadata=metadata,
    )
    _validate_offline_rl_model_arrays(
        {key: value for key, value in model_arrays.items() if key != "metadata_json"},
        metadata,
    )
    model_bytes = _deterministic_npz_bytes(model_arrays)
    model_sha256 = _sha256_bytes(model_bytes)
    acceptance = _acceptance(result.metrics, loader_audit)
    verified_mode = _mapping(loader_audit).get("mode_scope") == ["nia_pro"]
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "algorithm": {
            "primary": "discrete-IQL",
            "cql_baseline_run": False,
            "cql_status": "deferred",
            "cql_deferred_reason": "small-cohort-shadow-v0",
        },
        "scope": {
            "mode": "nia_pro-only" if verified_mode else "unverified-custom-cohort",
            "action_surface": "main-action-v1",
            "secondary_action_surface": "secondary-effect-card-select-v1-excluded",
            "formal_controller_replacement_allowed": False,
            "causal_claim": "logged-main-action-support-only",
        },
        "dataset": {
            "stages_path": str(stages_path),
            "stages_sha256": verified_hashes.get("stages_sha256"),
            "spec_path": str(spec_path),
            "spec_sha256": verified_hashes.get("training_spec_sha256"),
            **loader_audit,
        },
        "anchor": {
            "model_path": str(anchor_model_path),
            "model_sha256": verified_hashes.get("bc_anchor_model_sha256"),
            "report_path": str(anchor_report_path),
            "report_sha256": verified_hashes.get("bc_anchor_report_sha256"),
            "frozen": True,
        },
        "model": {**metadata, "path": str(model_path)},
        "metrics": result.metrics,
        "acceptance": acceptance,
        "capability_warnings": capability_warnings,
        "stale_learner_gate_acknowledged": True,
        "default_enabled": False,
        "shadow_only": True,
        "runtime_promotion_allowed": False,
        "artifacts": {"model_sha256": model_sha256},
    }
    report["report_content_sha256"] = _digest(report)
    report_bytes = canonical_json_bytes(report) + b"\n"
    _atomic_write_artifact_pair(
        model_path=model_path,
        model_bytes=model_bytes,
        report_path=report_path,
        report_bytes=report_bytes,
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train a shadow-only Exact Exam discrete-IQL artifact"
    )
    parser.add_argument("--stages", type=Path, default=DEFAULT_STAGES)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--anchor-model", type=Path, default=DEFAULT_ANCHOR_MODEL)
    parser.add_argument("--anchor-report", type=Path, default=DEFAULT_ANCHOR_REPORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--seed", type=int, default=20260829)
    parser.add_argument(
        "--acknowledge-stale-learner-gate",
        action="store_true",
        help="acknowledge that learner_enabled=false is stale capability metadata",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = train_discrete_iql_artifact(
        stages_path=args.stages,
        spec_path=args.spec,
        anchor_model_path=args.anchor_model,
        anchor_report_path=args.anchor_report,
        output_root=args.output,
        epochs=args.epochs,
        batch_size=args.batch_size,
        seed=args.seed,
        acknowledge_stale_learner_gate=args.acknowledge_stale_learner_gate,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


__all__ = [
    "DEFAULT_ANCHOR_MODEL",
    "DEFAULT_ANCHOR_REPORT",
    "DEFAULT_OUTPUT_ROOT",
    "DEFAULT_SPEC",
    "DEFAULT_STAGES",
    "EncodedExactObservation",
    "IQLDataset",
    "IQLTrainingResult",
    "MODEL_KIND",
    "MODEL_SCHEMA",
    "REPORT_SCHEMA",
    "FROZEN_ANCHOR_MODEL_SHA256",
    "FROZEN_ANCHOR_REPORT_SHA256",
    "FROZEN_SPEC_SHA256",
    "FROZEN_STAGES_SHA256",
    "encode_exact_observation",
    "load_iql_dataset",
    "load_offline_rl_model",
    "main",
    "score_offline_rl_candidates",
    "train_discrete_iql",
    "train_discrete_iql_artifact",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
