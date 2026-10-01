"""Admitted native-primary BC training with bounded packed batches.

The network, loss, gradients and Adam implementation are the existing pointer
model's implementation. This module replaces its globally padded dataset loop.
Only the independent admission factory can supply training rows. Artifacts and
offline imitation metrics remain shadow-only and never select an active model.
"""
from __future__ import annotations

import argparse
from collections import Counter, OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np

from . import behavior_cloning as bc
from .packed_primary_features import PackedPrimaryReader, PackedPrimaryBatch
from .training_artifact_io import atomic_write, canonical_json_bytes, sha256_file

SCHEMA = "gkms.native-primary-packed-bc-training.v1"
MODEL_SCHEMA = "gkms.native-primary-packed-bc-shadow-model.v1"
_DEFAULTS = {"epochs": 20, "batch_size": 32, "eval_batch_size": 32,
    "learning_rate": 0.001, "seed": 20260914, "patience": 5, "min_delta": 1e-6,
    "embedding_dim": 32, "hidden1": 128, "hidden2": 64,
    "max_working_bytes": 1024 * 1024 * 1024, "max_padded_batch_bytes": 16 * 1024 * 1024}


class NativePrimaryTrainingStopped(RuntimeError):
    pass


def _config(config):
    if not isinstance(config, Mapping) or set(config) - set(_DEFAULTS):
        raise ValueError("Unknown native-primary BC training specification field")
    result = {**_DEFAULTS, **dict(config)}
    for key in ("epochs", "batch_size", "eval_batch_size", "seed", "patience", "embedding_dim",
                "hidden1", "hidden2", "max_working_bytes", "max_padded_batch_bytes"):
        value = result[key]
        if type(value) is not int or value < (0 if key == "seed" else 1):
            raise ValueError("Invalid integer training specification: " + key)
    for key in ("learning_rate", "min_delta"):
        value = result[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (key == "learning_rate" and value == 0):
            raise ValueError("Invalid numeric training specification: " + key)
    return result


def _reference(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}


def _save(path, value):
    atomic_write(Path(path), canonical_json_bytes(value) + b"\n")


def _digest_parameters(parameters):
    h = hashlib.sha256()
    for key in sorted(parameters):
        value = parameters[key]
        h.update(key.encode()); h.update(str(value.shape).encode()); h.update(value.tobytes())
    return h.hexdigest()


def _resolve_admission(admission, admission_reference, index_path):
    from .native_primary_bc_admission import (
        load_verified_native_primary_bc_admission, validate_native_primary_bc_admission,
        iter_native_primary_bc_rows,
    )
    if (admission is None) == (admission_reference is None):
        raise ValueError("Supply exactly one verified admission context or owning receipt")
    if admission is None:
        admission = load_verified_native_primary_bc_admission(admission_reference, index_path=index_path)
    def validate():
        summary = validate_native_primary_bc_admission(admission)
        if summary.get("bc_fit_admitted") is not True:
            raise ValueError("Native-primary admission gate did not admit BC fitting")
        return summary
    summary = validate()
    rows = tuple(iter_native_primary_bc_rows(admission))
    return admission, summary, rows, validate


def _validate_rows(rows, layout):
    if not rows:
        raise ValueError("Admitted BC rows are empty")
    for name in ("context_buckets", "candidate_buckets"):
        if type(layout.get(name)) is not int or not 2 <= layout[name] <= 2**31:
            raise ValueError("Admitted pointer layout differs")
    seen, players, trajectories = set(), {}, {}
    counts = dict.fromkeys(("train", "validation", "test"), 0)
    for row in rows:
        split = row["split"]
        if split not in counts:
            raise ValueError("Unknown or blocked player split cannot enter BC")
        counts[split] += 1
        key = (row["packed_manifest"]["path"], row["packed_manifest"]["sha256"], row["row_index"])
        if key in seen or type(row["row_index"]) is not int or row["row_index"] < 0:
            raise ValueError("Duplicate or invalid packed source row")
        seen.add(key)
        for mapping, field in ((players, "player_sha256"), (trajectories, "whole_trajectory_id")):
            identity = row[field]
            if not isinstance(identity, str) or not identity or mapping.setdefault(identity, split) != split:
                raise ValueError("Unknown player or cross-split player/whole-trajectory overlap")
        for field in ("flow", "stage", "action_kind", "episode_id", "feature_contract_sha256", "raw_state_sha256"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise ValueError("Admitted row metadata is incomplete: " + field)
        if type(row.get("label_index")) is not int or row["label_index"] < 0:
            raise ValueError("Invalid admitted source label")
    if any(value == 0 for value in counts.values()):
        raise ValueError("Training, validation and test splits must all be nonempty")
    return counts


class _PackedRows:
    def __init__(self, rows, layout, budget):
        self.rows, self.layout, self.budget = rows, layout, budget
        self.readers = OrderedDict()

    def _reader(self, reference):
        key = (reference["path"], reference["sha256"])
        reader = self.readers.pop(key, None)
        if reader is None:
            reader = PackedPrimaryReader(reference["path"], expected_sha256=reference["sha256"])
            if any(reader.manifest[name] != self.layout[name] for name in ("context_buckets", "candidate_buckets")):
                raise ValueError("Packed pointer layout differs from the admitted contract set")
        self.readers[key] = reader
        if len(self.readers) > 8:
            self.readers.popitem(last=False)
        return reader

    def batch(self, indices):
        groups = {}
        for position, index in enumerate(indices):
            row = self.rows[int(index)]
            key = (row["packed_manifest"]["path"], row["packed_manifest"]["sha256"])
            groups.setdefault(key, []).append((position, row))
        batches = []
        width = candidates = tokens = 0
        for group in groups.values():
            batch = self._reader(group[0][1]["packed_manifest"]).get_rows(
                [row["row_index"] for _, row in group], max_batch_bytes=self.budget)
            for local, (_, expected) in enumerate(group):
                metadata = batch.metadata[local]
                if (metadata.get("representation_complete") is not True or metadata.get("mandatory_gaps") != []
                        or metadata.get("training_admitted") is not False
                        or any(metadata.get(key) != expected[key] for key in
                            ("feature_contract_sha256", "raw_state_sha256", "label_index", "flow", "stage"))):
                    raise ValueError("Packed row differs from its admitted source/label/complete representation")
            width = max(width, batch.context_indices.shape[1])
            candidates = max(candidates, batch.candidate_indices.shape[1])
            tokens = max(tokens, batch.candidate_indices.shape[2])
            needed = len(indices) * (8 * width + 8 * candidates * tokens + 4 * candidates + 4)
            if needed > self.budget:
                raise MemoryError("Combined packed batch exceeds the fixed working-memory budget")
            batches.append((group, batch))
        n = len(indices)
        contexts = np.zeros((n, width), np.int32); context_mask = np.zeros_like(contexts, dtype=np.float32)
        candidate = np.zeros((n, candidates, tokens), np.int32); token_mask = np.zeros_like(candidate, dtype=np.float32)
        candidate_mask = np.zeros((n, candidates), np.float32); targets = np.empty(n, np.int32)
        metadata = [None] * n
        for group, batch in batches:
            for local, (position, _) in enumerate(group):
                cw = batch.context_indices.shape[1]; nc, nt = batch.candidate_indices.shape[1:]
                contexts[position, :cw] = batch.context_indices[local]
                context_mask[position, :cw] = batch.context_mask[local]
                candidate[position, :nc, :nt] = batch.candidate_indices[local]
                token_mask[position, :nc, :nt] = batch.candidate_token_mask[local]
                candidate_mask[position, :nc] = batch.candidate_mask[local]
                targets[position] = batch.targets[local]; metadata[position] = batch.metadata[local]
        return PackedPrimaryBatch(contexts, context_mask, candidate, token_mask, candidate_mask,
            targets, tuple(int(i) for i in indices), tuple(metadata))

    def batches(self, indices, size):
        # A deterministic bisection adapts padding to large rows, without
        # truncation or omitting a decision. A single oversized row stops fit.
        def read(part):
            try:
                batch = self.batch(part)
            except MemoryError:
                if len(part) == 1:
                    raise
                middle = len(part) // 2
                yield from read(part[:middle]); yield from read(part[middle:])
            else:
                yield part, batch
        for start in range(0, len(indices), size):
            yield from read(indices[start:start + size])


class _Metrics:
    def __init__(self):
        self.groups = {"overall": {}, "flow": {}, "stage": {}, "kind": {}}

    def add(self, rows, probabilities, targets, mask):
        if not np.all(np.isfinite(probabilities)):
            raise ValueError("Nonfinite pointer probabilities")
        for row, scores, target, legal in zip(rows, probabilities, targets, mask):
            order = np.argsort(-scores, kind="stable")
            order = order[legal[order] > 0]
            if not len(order) or target not in order:
                raise ValueError("Evaluation target is outside the supplied primary candidates")
            rank = int(np.flatnonzero(order == target)[0]) + 1
            nll = -math.log(max(float(scores[target]), 1e-12))
            for group, key in (("overall", "all"), ("flow", row["flow"]), ("stage", row["stage"]), ("kind", row["action_kind"])):
                value = self.groups[group].setdefault(key, dict(count=0, top1=0, top3=0, nll=0.0, mrr=0.0,
                    uniform_expected_top1=0.0, uniform_nll=0.0))
                value["count"] += 1; value["top1"] += int(rank == 1); value["top3"] += int(rank <= 3)
                value["nll"] += nll; value["mrr"] += 1 / rank
                value["uniform_expected_top1"] += 1 / len(order); value["uniform_nll"] += math.log(len(order))

    def result(self):
        result = {}
        for group, rows in self.groups.items():
            result[group] = {key: {name: value if name == "count" else value / row["count"]
                for name, value in row.items()} for key, row in sorted(rows.items())}
        result.update(result.pop("overall").get("all", {}))
        result["macro_flow_top1"] = float(np.mean([v["top1"] for v in result["flow"].values()]))
        result["predictions_outside_supplied_candidates"] = 0
        return result


def _stop(stop_requested):
    if stop_requested is not None and stop_requested():
        raise NativePrimaryTrainingStopped("Training stop requested; partial checkpoints retained")


def _evaluate(parameters, dataset, rows, indices, batch_size, stop_requested=None):
    metrics = _Metrics()
    for selected, batch in dataset.batches(indices, batch_size):
        _stop(stop_requested)
        probabilities, _ = bc._outer_forward(parameters, batch.context_indices, batch.context_mask,
            batch.candidate_indices, batch.candidate_token_mask, batch.candidate_mask, cache=False)
        metrics.add([rows[int(i)] for i in selected], probabilities, batch.targets, batch.candidate_mask)
    return metrics.result()


def _source_weights(rows):
    """Exact dataset's source-only trajectory/flow weighting, unchanged."""
    trajectories = Counter((row["split"], row["whole_trajectory_id"]) for row in rows)
    flows = Counter((row["split"], row["flow"]) for row in rows)
    weights = np.asarray([1.0 / trajectories[(row["split"], row["whole_trajectory_id"])]
        / math.sqrt(flows[(row["split"], row["flow"])]) for row in rows], dtype=np.float32)
    train_mean = float(weights[[row["split"] == "train" for row in rows]].mean())
    if train_mean > 0:
        weights /= train_mean
    return weights


def _fit(rows, layout, config, *, validate_admission, callback=None, checkpoint=None, stop_requested=None):
    """Internal numerical loop; public fitting must first pass the admission gate."""
    split_counts = _validate_rows(rows, layout)
    validate_admission()
    d, h1, h2 = config["embedding_dim"], config["hidden1"], config["hidden2"]
    parameter_count = (layout["context_buckets"] + layout["candidate_buckets"]) * d + 3 * d * h1 + h1 + h1 * h2 + h2 + h2 + 1
    persistent_bytes = parameter_count * 4 * 6
    remainder = config["max_working_bytes"] - persistent_bytes
    budget = min(config["max_padded_batch_bytes"], remainder // max(32, 4 * d, 2 * h1 + 2 * h2))
    if budget < 24:
        raise MemoryError("Working-memory specification cannot hold pointer parameters and one batch")
    parameters = bc._init_outer_parameters(context_buckets=layout["context_buckets"], candidate_buckets=layout["candidate_buckets"],
        embedding_dim=config["embedding_dim"], hidden1=config["hidden1"], hidden2=config["hidden2"], seed=config["seed"])
    # Parameters, Adam states, gradients and the selected checkpoint all stay
    # resident. Reserve a conservative factor for the existing embedding gather
    # and backward intermediates in addition to packed int32/mask arrays.
    dataset = _PackedRows(rows, layout, budget)
    optimizer = bc._Adam(parameters, config["learning_rate"])
    rng = np.random.default_rng(config["seed"])
    splits = {name: np.array([i for i, row in enumerate(rows) if row["split"] == name], dtype=np.int64) for name in split_counts}
    weights = _source_weights(rows)
    initial_sha = _digest_parameters(parameters)
    history = []; best = None; best_nll = float("inf"); best_epoch = None; stale = 0
    for epoch in range(1, config["epochs"] + 1):
        validate_admission(); _stop(stop_requested)
        if callback: callback({"phase": "bc", "epoch": epoch, "epochs": config["epochs"], "rows_completed": 0})
        summed_loss = 0.0; weight_total = 0.0; count = 0; batches = 0
        for selected, batch in dataset.batches(rng.permutation(splits["train"]), config["batch_size"]):
            _stop(stop_requested)
            loss, gradients = bc._outer_gradients(parameters, (batch.context_indices, batch.context_mask,
                batch.candidate_indices, batch.candidate_token_mask, batch.candidate_mask, batch.targets,
                weights[selected]))
            if not math.isfinite(loss) or any(not np.all(np.isfinite(v)) for v in gradients.values()):
                raise ValueError("Nonfinite pointer loss or gradients; no completed model is published")
            optimizer.update(parameters, gradients)
            weight_sum = float(weights[selected].sum())
            summed_loss += loss * weight_sum; weight_total += weight_sum
            count += len(batch.targets); batches += 1
            if callback: callback({"phase": "bc", "epoch": epoch, "epochs": config["epochs"],
                "rows_completed": count, "rows_total": split_counts["train"], "batch_loss": loss})
        validate_admission()
        validation = _evaluate(parameters, dataset, rows, splits["validation"], config["eval_batch_size"], stop_requested)
        history.append({"epoch": epoch, "train_loss": summed_loss / weight_total, "trained_rows": count,
            "optimizer_batches": batches, "validation": validation})
        if validation["nll"] + config["min_delta"] < best_nll:
            best_nll = validation["nll"]; best_epoch = epoch; stale = 0
            best = {key: value.copy() for key, value in parameters.items()}
            if checkpoint: checkpoint(best, {"selected_epoch": best_epoch, "validation_nll": best_nll})
        else:
            stale += 1
        if callback: callback({"phase": "validation", "epoch": epoch, "validation": validation,
            "selected_epoch": best_epoch, "stale_epochs": stale})
        if stale >= config["patience"]:
            break
    if best is None:
        raise ValueError("No finite validation-selected checkpoint was produced")
    validate_admission(); _stop(stop_requested)
    evaluations = {}
    for split in ("train", "validation", "test"):
        if callback: callback({"phase": "offline-evaluation", "split": split})
        evaluations[split] = _evaluate(best, dataset, rows, splits[split], config["eval_batch_size"], stop_requested)
    validate_admission()
    return best, {"history": history, "epochs_ran": len(history), "optimizer_steps": optimizer.step,
        "checkpoint_selection": {"metric": "validation-nll", "selected_epoch": best_epoch,
            "test_used_for_selection": False, "patience": config["patience"], "min_delta": config["min_delta"]},
        "split_counts": split_counts, "evaluation": evaluations, "initial_parameter_sha256": initial_sha,
        "shuffle_policy": "fixed-seed-global-train-row-permutation; each train row once per epoch",
        "sample_weighting": {"rule": "existing-exact-dataset: inverse split/whole-trajectory count divided by sqrt(split/flow count); normalized by train mean",
            "outcomes_used": False, "weight_array_sha256": hashlib.sha256(weights.tobytes()).hexdigest(),
            "train_mean": float(weights[splits["train"]].mean())},
        "selected_parameter_sha256": _digest_parameters(best), "working_memory": {"persistent_estimate_bytes": persistent_bytes,
            "padded_batch_budget_bytes": budget, "global_padding": False, "oversized_batch_policy": "deterministic-bisection-no-truncation"}}


def train_native_primary_bc(*, output_dir, config, admission=None, admission_reference=None,
                            index_path=None, training_callback=None, stop_requested=None):
    """Fit only a freshly verified admission; save a separate shadow artifact."""
    config = _config(config)
    admitted, summary, rows, validate = _resolve_admission(admission, admission_reference, index_path)
    contract_set = admitted.contract_set
    layout = contract_set["pointer_layout"]
    _validate_rows(rows, layout)
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError("Use a fresh training attempt; existing artifacts are preserved: " + str(output))
    output.mkdir(parents=True)
    started = time.monotonic()
    sources = {name: _reference(Path(__file__).with_name(name)) for name in
        ("native_primary_bc_training.py", "behavior_cloning.py", "packed_primary_features.py", "native_primary_bc_admission.py")}
    metadata = {"schema": MODEL_SCHEMA, "kind": "native-primary-packed-candidate-pointer",
        "expected_loading_method": "gkms_tool.native_primary_bc_training.load_native_primary_bc_model",
        "loading_scope": "explicit-native-primary-BC+source-contract-set; not the active runtime loader",
        "shadow_only": True, "default_enabled": False, "runtime_promotion_allowed": False,
        "context_buckets": layout["context_buckets"], "candidate_buckets": layout["candidate_buckets"],
        "embedding_dim": config["embedding_dim"], "hidden": [config["hidden1"], config["hidden2"]],
        "candidate_feature_prefix": "exact-candidate-field:", "contract_set": contract_set,
        "admission_reference": admitted.receipt_reference, "training_specification": config,
        "implementation": sources, "action_after_state_observed": False, "RL_transition_qualified": False,
        "current_runtime_master_compatibility_qualified": False}
    _save(output / "training_specification.json", config)
    _save(output / "status.json", {"schema": SCHEMA, "status": "running", "shadow_only": True, "admission": admitted.receipt_reference})
    def callback(update):
        _save(output / "progress.json", update)
        if training_callback: training_callback(update)
    def checkpoint(parameters, selection):
        bc._atomic_npz(output / "best_checkpoint.npz", bc._model_arrays(parameters, {**metadata, "checkpoint": selection, "complete": False}))
        _save(output / "checkpoint.json", selection)
    try:
        parameters, metrics = _fit(rows, layout, config, validate_admission=validate,
            callback=callback, checkpoint=checkpoint, stop_requested=stop_requested)
        validate()
        model_path = output / "native_primary_bc_model.npz"
        bc._atomic_npz(model_path, bc._model_arrays(parameters, {**metadata, "complete": True,
            "checkpoint_selection": metrics["checkpoint_selection"]}))
        report = {"schema": SCHEMA, "status": "completed", "shadow_only": True, "default_enabled": False,
            "runtime_promotion_allowed": False, "admission": admitted.receipt_reference, "admission_summary": summary,
            "model": _reference(model_path), "training_specification": _reference(output / "training_specification.json"),
            "implementation": sources, "metrics": metrics, "elapsed_seconds": time.monotonic() - started,
            "offline_evaluation_scope": "held-out original-action imitation; no rollout or score-improvement claim",
            "policy_quality_accepted": False, "live_cultivation_verified": False, "score_improvement_verified": False,
            "action_after_state_observed": False, "RL_transition_qualified": False}
        _save(output / "report.json", report)
        result = {"schema": SCHEMA, "status": "completed", "report": _reference(output / "report.json"),
            "model": report["model"], "shadow_only": True, "runtime_promotion_allowed": False}
        _save(output / "status.json", result)
        return result
    except BaseException as exc:
        _save(output / "status.json", {"schema": SCHEMA,
            "status": "stopped" if isinstance(exc, NativePrimaryTrainingStopped) else "failed",
            "error_type": type(exc).__name__, "error": str(exc), "shadow_only": True,
            "runtime_promotion_allowed": False, "elapsed_seconds": time.monotonic() - started})
        raise


def load_native_primary_bc_model(model_reference, *, expected_contract_set_sha256):
    """Explicit shadow model loading; no registration or runtime activation."""
    from .native_structure_contract_set import validate_native_structure_contract_set
    if (not isinstance(model_reference, Mapping) or not isinstance(model_reference.get("path"), str)
            or sha256_file(Path(model_reference["path"])) != model_reference.get("sha256")):
        raise ValueError("Native-primary shadow model differs from its hash receipt")
    with np.load(model_reference["path"], allow_pickle=False) as arrays:
        metadata = json.loads(str(arrays["metadata_json"].item()))
        if (metadata.get("schema") != MODEL_SCHEMA or metadata.get("kind") != "native-primary-packed-candidate-pointer"
                or metadata.get("shadow_only") is not True or metadata.get("complete") is not True
                or metadata.get("runtime_promotion_allowed") is not False):
            raise ValueError("A completed explicit native-primary shadow artifact is required")
        contract_set = metadata["contract_set"]
        validate_native_structure_contract_set(contract_set)
        if contract_set["contract_set_sha256"] != expected_contract_set_sha256:
            raise ValueError("Native-primary model source contract set differs")
        layout = contract_set["pointer_layout"]
        if any(metadata[k] != layout[k] for k in ("context_buckets", "candidate_buckets")):
            raise ValueError("Native-primary model pointer layout differs")
        d = metadata["embedding_dim"]; h1, h2 = metadata["hidden"]
        shapes = {"context_embedding": (layout["context_buckets"], d), "candidate_embedding": (layout["candidate_buckets"], d),
            "w1": (d * 3, h1), "b1": (h1,), "w2": (h1, h2), "b2": (h2,), "w3": (h2,), "b3": (1,)}
        if set(arrays.files) != {*shapes, "metadata_json"}:
            raise ValueError("Native-primary model parameter set differs")
        parameters = {}
        for name, shape in shapes.items():
            value = arrays[name]
            if value.dtype != np.float32 or value.shape != shape or not np.all(np.isfinite(value)):
                raise ValueError("Native-primary model parameter shape/type/value differs")
            parameters[name] = value.copy()
    return parameters, metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission-reference", required=True, type=Path, help="JSON path/SHA reference to the admission receipt")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = train_native_primary_bc(admission_reference=json.loads(args.admission_reference.read_bytes()),
        config=json.loads(args.config.read_bytes()), output_dir=args.output)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
