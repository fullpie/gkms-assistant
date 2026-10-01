"""Freeze the local leaderboard episode union and its canonical labels.

Training identity is ``(trajectory_id, audition_index)``.  Repeated copies of
the same identity and action stream merge provenance; different trajectories
are never collapsed merely because they share an action-index sequence.  An
identity with conflicting flow or actions is excluded and recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .canonical_training_labels import (
    LEADERBOARD_EPISODE_SCHEMA,
    build_canonical_training_labels,
    default_existing_sources,
)
from .training_artifact_io import (
    atomic_write as _atomic_write,
    canonical_json_bytes as _canonical_bytes,
    sha256_file as _sha256_file,
)
from .training_coverage_matrix import ARCHETYPES, MODES


MANIFEST_SCHEMA: Final = "gkms.frozen-training-dataset-manifest.v1"
EPISODE_METADATA_SCHEMA: Final = "gkms.frozen-training-episode.v1"
CONFLICT_SCHEMA: Final = "gkms.frozen-training-episode-conflict.v1"
INDEX_SCHEMA: Final = "gkms.frozen-training-episode-index.v1"
PROVENANCE_SCHEMA: Final = "gkms.frozen-training-provenance.v1"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_LEADERBOARD_ROOT: Final = PROJECT_ROOT / "var" / "leaderboard_dataset"
DEFAULT_OUTPUT: Final = PROJECT_ROOT / "var" / "training_dataset" / "frozen_v1"

_MODE_BY_PRODUCE: Final = dict(MODES)
_ARCHETYPE_BY_PLAN_EFFECT: Final = {
    (plan_type, effect): archetype
    for archetype, plan_type, effect in ARCHETYPES
}


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _display_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def discover_episode_sources(
    root: Path = DEFAULT_LEADERBOARD_ROOT,
) -> tuple[Path, ...]:
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(root)
    return tuple(
        sorted(
            {
                *root.rglob("episodes.jsonl"),
                *root.glob("runtime_replay_*.jsonl"),
            },
            key=lambda value: value.as_posix().casefold(),
        )
    )


def _episode_identity(row: Mapping[str, Any]) -> tuple[str, str, int]:
    trajectory_id = row.get("trajectory_id")
    audition_index = row.get("audition_index")
    if not isinstance(trajectory_id, str) or not trajectory_id:
        raise ValueError("leaderboard episode has no trajectory_id")
    if (
        not isinstance(audition_index, int)
        or isinstance(audition_index, bool)
        or audition_index not in {0, 1, 2}
    ):
        raise ValueError("leaderboard episode has invalid audition_index")
    return (
        f"{trajectory_id}:audition:{audition_index}",
        trajectory_id,
        audition_index,
    )


def _flow(row: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    produce_id = row.get("produce_id")
    plan_type = row.get("plan_type")
    effect = row.get("exam_effect_type")
    archetype = _ARCHETYPE_BY_PLAN_EFFECT.get((plan_type, effect))
    if (
        not isinstance(produce_id, str)
        or produce_id not in _MODE_BY_PRODUCE
        or not isinstance(plan_type, str)
        or not isinstance(effect, str)
        or archetype is None
    ):
        return None, produce_id if isinstance(produce_id, str) else None, None
    return f"{produce_id}|{plan_type}|{effect}", produce_id, archetype


def _variant(row: Mapping[str, Any]) -> tuple[str, str, int]:
    actions = row.get("actions")
    if not isinstance(actions, list):
        raise ValueError("leaderboard episode actions must be an array")
    action_sha = _digest(actions)
    variant_sha = _digest(
        {
            "produce_id": row.get("produce_id"),
            "plan_type": row.get("plan_type"),
            "exam_effect_type": row.get("exam_effect_type"),
            "actions": actions,
        }
    )
    return action_sha, variant_sha, len(actions)


def _payload_core(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in row.items()
        if key not in {"sources", "freeze"}
    }


def _payload_relation(
    existing: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> str:
    """Return equal, existing-superset, candidate-superset, or conflict."""

    left = _payload_core(existing)
    right = _payload_core(candidate)
    left_in_right = all(key in right and right[key] == value for key, value in left.items())
    right_in_left = all(key in left and left[key] == value for key, value in right.items())
    if left_in_right and right_in_left:
        return "equal"
    if left_in_right:
        return "candidate-superset"
    if right_in_left:
        return "existing-superset"
    return "conflict"


def _split_for_trajectory(trajectory_id: str) -> dict[str, object]:
    group_id = f"split:{_digest({'basis': 'trajectory', 'value': trajectory_id})}"
    bucket = int(hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:8], 16) % 100
    assignment = "train" if bucket < 80 else "validation" if bucket < 90 else "test"
    return {
        "group_id": group_id,
        "basis": "trajectory",
        "assignment": assignment,
        "bucket": bucket,
        "player_leakage_risk": True,
    }


@dataclass
class _EpisodeChoice:
    episode_id: str
    trajectory_id: str
    audition_index: int
    flow: str | None
    produce_id: str | None
    archetype: str | None
    action_sha256: str
    variant_sha256: str
    content_sha256: str
    action_count: int
    row: dict[str, Any]
    selected_source_id: str
    selected_line_number: int
    selected_record_sha256: str
    source_ids: set[str] = field(default_factory=set)
    declared_sources: set[str] = field(default_factory=set)

@dataclass
class _Conflict:
    episode_id: str
    reasons: set[str] = field(default_factory=set)
    variants: dict[str, set[str]] = field(default_factory=dict)

    def add(self, variant_sha256: str, source_id: str) -> None:
        self.variants.setdefault(variant_sha256, set()).add(source_id)


@dataclass(frozen=True, slots=True)
class _Occurrence:
    source_id: str
    line_number: int
    record_sha256: str
    episode_id: str | None
    variant_sha256: str | None
    content_sha256: str | None
    initial_status: str


def _source_id(index: int) -> str:
    return f"source-{index:04d}"


def _summarize_choices(
    choices: Iterable[_EpisodeChoice],
) -> dict[str, object]:
    values = tuple(choices)
    trajectories: dict[str, list[int]] = defaultdict(list)
    action_count = 0
    for choice in values:
        trajectories[choice.trajectory_id].append(choice.audition_index)
        action_count += choice.action_count
    complete = sum(
        1 for auditions in trajectories.values() if sorted(auditions) == [0, 1, 2]
    )
    return {
        "episode_count": len(values),
        "trajectory_count": len(trajectories),
        "complete_trajectory_count": complete,
        "incomplete_trajectory_count": len(trajectories) - complete,
        "action_count": action_count,
    }


def _canonical_non_episode_sources(project_root: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in default_existing_sources(project_root)
        if path.name != "episodes.jsonl"
    )


def build_frozen_training_dataset(
    *,
    source_paths: Sequence[Path] | None = None,
    leaderboard_root: Path = DEFAULT_LEADERBOARD_ROOT,
    output_dir: Path = DEFAULT_OUTPUT,
    project_root: Path = PROJECT_ROOT,
    include_canonical: bool = True,
) -> dict[str, object]:
    sources = (
        tuple(Path(path).resolve() for path in source_paths)
        if source_paths is not None
        else discover_episode_sources(leaderboard_root)
    )
    sources = tuple(sorted(set(sources), key=lambda value: str(value).casefold()))
    if not sources:
        raise ValueError("frozen training dataset has no episode sources")
    if any(not path.is_file() for path in sources):
        missing = [str(path) for path in sources if not path.is_file()]
        raise FileNotFoundError(missing)

    output_root = Path(output_dir).resolve()
    episodes_path = output_root / "episodes.jsonl"
    episode_index_path = output_root / "episode_index.jsonl"
    provenance_path = output_root / "provenance.jsonl"
    conflicts_path = output_root / "conflicts.jsonl"
    manifest_path = output_root / "manifest.json"
    checksums_path = output_root / "checksums.sha256"
    canonical_root = output_root / "canonical"
    targets = {
        episodes_path.resolve(),
        episode_index_path.resolve(),
        provenance_path.resolve(),
        conflicts_path.resolve(),
        manifest_path.resolve(),
        checksums_path.resolve(),
    }
    overlap = targets.intersection(path.resolve() for path in sources)
    if overlap:
        raise ValueError(f"frozen output overlaps input: {sorted(map(str, overlap))}")
    output_root.mkdir(parents=True, exist_ok=True)

    source_metadata: list[dict[str, object]] = []
    source_id_by_path = {
        path: _source_id(index) for index, path in enumerate(sources, start=1)
    }
    choices: dict[str, _EpisodeChoice] = {}
    conflicts: dict[str, _Conflict] = {}
    occurrences: list[_Occurrence] = []
    invalid_rows = 0
    non_episode_rows = 0
    raw_rows = 0
    duplicate_source_rows = 0
    richer_replacements = 0

    for path in sources:
        source_id = source_id_by_path[path]
        file_rows = 0
        file_episode_rows = 0
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                raw_rows += 1
                file_rows += 1
                record_sha256 = hashlib.sha256(stripped.encode("utf-8")).hexdigest()
                try:
                    decoded = json.loads(stripped)
                except json.JSONDecodeError:
                    invalid_rows += 1
                    occurrences.append(
                        _Occurrence(
                            source_id,
                            line_number,
                            record_sha256,
                            None,
                            None,
                            None,
                            "invalid-json",
                        )
                    )
                    continue
                if (
                    not isinstance(decoded, Mapping)
                    or decoded.get("schema") != LEADERBOARD_EPISODE_SCHEMA
                ):
                    non_episode_rows += 1
                    occurrences.append(
                        _Occurrence(
                            source_id,
                            line_number,
                            record_sha256,
                            None,
                            None,
                            None,
                            "non-episode",
                        )
                    )
                    continue
                try:
                    episode_id, trajectory_id, audition_index = _episode_identity(decoded)
                    action_sha, variant_sha, action_count = _variant(decoded)
                except ValueError:
                    invalid_rows += 1
                    occurrences.append(
                        _Occurrence(
                            source_id,
                            line_number,
                            record_sha256,
                            None,
                            None,
                            None,
                            "invalid-episode",
                        )
                    )
                    continue
                file_episode_rows += 1
                flow, produce_id, archetype = _flow(decoded)
                row = dict(decoded)
                row.pop("freeze", None)
                content_sha256 = _digest(_payload_core(row))
                occurrences.append(
                    _Occurrence(
                        source_id,
                        line_number,
                        record_sha256,
                        episode_id,
                        variant_sha,
                        content_sha256,
                        "episode",
                    )
                )
                declared = {
                    str(value)
                    for value in row.get("sources", [])
                    if isinstance(value, str) and value
                }
                if episode_id in conflicts:
                    conflicts[episode_id].add(content_sha256, source_id)
                    continue
                existing = choices.get(episode_id)
                if existing is None:
                    choices[episode_id] = _EpisodeChoice(
                        episode_id=episode_id,
                        trajectory_id=trajectory_id,
                        audition_index=audition_index,
                        flow=flow,
                        produce_id=produce_id,
                        archetype=archetype,
                        action_sha256=action_sha,
                        variant_sha256=variant_sha,
                        content_sha256=content_sha256,
                        action_count=action_count,
                        row=row,
                        selected_source_id=source_id,
                        selected_line_number=line_number,
                        selected_record_sha256=record_sha256,
                        source_ids={source_id},
                        declared_sources=declared,
                    )
                    continue
                if existing.variant_sha256 != variant_sha:
                    conflict = _Conflict(episode_id)
                    conflict.reasons.add("action-or-flow-conflict")
                    conflict.add(existing.content_sha256, existing.selected_source_id)
                    for prior_source in existing.source_ids:
                        conflict.add(existing.content_sha256, prior_source)
                    conflict.add(content_sha256, source_id)
                    conflicts[episode_id] = conflict
                    choices.pop(episode_id, None)
                    continue
                relation = _payload_relation(existing.row, row)
                if relation == "conflict":
                    conflict = _Conflict(episode_id)
                    conflict.reasons.add("payload-conflict")
                    conflict.add(existing.content_sha256, existing.selected_source_id)
                    for prior_source in existing.source_ids:
                        conflict.add(existing.content_sha256, prior_source)
                    conflict.add(content_sha256, source_id)
                    conflicts[episode_id] = conflict
                    choices.pop(episode_id, None)
                    continue
                duplicate_source_rows += 1
                existing.source_ids.add(source_id)
                existing.declared_sources.update(declared)
                if relation == "candidate-superset":
                    existing.row = row
                    existing.selected_source_id = source_id
                    existing.selected_line_number = line_number
                    existing.selected_record_sha256 = record_sha256
                    existing.content_sha256 = content_sha256
                    richer_replacements += 1
        source_metadata.append(
            {
                "source_id": source_id,
                "path": _display_path(path, project_root),
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
                "row_count": file_rows,
                "episode_row_count": file_episode_rows,
            }
        )

    ordered = tuple(choices[key] for key in sorted(choices))
    encoded_rows: list[bytes] = []
    index_rows: list[bytes] = []
    ordinal_by_episode: dict[str, int] = {}
    split_episode_counts: Counter[str] = Counter()
    split_trajectories: dict[str, set[str]] = defaultdict(set)
    for ordinal, choice in enumerate(ordered):
        ordinal_by_episode[choice.episode_id] = ordinal
        split = _split_for_trajectory(choice.trajectory_id)
        assignment = str(split["assignment"])
        split_episode_counts[assignment] += 1
        split_trajectories[assignment].add(choice.trajectory_id)
        row = dict(choice.row)
        if choice.declared_sources:
            row["sources"] = sorted(choice.declared_sources)
        row["episode_id"] = choice.episode_id
        row["freeze"] = {
            "schema": EPISODE_METADATA_SCHEMA,
            "episode_id": choice.episode_id,
            "action_sha256": choice.action_sha256,
            "variant_sha256": choice.variant_sha256,
            "source_ids": sorted(choice.source_ids),
            "source_count": len(choice.source_ids),
            "selected_source_id": choice.selected_source_id,
            "selected_line_number": choice.selected_line_number,
            "selected_record_sha256": choice.selected_record_sha256,
            "content_sha256": choice.content_sha256,
            "training_scope": (
                "five_archetype" if choice.flow is not None else "out_of_scope"
            ),
            "flow": choice.flow,
            "archetype": choice.archetype,
            "split": split,
        }
        encoded_rows.append(_canonical_bytes(row) + b"\n")
        index_rows.append(
            _canonical_bytes(
                {
                    "schema": INDEX_SCHEMA,
                    "episode_id": choice.episode_id,
                    "output_ordinal": ordinal,
                    "trajectory_id": choice.trajectory_id,
                    "audition_index": choice.audition_index,
                    "flow": choice.flow,
                    "archetype": choice.archetype,
                    "training_in_scope": choice.flow is not None,
                    "action_sha256": choice.action_sha256,
                    "content_sha256": choice.content_sha256,
                    "source_ids": sorted(choice.source_ids),
                    "selected_source_id": choice.selected_source_id,
                    "split": split,
                }
            )
            + b"\n"
        )
    _atomic_write(episodes_path, b"".join(encoded_rows))
    _atomic_write(episode_index_path, b"".join(index_rows))

    conflict_rows = []
    for episode_id in sorted(conflicts):
        conflict = conflicts[episode_id]
        conflict_rows.append(
            _canonical_bytes(
                {
                    "schema": CONFLICT_SCHEMA,
                    "episode_id": episode_id,
                    "reasons": sorted(conflict.reasons),
                    "variants": [
                        {
                            "variant_sha256": variant,
                            "source_ids": sorted(source_ids),
                        }
                        for variant, source_ids in sorted(conflict.variants.items())
                    ],
                }
            )
            + b"\n"
        )
    _atomic_write(conflicts_path, b"".join(conflict_rows))

    provenance_rows: list[bytes] = []
    provenance_dispositions: Counter[str] = Counter()
    for occurrence in occurrences:
        disposition = occurrence.initial_status
        output_ordinal: int | None = None
        if occurrence.episode_id is not None:
            if occurrence.episode_id in conflicts:
                disposition = "conflict"
            else:
                selected = choices.get(occurrence.episode_id)
                if selected is None:
                    disposition = "excluded"
                elif (
                    occurrence.source_id == selected.selected_source_id
                    and occurrence.line_number == selected.selected_line_number
                    and occurrence.record_sha256 == selected.selected_record_sha256
                ):
                    disposition = "selected"
                    output_ordinal = ordinal_by_episode[occurrence.episode_id]
                else:
                    disposition = "duplicate-source"
                    output_ordinal = ordinal_by_episode[occurrence.episode_id]
        provenance_dispositions[disposition] += 1
        provenance_rows.append(
            _canonical_bytes(
                {
                    "schema": PROVENANCE_SCHEMA,
                    "source_id": occurrence.source_id,
                    "line_number": occurrence.line_number,
                    "record_sha256": occurrence.record_sha256,
                    "episode_id": occurrence.episode_id,
                    "variant_sha256": occurrence.variant_sha256,
                    "content_sha256": occurrence.content_sha256,
                    "disposition": disposition,
                    "output_ordinal": output_ordinal,
                }
            )
            + b"\n"
        )
    _atomic_write(provenance_path, b"".join(provenance_rows))

    five_flow = tuple(choice for choice in ordered if choice.flow is not None)
    out_of_scope = tuple(choice for choice in ordered if choice.flow is None)
    by_flow: dict[str, dict[str, object]] = {}
    for produce_id, _mode in MODES:
        for archetype, plan_type, effect in ARCHETYPES:
            flow = f"{produce_id}|{plan_type}|{effect}"
            by_flow[flow] = _summarize_choices(
                choice for choice in five_flow if choice.flow == flow
            )

    freeze_ready = (
        bool(ordered)
        and invalid_rows == 0
        and non_episode_rows == 0
        and not conflicts
    )
    canonical_manifest: Mapping[str, Any] | None = None
    if include_canonical:
        if not freeze_ready:
            raise ValueError("conflicted or invalid episode union cannot be canonicalized")
        canonical_inputs = (
            episodes_path,
            *_canonical_non_episode_sources(project_root),
        )
        canonical_manifest = build_canonical_training_labels(
            canonical_inputs,
            canonical_root,
        )

    manifest: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "freeze_ready": freeze_ready,
        "contract": {
            "episode_identity": "trajectory_id + audition_index",
            "duplicate_policy": "same identity and flow/action stream merges provenance",
            "conflict_policy": "same identity with different flow/action stream is excluded",
            "trajectory_policy": "different trajectory IDs are never action-signature deduplicated",
            "split_policy": "trajectory-grouped sha256 80/10/10",
            "out_of_scope_policy": "retained in frozen episodes; excluded by training_scope",
        },
        "counts": {
            "raw_rows": raw_rows,
            "episode_count": len(ordered),
            "duplicate_source_rows": duplicate_source_rows,
            "richer_replacements": richer_replacements,
            "invalid_rows": invalid_rows,
            "non_episode_rows": non_episode_rows,
            "conflict_episode_count": len(conflicts),
            "all_archetypes": _summarize_choices(ordered),
            "five_archetype": _summarize_choices(five_flow),
            "out_of_scope": _summarize_choices(out_of_scope),
            "by_flow": by_flow,
            "split_episode_counts": dict(sorted(split_episode_counts.items())),
            "split_trajectory_counts": {
                key: len(value) for key, value in sorted(split_trajectories.items())
            },
            "provenance_dispositions": dict(
                sorted(provenance_dispositions.items())
            ),
        },
        "sources": source_metadata,
        "artifacts": {
            "episodes": {
                "path": "episodes.jsonl",
                "bytes": episodes_path.stat().st_size,
                "sha256": _sha256_file(episodes_path),
            },
            "conflicts": {
                "path": "conflicts.jsonl",
                "bytes": conflicts_path.stat().st_size,
                "sha256": _sha256_file(conflicts_path),
            },
            "episode_index": {
                "path": "episode_index.jsonl",
                "bytes": episode_index_path.stat().st_size,
                "sha256": _sha256_file(episode_index_path),
            },
            "provenance": {
                "path": "provenance.jsonl",
                "bytes": provenance_path.stat().st_size,
                "sha256": _sha256_file(provenance_path),
            },
            "canonical": (
                {
                    "path": "canonical",
                    "counts": canonical_manifest.get("counts"),
                    "artifacts": canonical_manifest.get("artifacts"),
                    "artifact_hashes": canonical_manifest.get("artifact_hashes"),
                }
                if canonical_manifest is not None
                else None
            ),
        },
    }
    manifest["manifest_content_sha256"] = _digest(manifest)
    _atomic_write(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )

    checksum_targets = [
        episodes_path,
        episode_index_path,
        provenance_path,
        conflicts_path,
        manifest_path,
    ]
    if canonical_manifest is not None:
        checksum_targets.extend(
            [
                canonical_root / "labels.jsonl",
                canonical_root / "manifest.json",
                canonical_root / "checksums.sha256",
            ]
        )
    checksum_lines = []
    for path in checksum_targets:
        relative = path.relative_to(output_root).as_posix()
        checksum_lines.append(f"{_sha256_file(path)}  {relative}\n")
    _atomic_write(checksums_path, "".join(checksum_lines).encode("ascii"))

    result = dict(manifest)
    result["artifact_hashes"] = {
        "manifest.json": _sha256_file(manifest_path),
        "checksums.sha256": _sha256_file(checksums_path),
    }
    return result


def verify_frozen_training_dataset(
    dataset_dir: Path = DEFAULT_OUTPUT,
    *,
    project_root: Path = PROJECT_ROOT,
    verify_sources: bool = True,
) -> dict[str, object]:
    root = Path(dataset_dir).resolve()
    manifest_path = root / "manifest.json"
    checksums_path = root / "checksums.sha256"
    if not manifest_path.is_file() or not checksums_path.is_file():
        raise FileNotFoundError("frozen dataset manifest/checksums are missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, Mapping) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("frozen dataset manifest schema mismatch")
    declared_content_sha = manifest.get("manifest_content_sha256")
    without_content_sha = dict(manifest)
    without_content_sha.pop("manifest_content_sha256", None)
    if declared_content_sha != _digest(without_content_sha):
        raise ValueError("frozen dataset manifest content hash mismatch")

    checked_artifacts: list[str] = []
    for line in checksums_path.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        try:
            expected, relative = line.split("  ", 1)
        except ValueError as error:
            raise ValueError("invalid frozen checksums line") from error
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ValueError("frozen checksum target escapes dataset root") from error
        if not target.is_file() or _sha256_file(target) != expected:
            raise ValueError(f"frozen artifact checksum mismatch: {relative}")
        checked_artifacts.append(relative)

    counts = manifest.get("counts")
    if not isinstance(counts, Mapping):
        raise ValueError("frozen manifest has no counts")
    expected_lines = {
        "episodes.jsonl": counts.get("episode_count"),
        "episode_index.jsonl": counts.get("episode_count"),
        "provenance.jsonl": counts.get("raw_rows"),
        "conflicts.jsonl": counts.get("conflict_episode_count"),
    }
    for relative, expected in expected_lines.items():
        if not isinstance(expected, int) or isinstance(expected, bool):
            raise ValueError(f"frozen manifest count is invalid: {relative}")
        with (root / relative).open("r", encoding="utf-8-sig") as handle:
            actual = sum(1 for line in handle if line.strip())
        if actual != expected:
            raise ValueError(f"frozen artifact row count mismatch: {relative}")

    trajectory_splits: dict[str, str] = {}
    previous_episode_id: str | None = None
    with (root / "episodes.jsonl").open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, Mapping) or row.get("schema") != LEADERBOARD_EPISODE_SCHEMA:
                raise ValueError(f"invalid frozen episode at line {line_number}")
            frozen = row.get("freeze")
            if not isinstance(frozen, Mapping) or frozen.get("schema") != EPISODE_METADATA_SCHEMA:
                raise ValueError(f"missing frozen metadata at line {line_number}")
            episode_id, trajectory_id, _audition_index = _episode_identity(row)
            if frozen.get("episode_id") != episode_id:
                raise ValueError(f"frozen episode identity mismatch at line {line_number}")
            if previous_episode_id is not None and episode_id <= previous_episode_id:
                raise ValueError("frozen episodes are not strictly identity-sorted")
            previous_episode_id = episode_id
            action_sha, _variant_sha, _action_count = _variant(row)
            if frozen.get("action_sha256") != action_sha:
                raise ValueError(f"frozen action hash mismatch at line {line_number}")
            split = frozen.get("split")
            if not isinstance(split, Mapping):
                raise ValueError(f"frozen split is missing at line {line_number}")
            assignment = split.get("assignment")
            if not isinstance(assignment, str):
                raise ValueError(f"frozen split is invalid at line {line_number}")
            previous_assignment = trajectory_splits.setdefault(
                trajectory_id, assignment
            )
            if previous_assignment != assignment:
                raise ValueError("one trajectory crosses frozen splits")

    verified_sources = 0
    if verify_sources:
        sources = manifest.get("sources")
        if not isinstance(sources, list):
            raise ValueError("frozen manifest sources are missing")
        for value in sources:
            if not isinstance(value, Mapping):
                raise ValueError("invalid frozen source record")
            raw_path = value.get("path")
            expected_sha = value.get("sha256")
            if not isinstance(raw_path, str) or not isinstance(expected_sha, str):
                raise ValueError("invalid frozen source identity")
            source_path = Path(raw_path)
            if not source_path.is_absolute():
                source_path = Path(project_root) / source_path
            if not source_path.is_file() or _sha256_file(source_path) != expected_sha:
                raise ValueError(f"frozen source checksum mismatch: {raw_path}")
            verified_sources += 1

    return {
        "schema": "gkms.frozen-training-dataset-verification.v1",
        "verified": True,
        "freeze_ready": manifest.get("freeze_ready") is True,
        "checked_artifacts": sorted(checked_artifacts),
        "checked_artifact_count": len(checked_artifacts),
        "verified_source_count": verified_sources,
        "episode_count": counts.get("episode_count"),
        "trajectory_count": len(trajectory_splits),
        "manifest_sha256": _sha256_file(manifest_path),
        "checksums_sha256": _sha256_file(checksums_path),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze the identity-deduplicated local leaderboard training union"
    )
    parser.add_argument("--leaderboard-root", type=Path, default=DEFAULT_LEADERBOARD_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-canonical", action="store_true")
    parser.add_argument(
        "command", nargs="?", choices=("build", "verify"), default="build"
    )
    parser.add_argument("--no-source-verification", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "verify":
        result = verify_frozen_training_dataset(
            args.output,
            verify_sources=not args.no_source_verification,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    result = build_frozen_training_dataset(
        leaderboard_root=args.leaderboard_root,
        output_dir=args.output,
        include_canonical=not args.no_canonical,
    )
    print(
        json.dumps(
            {
                "schema": result["schema"],
                "freeze_ready": result["freeze_ready"],
                "counts": result["counts"],
                "artifact_hashes": result["artifact_hashes"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


__all__ = [
    "CONFLICT_SCHEMA",
    "DEFAULT_LEADERBOARD_ROOT",
    "DEFAULT_OUTPUT",
    "EPISODE_METADATA_SCHEMA",
    "MANIFEST_SCHEMA",
    "build_frozen_training_dataset",
    "discover_episode_sources",
    "main",
    "verify_frozen_training_dataset",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
