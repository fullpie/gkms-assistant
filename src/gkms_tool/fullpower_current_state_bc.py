"""BC on version-bound current-state simulator teachers, with explicit origin.

The shared pointer optimizer is reused. Native-only dataset labels, existing
actor weights, and active bundle selection are never modified by this command.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np

from . import behavior_cloning as bc
from .fullpower_teacher_validation import load_validated_teacher_dataset
from .training_artifact_io import atomic_write, canonical_json_bytes, sha256_file

PREPARED_SCHEMA = "gkms.fullpower-current-bc-prepared.v1"
MODEL_SCHEMA = "gkms.fullpower-current-state-bc.v1"
CANDIDATE_PREFIX = "fullpower-current-candidate:"
ROOT = Path(__file__).resolve().parents[2]
# These modules define the actual state/cost/effect representation, not just
# the top-level encoder. A later code change requires a newly prepared cohort.
ENCODER_FILES = (
    "fullpower_decision_observation.py", "fullpower_decision_features.py", "fullpower_native_observation.py",
    "plan3_engine.py", "plan3_native_state.py", "plan3_native_search.py",
    "plan3_ordered_customization.py", "card_effect_predicates.py", "plan3_drink.py", "plan3_search_stamina_change.py",
    "version_bound_master.py", "behavior_cloning.py", "fullpower_current_state_bc.py",
)


def _write(path, value):
    atomic_write(Path(path), canonical_json_bytes(value) + b"\n")


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def encoder_hashes():
    return {name: sha256_file(ROOT / "src/gkms_tool" / name) for name in ENCODER_FILES}


def _version_master(root, identity):
    path = (Path(root) / identity / "manifest.json").resolve()
    value = _read(path)
    if value["master_hash"] != identity or value["upstream_commit_subject"] != identity:
        raise ValueError("historical Master identity is not bound")
    database, directory = Path(value["database"]), Path(value["master_dir"])
    if sha256_file(database) != value["database_sha256"]:
        raise ValueError("historical Master database changed")
    for name, expected in value["yaml_sha256"].items():
        if sha256_file(directory / name) != expected:
            raise ValueError("historical Master YAML changed: " + name)
    return database, directory, {"path": str(path), "sha256": sha256_file(path),
                                "master_hash": identity, "database_sha256": value["database_sha256"]}


def _frequency_key(candidate):
    """Stable comparison identity; never use synthetic/native GUIDs as labels."""
    return json.dumps({key: candidate.get(key) for key in
        ("kind", "slot_index", "card_id", "effective_upgrade", "drink_id")}, sort_keys=True)


def _recorded_master_locks(manifest_path):
    """Bind feature Master files to the exact files used by reconstruction."""
    root = Path(manifest_path).resolve().parent
    manifest = _read(manifest_path)
    source_path = Path(manifest["source_manifest_path"])
    source_path = source_path if source_path.is_absolute() else root / source_path
    sources = _read(source_path)
    locks = {}
    for entry in sources["episodes"]:
        report_ref = entry.get("files", {}).get("report.json")
        report_path = Path(report_ref["path"] if report_ref else entry["report_path"])
        report_path = report_path if report_path.is_absolute() else root / report_path
        expected = report_ref["sha256"] if report_ref else entry["report_sha256"]
        if sha256_file(report_path) != expected:
            raise ValueError("frozen reconstruction report changed")
        bindings = [proof for proof in _read(report_path)["startup_proof"] if proof.get("kind") == "master-binding"]
        if len(bindings) != 1:
            raise ValueError("reconstruction has no unique Master file binding")
        binding = bindings[0]
        if locks.setdefault(binding["hash"], binding["manifest_sha256"]) != binding["manifest_sha256"]:
            raise ValueError("same Master version used different reconstruction files")
    return locks


def prepare_teacher_bc(*, teacher_manifest: Path, master_root: Path, output: Path):
    from .fullpower_decision_features import FEATURE_SCHEMA, encode_teacher_transition
    from .fullpower_decision_observation import OBSERVATION_SCHEMA, FullPowerObservationUnavailable
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("prepared output must be new; never overwrite a frozen candidate")
    source_schema = _read(teacher_manifest).get("schema")
    if source_schema == "gkms.fullpower-tiered-teacher-bc-dataset.v1":
        from .fullpower_tiered_teacher_dataset import load_tiered_teacher_dataset
        verified = load_tiered_teacher_dataset(teacher_manifest)
    else:
        verified = load_validated_teacher_dataset(teacher_manifest)
    recorded_master_locks = _recorded_master_locks(verified.manifest_path)
    before_hashes = encoder_hashes()
    masters, locks, encoded, exclusions = {}, {}, [], []
    for row in verified.rows:
        version = row["source"]["master_hash"]
        if version not in masters:
            database, directory, lock = _version_master(master_root, version)
            if recorded_master_locks.get(version) != lock["sha256"]:
                raise ValueError("feature Master differs from the original reconstruction Master")
            masters[version], locks[version] = (database, directory), lock
        database, directory = masters[version]
        try:
            features = encode_teacher_transition(row, database=database, master_dir=directory)
            if features.gaps:
                raise FullPowerObservationUnavailable(features.gaps)
        except FullPowerObservationUnavailable as error:
            exclusions.append({"episode_id": row["source"]["episode_id"],
                "action_ordinal": row["action_ordinal"], "gaps": list(error.gaps)})
            continue
        if features.chosen_index is None or not 0 <= features.chosen_index < len(features.candidate_fields):
            raise ValueError("encoded target is not in its local candidate pool")
        encoded.append({"source": row["source"], "authority": "simulator-teacher",
            "action_ordinal": row["action_ordinal"], "context_tokens": features.context_tokens,
            "candidate_fields": features.candidate_fields, "target": features.chosen_index,
            "missing_fields": list(getattr(features, "missing_fields", ())),
            "quality": row.get("quality", "golden-complete-local-replay"),
            "sample_weight": row.get("sample_weight", 1.0),
            "frequency_keys": [_frequency_key(c) for c in row["legal_candidates"]["rows"]],
            "flow_stage": row["decision_scope"]["flow"] + "|" + row["decision_scope"]["step_type"]})
    if not encoded:
        raise ValueError("no teacher rows could be encoded with the current-state contract")
    if before_hashes != encoder_hashes():
        raise ValueError("encoder changed while preparing teacher features")
    counts = dict(Counter(row["source"]["split"] for row in encoded))
    output.mkdir(parents=True, exist_ok=True)
    rows_path = output / "encoded_teacher_rows.jsonl"
    atomic_write(rows_path, b"".join(canonical_json_bytes(row) + b"\n" for row in encoded))
    manifest = {"schema": PREPARED_SCHEMA, "feature_schema": FEATURE_SCHEMA,
        "observation_schema": OBSERVATION_SCHEMA, "authority": "simulator-teacher",
        "source_manifest_path": str(verified.manifest_path), "source_manifest_sha256": verified.manifest_sha256,
        "source_dataset_schema": source_schema,
        "encoder_sha256": before_hashes, "historical_masters": locks, "encoded_rows_path": str(rows_path),
        "encoded_rows_sha256": sha256_file(rows_path), "rows": len(encoded), "split_counts": counts,
        "trajectory_splits": verified.trajectory_splits, "excluded_encoding_gaps": exclusions,
        "missing_field_counts": dict(Counter(field for row in encoded for field in row["missing_fields"])),
        "quality_split_counts": {quality: dict(Counter(r["source"]["split"] for r in encoded if r["quality"] == quality))
                                 for quality in sorted({r["quality"] for r in encoded})},
        "has_original_holdout": all(counts.get(s, 0) for s in bc.SPLIT_CODES),
        "native_evidence_claimed": False, "runtime_promotion_allowed": False}
    _write(output / "manifest.json", manifest)
    return manifest


def pointer_dataset(rows, *, context_buckets=8192, candidate_buckets=4096):
    """Build optimizer inputs with one weight budget per original trajectory."""
    if not rows:
        raise ValueError("BC cohort is empty")
    counts = Counter(row["source"]["trajectory_id"] for row in rows)
    flow_names = tuple(sorted({row["flow_stage"] for row in rows}))
    splits, contexts, candidates, targets, keys, trajectories, weights, flows = [], [], [], [], [], [], [], []
    trajectory_splits = {}
    for row in rows:
        source, fields, target = row["source"], row["candidate_fields"], row["target"]
        if row["authority"] != "simulator-teacher":
            raise ValueError("this cohort admits simulator teachers only")
        if source["split"] not in bc.SPLIT_CODES or trajectory_splits.setdefault(source["trajectory_id"], source["split"]) != source["split"]:
            raise ValueError("BC cohort changes a trajectory split")
        if type(target) is not int or not 0 <= target < len(fields) or len(fields) < 2 or any(not f for f in fields):
            raise ValueError("BC target/candidates are incomplete")
        if not row["context_tokens"] or len(row["frequency_keys"]) != len(fields):
            raise ValueError("BC features are incomplete")
        contexts.append(list(dict.fromkeys(bc._hash_index(t, context_buckets) for t in row["context_tokens"])))
        candidates.append([list(dict.fromkeys(bc._hash_index(CANDIDATE_PREFIX + t, candidate_buckets) for t in f)) for f in fields])
        splits.append(bc.SPLIT_CODES[source["split"]]); targets.append(target)
        keys.append(tuple(row["frequency_keys"])); trajectories.append(source["trajectory_id"])
        quality_weight = row.get("sample_weight", 1.0)
        if type(quality_weight) not in (int, float) or not np.isfinite(quality_weight) or not 0 < quality_weight <= 1:
            raise ValueError("BC quality weight must be finite and within (0,1]")
        weights.append(quality_weight / counts[source["trajectory_id"]]); flows.append(flow_names.index(row["flow_stage"]))
    context_indices, context_mask = bc._pad_token_rows(contexts)
    candidate_indices, candidate_token_mask, candidate_mask = bc._pad_candidate_token_rows(candidates)
    return bc.OuterDataset(context_indices=context_indices, context_mask=context_mask,
        candidate_indices=candidate_indices, candidate_token_mask=candidate_token_mask, candidate_mask=candidate_mask,
        targets=np.asarray(targets, dtype=np.int64), splits=np.asarray(splits, dtype=np.int8),
        weights=np.asarray(weights, dtype=np.float32), flow_ids=np.asarray(flows, dtype=np.int64), flow_names=flow_names,
        candidate_keys=tuple(keys), chosen_keys=tuple(k[t] for k,t in zip(keys, targets)), trajectory_ids=tuple(trajectories),
        context_buckets=context_buckets, candidate_buckets=candidate_buckets)


def train_prepared_teacher_bc(prepared: Path, *, output: Path, epochs=100, batch_size=64, seed=20260909):
    from .fullpower_decision_features import FEATURE_SCHEMA
    from .fullpower_decision_observation import OBSERVATION_SCHEMA
    manifest = _read(prepared)
    if (manifest.get("schema") != PREPARED_SCHEMA or manifest.get("feature_schema") != FEATURE_SCHEMA
            or manifest.get("observation_schema") != OBSERVATION_SCHEMA or manifest.get("authority") != "simulator-teacher"):
        raise ValueError("prepared BC feature contract differs from this trainer")
    if manifest["encoder_sha256"] != encoder_hashes():
        raise ValueError("BC feature implementation changed; prepare a new cohort")
    if sha256_file(Path(manifest["source_manifest_path"])) != manifest["source_manifest_sha256"]:
        raise ValueError("BC source dataset manifest changed")
    rows_path = Path(manifest["encoded_rows_path"])
    if sha256_file(rows_path) != manifest["encoded_rows_sha256"]:
        raise ValueError("prepared BC rows changed")
    with rows_path.open(encoding="utf8") as stream:
        rows = [json.loads(line) for line in stream]
    counts = dict(Counter(row["source"]["split"] for row in rows))
    if len(rows) != manifest["rows"] or counts != manifest["split_counts"]:
        raise ValueError("prepared BC inventory changed")
    if not all(counts.get(split, 0) > 0 for split in bc.SPLIT_CODES):
        raise ValueError("cannot train a heldout model without original train/validation/test trajectories")
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("model output must be new")
    dataset = pointer_dataset(rows)
    parameters, metrics = bc.train_outer_model(dataset, epochs=epochs, batch_size=batch_size, seed=seed,
        patience=15, maximum_validation_per_flow_drop_pp=5.0)
    if manifest["encoder_sha256"] != encoder_hashes():
        raise ValueError("BC feature implementation changed during training")
    metadata = {"schema": MODEL_SCHEMA, "feature_schema": FEATURE_SCHEMA, "observation_schema": OBSERVATION_SCHEMA,
        "kind": "fullpower-current-candidate-pointer", "context_buckets": dataset.context_buckets,
        "candidate_buckets": dataset.candidate_buckets, "candidate_feature_prefix": CANDIDATE_PREFIX,
        "supervision": "behavior-cloning", "authority": "simulator-teacher", "native_evidence_claimed": False,
        "prepared_manifest_sha256": sha256_file(Path(prepared)), "encoder_sha256": manifest["encoder_sha256"],
        "seed": seed, "candidate_policy": "current-state-main-action; secondary choice remains separate", "shadow_only": True}
    model_path = output / "fullpower_current_state_bc.npz"
    bc._atomic_npz(model_path, bc._model_arrays(parameters, metadata))
    report = {"schema": "gkms.fullpower-current-bc-report.v1", "model": str(model_path),
        "model_sha256": sha256_file(model_path), "metadata": metadata, "split_counts": counts, "metrics": metrics,
        "quality_split_counts": manifest.get("quality_split_counts"),
        "missing_field_counts": manifest.get("missing_field_counts"),
        "quality_metrics": quality_metrics(parameters, rows),
        "native_heldout_evaluated": False, "runtime_promotion_allowed": False,
        "limitations": ["Metrics compare simulator teachers on original trajectory splits; weak prefixes retain unverified terminal outcomes.",
                        "Very small teacher validation/test sets are diagnostic, not robust generalization evidence.",
                        "They do not prove native performance or automatic cultivation success."]}
    _write(output / "report.json", report)
    return report


def quality_metrics(parameters, rows):
    result = {}
    for quality in sorted({row.get("quality", "golden-complete-local-replay") for row in rows}):
        subset = [row for row in rows if row.get("quality", "golden-complete-local-replay") == quality]
        dataset = pointer_dataset(subset)
        counts = Counter(row["source"]["split"] for row in subset)
        result[quality] = {split: bc.evaluate_outer_model(parameters, dataset, split) if counts[split]
                          else {"count": 0, "nll": None, "macro_flow_top1": None, "reason": "no-original-heldout-rows-in-quality-tier"}
                          for split in bc.SPLIT_CODES}
    return result


def score_encoded_candidates(model_path: Path, encoded):
    """Score the same encoded shape used by training, regardless of its origin.

    This returns probabilities only. Runtime legal bindings and secondary
    actions remain the caller's responsibility; no game command is submitted.
    """
    from .fullpower_decision_features import FEATURE_SCHEMA
    from .fullpower_decision_observation import OBSERVATION_SCHEMA
    with np.load(Path(model_path), allow_pickle=False) as archive:
        metadata = json.loads(str(archive["metadata_json"].item()))
        parameters = {key: archive[key].copy() for key in archive.files if key != "metadata_json"}
    if (metadata.get("schema") != MODEL_SCHEMA or metadata.get("kind") != "fullpower-current-candidate-pointer"
            or metadata.get("feature_schema") != FEATURE_SCHEMA or metadata.get("observation_schema") != OBSERVATION_SCHEMA
            or encoded.feature_schema != FEATURE_SCHEMA or encoded.observation_schema != OBSERVATION_SCHEMA):
        raise ValueError("FullPower model/observation contract mismatch")
    if metadata["encoder_sha256"] != encoder_hashes():
        raise ValueError("FullPower feature implementation differs from trained model")
    if encoded.gaps or not encoded.context_tokens or not encoded.candidate_fields:
        raise ValueError("FullPower decision has unresolved encoding gaps")
    context = [list(dict.fromkeys(bc._hash_index(t, metadata["context_buckets"]) for t in encoded.context_tokens))]
    fields = [[list(dict.fromkeys(bc._hash_index(CANDIDATE_PREFIX + t, metadata["candidate_buckets"]) for t in f))
               for f in encoded.candidate_fields]]
    if any(not f for f in fields[0]):
        raise ValueError("FullPower candidate fields are empty")
    context_indices, context_mask = bc._pad_token_rows(context)
    candidate_indices, candidate_token_mask, candidate_mask = bc._pad_candidate_token_rows(fields)
    probabilities, _ = bc._outer_forward(parameters, context_indices, context_mask,
        candidate_indices, candidate_token_mask, candidate_mask, cache=False)
    if not np.all(np.isfinite(probabilities)) or np.any(probabilities < 0) or np.any(probabilities > 1):
        raise ValueError("FullPower model returned invalid probabilities")
    return tuple(float(v) for v in probabilities[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--teacher-manifest", type=Path, required=True)
    prepare.add_argument("--master-root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    train = sub.add_parser("train")
    train.add_argument("--prepared", type=Path, required=True)
    train.add_argument("--output", type=Path, required=True)
    train.add_argument("--epochs", type=int, default=100)
    train.add_argument("--batch-size", type=int, default=64)
    args = vars(parser.parse_args()); command = args.pop("command")
    result = prepare_teacher_bc(**args) if command == "prepare" else train_prepared_teacher_bc(**args)
    print(json.dumps({k:v for k,v in result.items() if k in {"schema", "rows", "split_counts", "model", "model_sha256"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
