"""Manual, versioned card-model updates using the existing frozen BC/IQL chain."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
import uuid

from .card_data_update import (
    DEFAULT_OUTPUT_ROOT as CARD_UPDATE_ROOT, load_card_data_snapshot,
    read_card_data_snapshot, run_card_data_update,
)
from .card_semantic_features import (
    FEATURE_SCHEMA, FEATURE_SCHEMA_V1, FEATURE_SCHEMA_V2, build_card_semantic_features, load_card_semantic_features,
    write_card_semantic_features,
)
from .exact_policy_contract import strict_exact_candidate_index
from .master_db import DEFAULT_DATABASE
from .policy_bundle import DEFAULT_BUNDLE_ROOT, PROJECT_ROOT, PolicyBundle, write_policy_bundle_manifest
from .training_artifact_io import atomic_write, canonical_json_bytes, sha256_file
from .training_spec import SPEC_V2_SCHEMA


SCHEMA = "gkms.model-update-job.v1"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "var" / "model_updates"
_LOCK = threading.Lock()
_SPLITS = ("train", "validation", "test")


class ModelUpdateCancelled(InterruptedError):
    pass


@dataclass(frozen=True, slots=True)
class ModelUpdateRequest:
    source_spec_path: Path | None = None
    database: Path = DEFAULT_DATABASE
    output_root: Path = DEFAULT_OUTPUT_ROOT
    bundle_root: Path = DEFAULT_BUNDLE_ROOT
    card_update_root: Path = CARD_UPDATE_ROOT
    bc_epochs: int = 60
    rl_epochs: int = 30
    batch_size: int = 64
    seed: int = 20260905
    minimum_train_trajectories_per_new_card: int = 2

    def __post_init__(self) -> None:
        for key in ("database", "output_root", "bundle_root", "card_update_root"):
            object.__setattr__(self, key, Path(getattr(self, key)))
        if self.source_spec_path is not None:
            object.__setattr__(self, "source_spec_path", Path(self.source_spec_path))
        for key in ("bc_epochs", "rl_epochs", "batch_size", "minimum_train_trajectories_per_new_card"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"{key} must be a positive integer")
        if self.rl_epochs < 2:
            raise ValueError("RL needs at least one warmup and one actor epoch")


@dataclass(frozen=True, slots=True)
class ModelUpdateResult:
    status: str
    report_path: Path
    bundle_path: Path | None
    blockers: tuple[str, ...]


def _path(value: object) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError("frozen input path is missing")
    path = Path(value)
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _card_ref(candidate: Mapping[str, object]) -> str | None:
    kind = candidate.get("kind", candidate.get("action_type"))
    if kind not in {"use-hand", "use_hand", "play", "card"}:
        return None
    card_id, upgrade = candidate.get("card_id"), candidate.get("upgrade")
    if not isinstance(card_id, str) or type(upgrade) is not int:
        raise ValueError("training candidate lacks exact card ID/upgrade")
    return f"{card_id}@{upgrade}"


def audit_semantic_training_dataset(
    source_spec_path: Path,
    *,
    feature_contract: Mapping[str, object],
    required_card_ids: Sequence[str] = (),
    minimum_train_trajectories: int = 2,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Revalidate complete frozen stages before spending time training models."""
    from .offline_rl_learner import encode_exact_observation

    spec = _object(source_spec_path)
    if spec.get("schema") != SPEC_V2_SCHEMA:
        raise ValueError("model update requires a frozen training spec v2")
    lock = spec["dataset_lock"]["exact_exam"]
    if lock.get("state_authority") != "frozen native runtime S,A,S'":
        raise ValueError("dataset is not frozen native runtime evidence")
    paths = {}
    for key in ("stages", "manifest", "canonical_labels_manifest", "trajectory_split_manifest"):
        path = _path(lock[key + "_path"])
        if sha256_file(path) != lock[key + "_sha256"]:
            raise ValueError(f"frozen {key} hash mismatch")
        paths[key] = path
    manifest = _object(paths["manifest"])
    if not (manifest.get("schema") == "gkms.offline-rl-dataset-manifest.v1"
            and manifest.get("train_ready") is True
            and manifest.get("stages_sha256") == lock["stages_sha256"]
            and manifest.get("audit", {}).get("train_ready") is True
            and manifest.get("audit", {}).get("blockers") == []):
        raise ValueError("frozen dataset has not passed its readiness gate")
    if _object(paths["canonical_labels_manifest"]).get("schema") != "gkms.canonical-training-label-manifest.v1":
        raise ValueError("canonical manifest schema mismatch")
    split_manifest = _object(paths["trajectory_split_manifest"])
    if split_manifest.get("policy_version") != 1:
        raise ValueError("trajectory split policy is unsupported")
    features = load_card_semantic_features(feature_contract)
    card_counts = defaultdict(Counter)
    trajectories_by_card = defaultdict(lambda: defaultdict(set))
    trajectory_splits, source_splits = {}, defaultdict(set)
    stage_ids, stage_keys, digests, encoded_splits = set(), set(), set(), {}
    split_counts, chosen_counts = Counter(), Counter()
    observed_refs, flows = set(), set()
    rows = 0
    with paths["stages"].open(encoding="utf-8-sig") as stream:
        for stage_number, line in enumerate(stream, 1):
            if cancelled is not None and cancelled():
                raise ModelUpdateCancelled("dataset audit")
            if not line.strip():
                continue
            stage = json.loads(line)
            if stage.get("schema") != "gkms.offline-rl-stage.v1":
                raise ValueError("unsupported stage schema")
            stage_id, trajectory, split, source = (stage[key] for key in ("stage_id", "trajectory_id", "split", "source_id"))
            if any(not isinstance(value, str) or not value for value in (stage_id, trajectory, source)):
                raise ValueError("stage provenance identity is incomplete")
            if "simulator" in source.lower():
                raise ValueError("simulator rows cannot be relabelled as native IQL evidence")
            stage_key = (trajectory, stage["flow"], stage["stage"])
            if stage_id in stage_ids or stage_key in stage_keys or split not in _SPLITS:
                raise ValueError("duplicate stage or invalid dataset split")
            stage_ids.add(stage_id)
            stage_keys.add(stage_key)
            if trajectory_splits.setdefault(trajectory, split) != split:
                raise ValueError("one trajectory crosses train/validation/test")
            # One import/capture container can hold independent trajectories.
            # Leakage is enforced below on trajectories and actual observations.
            source_splits[source].add(split)
            transitions = stage.get("transitions")
            if not isinstance(transitions, list) or not transitions or stage.get("transition_count") != len(transitions):
                raise ValueError("stage transition count is invalid")
            previous_after, previous_step, total_reward = None, None, 0
            flows.add(stage["flow"])
            for index, row in enumerate(transitions):
                before, after = row["state_before"], row["state_after"]
                before_hash, after_hash = _digest(before), _digest(after)
                if row.get("before_sha256") != before_hash or row.get("after_sha256") != after_hash:
                    raise ValueError("native transition digest mismatch")
                if previous_after is not None and previous_after != before_hash:
                    raise ValueError("native stage has a state gap")
                if type(row.get("step")) is not int or (previous_step is not None and row["step"] != previous_step + 1):
                    raise ValueError("native stage has a step gap")
                if (before_hash, after_hash) in digests:
                    raise ValueError("duplicate native transition")
                digests.add((before_hash, after_hash))
                previous_after, previous_step = after_hash, row["step"]
                if type(row.get("terminal")) is not bool or row["terminal"] != (index == len(transitions) - 1):
                    raise ValueError("terminal must occur once at stage end")
                reward = row.get("reward")
                if isinstance(reward, bool) or not isinstance(reward, (int, float)) or reward < 0 or reward != after["score"] - before["score"]:
                    raise ValueError("RL reward is not the observed score delta")
                total_reward += reward
                legal = row["legal_candidates"]
                selected = strict_exact_candidate_index(before, row["action"], legal)
                observation = encode_exact_observation(state_before=before, flow=stage["flow"], stage=stage["stage"],
                    legal_candidates=legal, semantic_features=features)
                encoded = _digest((observation.context_indices, observation.candidate_indices))
                if encoded_splits.setdefault(encoded, split) != split:
                    raise ValueError("encoded observation overlaps held-out splits")
                exposed = set()
                for candidate in legal:
                    ref = _card_ref(candidate)
                    if ref is not None:
                        observed_refs.add(ref)
                        exposed.add(ref.rpartition("@")[0])
                for identity in exposed:
                    card_counts[identity][split] += 1
                    trajectories_by_card[identity][split].add(trajectory)
                chosen_ref = _card_ref(legal[selected])
                if chosen_ref is not None and split == "train":
                    chosen_counts[chosen_ref.rpartition("@")[0]] += 1
                split_counts[split] += 1
                rows += 1
            if stage.get("terminal") is not True or total_reward != stage.get("return"):
                raise ValueError("stage terminal/return contract mismatch")
            if progress is not None:
                progress(f"檢查資料：{stage_number} 個演出、{rows} 筆動作")
    inventory = spec["inventory"]["exact_exam"]
    if dict(split_manifest.get("trajectory_splits", {})) != trajectory_splits:
        raise ValueError("trajectory split manifest differs from stage data")
    if rows != inventory.get("transition_count") or rows != manifest.get("transition_count"):
        raise ValueError("frozen transition inventory differs from stage data")
    if len(stage_ids) != inventory.get("stage_count") or len(stage_ids) != manifest.get("stage_count"):
        raise ValueError("frozen stage inventory differs from stage data")
    if dict(split_counts) != inventory.get("split_transition_counts") or any(not split_counts[split] for split in _SPLITS):
        raise ValueError("train/validation/test split inventory is incomplete")
    blockers = []
    for identity in sorted(set(required_card_ids)):
        for split in _SPLITS:
            count = len(trajectories_by_card[identity][split])
            minimum = minimum_train_trajectories if split == "train" else 1
            if count < minimum:
                blockers.append(f"new-card-trajectory-coverage:{identity}:{split}:{count}<{minimum}")
        if not chosen_counts[identity]:
            blockers.append(f"new-card-never-chosen-in-training:{identity}")
    return {"ready": not blockers, "blockers": blockers, "transition_count": rows,
            "stage_count": len(stage_ids), "trajectory_count": len(trajectory_splits),
            "split_counts": dict(split_counts), "flows": sorted(flows), "observed_card_refs": sorted(observed_refs),
            "required_card_ids": sorted(set(required_card_ids)),
            "required_card_coverage": {key: {split: len(trajectories_by_card[key][split]) for split in _SPLITS}
                                       for key in sorted(set(required_card_ids))},
            "stages_path": str(paths["stages"]), "stages_sha256": lock["stages_sha256"],
            "source_container_cross_split_count": sum(len(values) > 1 for values in source_splits.values()),
            "state_authority": lock["state_authority"], "historical_rows_relabelled": False}


