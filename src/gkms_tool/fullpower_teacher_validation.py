"""Read frozen simulator teachers without changing their evidence authority.

This is the training boundary, separate from the reconstruction worker. It has
no game client, process audit hook, current-Master default, or native-RL path.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from .training_artifact_io import sha256_file

DATASET_SCHEMA = "gkms.simulator-teacher-bc-dataset.v1"
ROW_SCHEMA = "gkms.simulator-teacher-transition.v1"
SPLITS = frozenset({"train", "validation", "test"})


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf8")).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate teacher JSON key: " + key)
            result[key] = value
        return result
    value = json.loads(path.read_text(encoding="utf8"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("teacher artifact must be a JSON object")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _frozen_file(root: Path, value: str, expected: str | None = None) -> Path:
    path = Path(value)
    path = path.resolve() if path.is_absolute() else (root / path).resolve()
    _require(path.is_relative_to(root), "teacher references a mutable file outside its frozen directory")
    _require(path.is_file(), "teacher source file is missing")
    if expected is not None:
        _require(sha256_file(path) == expected, "teacher source file hash mismatch")
    return path


def _rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


@dataclass(frozen=True)
class ValidatedTeacherDataset:
    manifest_path: Path
    manifest_sha256: str
    rows: tuple[dict[str, Any], ...]
    trajectory_splits: dict[str, str]
    source_episodes: dict[str, dict[str, Any]]

    @property
    def split_counts(self) -> dict[str, int]:
        return dict(Counter(row["source"]["split"] for row in self.rows))

    def require_holdout(self) -> None:
        counts = self.split_counts
        _require(all(counts.get(split, 0) > 0 for split in SPLITS),
                 "training requires original train/validation/test trajectories; do not re-split a single replay")


def _validate_episode(entry, root):
    report_path = _frozen_file(root, entry["report_path"], entry["report_sha256"])
    transition_path = _frozen_file(root, entry["source_transitions_path"], entry["source_transitions_sha256"])
    original_path = _frozen_file(root, entry["frozen_original_history_episode_path"])
    report, original = _read(report_path), _read(original_path)
    source = report["source"]
    _require(_digest(original) == entry["source_row_sha256"] == source["original_row_sha256"],
             "teacher original History row hash mismatch")
    for key in ("episode_id", "trajectory_id"):
        _require(entry[key] == source[key] == original[key], "teacher episode identity mismatch")
    _require(source["split"] == entry["split"] == original["freeze"]["split"]["assignment"]
             and source["split"] in SPLITS, "teacher changes original trajectory split")
    for key in ("master_hash", "master_version"):
        _require(source[key] == original[key], "teacher changes original Master version")
    _require(report.get("native_evidence_claimed") is False
             and report.get("native_legal_pool_claimed") is False,
             "simulator teacher cannot claim native evidence")
    _require(all(report.get(key) is True for key in ("complete", "terminal_score_matches",
             "local_terminal_reached", "local_trajectory_continuity")), "teacher episode did not finish validation")
    footprint = report.get("master_access_footprint", {})
    _require(footprint.get("policy") == "record-and-reject-unbound-Master-file-access"
             and footprint.get("rejected") == [] and bool(footprint.get("paths")),
             "teacher has no clean version-bound Master footprint")
    rows = _rows(transition_path)
    _require(bool(rows), "teacher episode has no transitions")
    cursor = 0
    for index, row in enumerate(rows):
        _require(row.get("schema") == ROW_SCHEMA and row.get("source") == source,
                 "teacher transition source differs from its episode")
        _require(row.get("native_state_claimed") is False and row.get("native_legal_pool_claimed") is False
                 and row.get("evidence_authority") == "simulator-teacher", "teacher row changes authority")
        _require(row["before_sha256"] == _digest(row["sim_state_before"])
                 and row["after_sha256"] == _digest(row["sim_state_after"]), "teacher state hash mismatch")
        if index:
            _require(rows[index - 1]["after_sha256"] == row["before_sha256"], "teacher trajectory is discontinuous")
        _require(row["action_ordinal"] == cursor and cursor < len(original["actions"]),
                 "teacher skips an original primary action")
        action = original["actions"][cursor]
        _require(row["original_action"] == action and row["chosen"]["kind"] == action["action_type"],
                 "teacher chosen command differs from original action")
        expected_slot = action["indexes"][0] if action["indexes"] else None
        _require(row["chosen"].get("slot_index") == expected_slot, "teacher chosen slot differs from original action")
        cursor += 1
        while cursor < len(original["actions"]) and original["actions"][cursor]["action_type"] == "effect-card-select":
            cursor += 1
    _require(cursor == len(original["actions"]) == report["reconstructed_actions"], "teacher original action stream incomplete")
    _require(rows[-1]["sim_state_after"]["scalar"]["turns_remaining"] == 0
             and rows[-1]["sim_state_after"]["scalar"]["score"] == original["terminal_score"]
             == report["terminal_score_reconstructed"] == report["terminal_score_recorded"],
             "teacher terminal state does not match recorded score")
    _require(_digest(report["initial_state"]) == rows[0]["before_sha256"], "teacher initial state changed")
    return source, {(r["source"]["episode_id"], r["action_ordinal"]): r for r in rows}, report


def load_validated_teacher_dataset(manifest_path: Path) -> ValidatedTeacherDataset:
    """Validate frozen files and original splits before any model sees a row."""
    manifest_path = Path(manifest_path).resolve()
    root, manifest = manifest_path.parent, _read(manifest_path)
    _require(manifest.get("schema") == DATASET_SCHEMA and manifest.get("authority") == "simulator-teacher"
             and manifest.get("supervision") == "behavior-cloning", "unsupported teacher dataset contract")
    for field in ("native_evidence_claimed", "native_legal_mask_claimed", "native_offline_rl_manifest"):
        _require(manifest.get(field) is False, "teacher dataset cannot be relabelled native")
    source_path = _frozen_file(root, manifest["source_manifest_path"], manifest["source_manifest_sha256"])
    source_manifest = _read(source_path)
    _require(source_manifest.get("schema") == "gkms.simulator-teacher-source-manifest.v1", "unknown teacher source schema")
    source_rows, sources, reports, splits = {}, {}, {}, {}
    for entry in source_manifest["episodes"]:
        source, episode_rows, report = _validate_episode(entry, root)
        episode, trajectory = source["episode_id"], source["trajectory_id"]
        _require(episode not in sources, "teacher episode was duplicated")
        _require(splits.setdefault(trajectory, source["split"]) == source["split"], "teacher trajectory crosses splits")
        sources[episode], reports[episode] = source, (entry, report)
        source_rows.update(episode_rows)
    rows_path = _frozen_file(root, manifest["teacher_rows_path"], manifest["teacher_rows_sha256"])
    selected, seen = _rows(rows_path), set()
    for row in selected:
        key = row["source"]["episode_id"], row["action_ordinal"]
        _require(key not in seen and key in source_rows, "teacher example duplicated or not in a validated episode")
        seen.add(key)
        original = source_rows[key]
        strip = lambda item: {k: v for k, v in item.items() if k not in {"validation_receipt", "training_admission"}}
        _require(strip(row) == strip(original), "teacher example differs from its frozen transition")
        _require(row.get("training_admission") == "validated-simulator-teacher-only", "teacher example was not admitted")
        pool = row["legal_candidates"]
        _require(pool.get("authority") == "simulator-enumeration" and pool.get("complete") is True
                 and pool.get("unresolved") == [] and len(pool["rows"]) >= 2,
                 "teacher example has an incomplete local candidate pool")
        _require(sum(c == row["chosen"] for c in pool["rows"]) == 1, "teacher target does not bind to one local candidate")
        entry, _ = reports[key[0]]
        _require(row["validation_receipt"] == {"episode_report_sha256": entry["report_sha256"],
                 "terminal_score_matches": True, "original_episode_sha256": entry["source_row_sha256"]},
                 "teacher validation receipt changed")
    _require(len(selected) == manifest["row_count"] and bool(selected), "teacher row count mismatch")
    _require(dict(Counter(row["source"]["split"] for row in selected)) == manifest["split_row_counts"], "teacher split counts mismatch")
    _require(splits == manifest["trajectory_splits"] and len(splits) == manifest["trajectory_count"], "teacher trajectory inventory mismatch")
    return ValidatedTeacherDataset(manifest_path, sha256_file(manifest_path), tuple(selected), splits, sources)