def run_model_update(
    request: ModelUpdateRequest = ModelUpdateRequest(),
    *,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ModelUpdateResult:
    """Validate -> freeze feature/spec -> real BC/IQL -> gate -> candidate bundle."""
    if not isinstance(request, ModelUpdateRequest):
        raise TypeError("request must be ModelUpdateRequest")
    job_id = "card-semantic-" + uuid.uuid4().hex[:12]
    root = request.output_root.resolve() / job_id
    report_path = root / "report.json"
    report: dict[str, object] = {"schema": SCHEMA, "job_id": job_id, "status": "running",
        "phases": [], "blockers": [], "active_model_changed": False, "bundle_path": None}
    if not _LOCK.acquire(blocking=False):
        raise RuntimeError("a model update is already running")

    def checkpoint(message: str) -> None:
        if cancelled is not None and cancelled():
            raise ModelUpdateCancelled(message)
        report["current_step"] = message
        atomic_write(report_path, canonical_json_bytes(report) + b"\n")
        atomic_write(request.output_root / "latest_job.json", canonical_json_bytes({
            "schema": SCHEMA, "status": "running", "report_path": str(report_path),
            "bundle_path": None, "blockers": [],
        }) + b"\n")
        if progress is not None:
            progress(message)

    def finish(status: str, blockers=(), bundle_path: Path | None = None) -> ModelUpdateResult:
        report.update(status=status, blockers=list(blockers), bundle_path=None if bundle_path is None else str(bundle_path))
        atomic_write(report_path, canonical_json_bytes(report) + b"\n")
        atomic_write(request.output_root / "latest_job.json", canonical_json_bytes({
            "schema": SCHEMA, "status": status, "report_path": str(report_path),
            "bundle_path": report["bundle_path"], "blockers": list(blockers),
        }) + b"\n")
        return ModelUpdateResult(status, report_path, bundle_path, tuple(blockers))

    try:
        checkpoint("驗證卡片資料與目前模型")
        base_bundle = PolicyBundle.load()
        report["source_bundle_path"] = str(base_bundle.manifest_path)
        source_spec_path = _path(request.source_spec_path or base_bundle.payload["training_spec_path"])
        source_spec = _object(source_spec_path)
        report["source_spec_path"] = str(source_spec_path)
        report["source_spec_sha256"] = sha256_file(source_spec_path)
        card_update = run_card_data_update(database=request.database, output_root=request.card_update_root,
            progress=checkpoint, cancelled=cancelled)
        report["card_update_report"] = str(card_update.report_path)
        report["phases"].append("card-data-validated")
        if card_update.status == "blocked":
            return finish("blocked", card_update.summary["blockers"])
        checkpoint("建立版本化卡牌效果與數值特徵")
        feature_contract = write_card_semantic_features(card_update.snapshot_path, root / "card_semantic_features.json")
        features = load_card_semantic_features(feature_contract)
        report["card_feature_contract"] = feature_contract
        if features.payload["blockers"]:
            report["semantic_blockers"] = features.payload["blockers"]
            return finish("blocked", ("card-semantic-graph-incomplete",))
        old_features = None
        old_model = base_bundle.model_path("exact_exam_policy")
        from .behavior_cloning import load_model_artifact
        old_contract = load_model_artifact(old_model)[1].get("card_feature_contract")
        if old_contract is not None:
            old_features = load_card_semantic_features(old_contract, relative_to=old_model.parent).payload
        elif (request.card_update_root / "baseline.json").is_file():
            baseline_path = request.card_update_root / "baseline.json"
            baseline = load_card_data_snapshot(baseline_path)
            auxiliary = baseline.get("semantic_tables", {})
            baseline_schema = FEATURE_SCHEMA if "produce_audition" in auxiliary else FEATURE_SCHEMA_V2 if auxiliary else FEATURE_SCHEMA_V1
            old_features = build_card_semantic_features(baseline_path, schema=baseline_schema)
        before_cards = {} if old_features is None else old_features["cards"]
        # Adding a feature representation is not a Master rule change. Compare
        # the shared v1 definition fields during migration; v2-to-v2 updates
        # additionally compare the frozen effective/customization definitions.
        definition_keys = ("tokens", "customize_ids") if old_features is not None and old_features["schema"] == FEATURE_SCHEMA_V1 else None
        def definition(value):
            if value is None or definition_keys is None:
                return value
            return {key: value[key] for key in definition_keys}
        changed_refs = sorted(ref for ref, value in features.payload["cards"].items()
                              if old_features is not None and definition(before_cards.get(ref)) != definition(value))
        report["feature_schema_migration"] = {
            "from": None if old_features is None else old_features["schema"],
            "to": features.schema, "reuses_previous_weights": False,
        }
        changed_existing = [ref for ref in changed_refs if ref in before_cards]
        required_ids = sorted({ref.rpartition("@")[0] for ref in changed_refs})
        report["changed_card_refs"] = changed_refs
        manifest = _object(_path(source_spec["dataset_lock"]["exact_exam"]["manifest_path"]))
        if changed_existing and manifest.get("card_data_fingerprint") != feature_contract["snapshot_fingerprint"]:
            return finish("blocked", ("changed-existing-card-rules-require-dataset-bound-to-current-master",))
        checkpoint("準備相容資料集並檢查新卡的獨立驗證覆蓋")
        audit = audit_semantic_training_dataset(source_spec_path, feature_contract=feature_contract,
            required_card_ids=required_ids, minimum_train_trajectories=request.minimum_train_trajectories_per_new_card,
            progress=checkpoint, cancelled=cancelled)
        report["dataset_audit"] = audit
        report["phases"].append("dataset-audited")
        if not audit["ready"]:
            return finish("blocked", audit["blockers"])
        spec = deepcopy(source_spec)
        spec.pop("spec_sha256", None)
        spec["card_feature_contract"] = {**feature_contract, "features_path": "card_semantic_features.json"}
        spec.setdefault("objectives", {}).setdefault("exact_main_action_bc", {})["rows"] = audit["transition_count"]
        spec["objectives"]["offline_rl"].update(learner_enabled=True)
        spec["model_update"] = {"source_spec_sha256": report["source_spec_sha256"],
                                "feature_schema": FEATURE_SCHEMA, "rewards_or_splits_modified": False}
        spec_path = root / "training_spec.json"
        atomic_write(spec_path, canonical_json_bytes(spec) + b"\n")
        report["training_spec_path"] = str(spec_path)

        def epoch(value: Mapping[str, object]) -> None:
            checkpoint(f"訓練 {str(value['phase']).upper()}：{value['epoch']} / {value['epochs']} 回合")

        checkpoint("訓練 BC 候選模型")
        from .behavior_cloning import train_exact_main_action_artifact
        bc = train_exact_main_action_artifact(stages_path=Path(audit["stages_path"]), spec_path=spec_path,
            output_root=root / "bc", epochs=request.bc_epochs, batch_size=request.batch_size, seed=request.seed,
            training_callback=epoch)
        report["bc_acceptance"] = bc["acceptance"]
        report["bc_report_path"] = str(root / "bc" / "exact_main_action_bc_report.json")
        report["phases"].append("bc-trained-and-validated")
        if not bc["acceptance"]["shadow_ready"]:
            return finish("validation-failed", ("bc-heldout-gate-failed", *bc["acceptance"]["shadow_blockers"]))
        checkpoint("訓練 RL 候選並對照新 BC 基準")
        from .offline_rl_learner import train_discrete_iql_artifact
        rl = train_discrete_iql_artifact(stages_path=Path(audit["stages_path"]), spec_path=spec_path,
            anchor_model_path=root / "bc" / "exact_main_action_bc_model.npz",
            anchor_report_path=root / "bc" / "exact_main_action_bc_report.json", output_root=root / "rl",
            epochs=request.rl_epochs, batch_size=request.batch_size, seed=request.seed,
            actor_warmup_epochs=min(5, request.rl_epochs - 1),
            training_callback=epoch)
        report["rl_acceptance"] = rl["acceptance"]
        report["rl_report_path"] = str(root / "rl" / "exact_main_action_iql_report.json")
        report["phases"].append("rl-trained-and-validated")
        rl_ready = rl["acceptance"]["shadow_ready"] and rl["acceptance"]["iql_validated"]
        report["enabled_policy"] = "rl-to-bc" if rl_ready else "bc"
        report["warnings"] = [] if rl_ready else ["RL 僅保留診斷，候選使用已通過驗證的 BC。", *rl["acceptance"]["shadow_blockers"]]
        checkpoint("驗證版本鎖並建立可選候選模型包")
        if read_card_data_snapshot(request.database)["fingerprint"] != feature_contract["snapshot_fingerprint"]:
            return finish("blocked", ("master-changed-during-model-training",))
        packaged = publish_existing_model_update_candidate(root, bundle_root=request.bundle_root,
                                                          database=request.database)
        report.update(_object(report_path))
        return finish("candidate-ready", bundle_path=packaged.bundle_path)
    except ModelUpdateCancelled:
        return finish("cancelled", ("user-cancelled",))
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        return finish("blocked", (report["error"],))
    finally:
        _LOCK.release()


def publish_existing_model_update_candidate(job_root: Path, *, bundle_root: Path = DEFAULT_BUNDLE_ROOT,
                                            database: Path = DEFAULT_DATABASE) -> ModelUpdateResult:
    """Publish already trained component results without changing their gates.

    An accepted BC can be selected independently of an unvalidated RL critic.
    This also finishes a job interrupted after training and before packaging.
    It does not retrain, rewrite acceptance reports, or activate the bundle.
    """
    root = Path(job_root).resolve()
    report_path = root / "report.json"
    report = _object(report_path)
    if report.get("schema") != SCHEMA:
        raise ValueError("not a model-update report")
    spec_path = root / "training_spec.json"
    spec = _object(spec_path)
    contract = spec["card_feature_contract"]
    load_card_semantic_features(contract, relative_to=root)
    if read_card_data_snapshot(database)["fingerprint"] != contract["snapshot_fingerprint"]:
        raise ValueError("current Master differs from the trained feature version")
    bc = _object(root / "bc" / "exact_main_action_bc_report.json")
    rl = _object(root / "rl" / "exact_main_action_iql_report.json")
    if bc["acceptance"].get("shadow_ready") is not True:
        raise ValueError("BC held-out validation did not pass")
    rl_ready = rl["acceptance"].get("shadow_ready") is True and rl["acceptance"].get("iql_validated") is True
    source = report.get("source_bundle_path")
    base = PolicyBundle.load(None if source is None else _path(source))
    bundle_path = bundle_root / str(report["job_id"]) / "manifest.json"
    write_policy_bundle_manifest(bundle_path, bundle_id=str(report["job_id"]), spec_path=spec_path,
        outer_model_path=base.model_path("outer_policy"), outer_report_path=base.report_path("outer_policy"),
        exact_model_path=root / "bc" / "exact_main_action_bc_model.npz",
        exact_report_path=root / "bc" / "exact_main_action_bc_report.json", replay_model_path=None, replay_report_path=None,
        exact_live_apply_allowed=True, offline_rl_model_path=root / "rl" / "exact_main_action_iql_model.npz",
        offline_rl_report_path=root / "rl" / "exact_main_action_iql_report.json", offline_rl_runtime_enabled=rl_ready)
    PolicyBundle.load(bundle_path)
    report.update(status="candidate-ready", bundle_path=str(bundle_path), blockers=[],
                  enabled_policy="rl-to-bc" if rl_ready else "bc",
                  warnings=[] if rl_ready else ["RL 僅保留診斷，候選使用已通過驗證的 BC。", *rl["acceptance"]["shadow_blockers"]])
    if "candidate-bundle-verified" not in report["phases"]:
        report["phases"].append("candidate-bundle-verified")
    atomic_write(report_path, canonical_json_bytes(report) + b"\n")
    atomic_write(root.parent / "latest_job.json", canonical_json_bytes({"schema": SCHEMA,
        "status": "candidate-ready", "report_path": str(report_path), "bundle_path": str(bundle_path), "blockers": []}) + b"\n")
    return ModelUpdateResult("candidate-ready", report_path, bundle_path, ())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a manually selected card-semantic BC/RL candidate")
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--bc-epochs", type=int, default=60)
    parser.add_argument("--rl-epochs", type=int, default=30)
    args = parser.parse_args(argv)
    result = run_model_update(ModelUpdateRequest(source_spec_path=args.spec, bc_epochs=args.bc_epochs, rl_epochs=args.rl_epochs), progress=print)
    print(json.dumps({"status": result.status, "report_path": str(result.report_path), "blockers": result.blockers}))
    return 0 if result.status == "candidate-ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
