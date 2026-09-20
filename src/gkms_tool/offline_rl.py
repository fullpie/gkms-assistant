"""Fail-closed offline-RL stage assembly and training gate for GKMS."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from .canonical_training_labels import INNER_TRANSITION_SCHEMA, LABEL_SCHEMA
from .training_artifact_io import (
    atomic_write as _atomic_write,
    canonical_json_bytes as _canonical_bytes,
    sha256_file as _sha256_file,
)


AUDIT_SCHEMA: Final = "gkms.offline-rl-readiness-audit.v1"
STAGE_SCHEMA: Final = "gkms.offline-rl-stage.v1"
MANIFEST_SCHEMA: Final = "gkms.offline-rl-dataset-manifest.v1"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_LABELS: Final = (
    PROJECT_ROOT
    / "var"
    / "training_dataset"
    / "frozen_v1"
    / "canonical"
    / "labels.jsonl"
)
DEFAULT_READINESS: Final = (
    PROJECT_ROOT / "var" / "nia_training" / "full_rl_readiness.json"
)
DEFAULT_AUDIT_OUTPUT: Final = (
    PROJECT_ROOT / "var" / "offline_rl" / "frozen_v1" / "readiness_audit.json"
)
DEFAULT_DATASET_OUTPUT: Final = PROJECT_ROOT / "var" / "offline_rl" / "frozen_v1"


class OfflineRLNotReadyError(RuntimeError):
    pass


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _plain_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


@dataclass(frozen=True, slots=True)
class RLTransition:
    trajectory_id: str
    flow: str
    stage: str
    step: int
    state_before: Mapping[str, Any]
    action: object
    legal_candidates: tuple[object, ...]
    reward: int | float
    state_after: Mapping[str, Any]
    terminal: bool
    split: str
    source_id: str

    @property
    def before_sha256(self) -> str:
        return _digest(self.state_before)

    @property
    def after_sha256(self) -> str:
        return _digest(self.state_after)


@dataclass(frozen=True, slots=True)
class RLStage:
    stage_id: str
    trajectory_id: str
    flow: str
    stage: str
    split: str
    source_id: str
    transitions: tuple[RLTransition, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": STAGE_SCHEMA,
            "stage_id": self.stage_id,
            "trajectory_id": self.trajectory_id,
            "flow": self.flow,
            "stage": self.stage,
            "split": self.split,
            "source_id": self.source_id,
            "transition_count": len(self.transitions),
            "return": sum(value.reward for value in self.transitions),
            "terminal": True,
            "transitions": [
                {
                    "step": value.step,
                    "state_before": value.state_before,
                    "action": value.action,
                    "legal_candidates": list(value.legal_candidates),
                    "reward": value.reward,
                    "state_after": value.state_after,
                    "terminal": value.terminal,
                    "before_sha256": value.before_sha256,
                    "after_sha256": value.after_sha256,
                }
                for value in self.transitions
            ],
        }


def _flow(scope: Mapping[str, Any]) -> str:
    return "|".join(
        str(scope.get(key))
        for key in ("produce_id", "plan_type", "exam_effect_type")
    )


def _transition_from_label(row: Mapping[str, Any], *, allowed_flows: frozenset[str] | None = None) -> RLTransition | None:
    if not (
        row.get("schema") == LABEL_SCHEMA
        and row.get("source_schema") == INNER_TRANSITION_SCHEMA
        and _mapping(row.get("dedupe")).get("status") == "canonical"
        and _mapping(row.get("exact")).get("transition") is True
        and row.get("full_rl_policy_ready") is True
        and _mapping(row.get("quality")).get("training_eligible") is True
        and row.get("policy_surface") == "main-action-v1"
    ):
        return None
    legal = _mapping(row.get("legal"))
    if not (
        legal.get("candidate_scope") == "unified"
        and legal.get("complete") is True
        and legal.get("exact") is True
        and legal.get("chosen_member") is True
        and isinstance(legal.get("candidates"), list)
    ):
        return None
    transition = _mapping(row.get("transition"))
    before = transition.get("state_before")
    after = transition.get("state_after")
    action = transition.get("action")
    reward = _plain_number(_mapping(row.get("reward")).get("reward"))
    terminal = _mapping(row.get("reward")).get("terminal")
    identity = _mapping(row.get("identity"))
    trajectory_id = identity.get("trajectory_id")
    step = identity.get("step")
    scope = _mapping(row.get("scope"))
    if not (
        scope.get("mode") in {"nia_pro", "nia_master"}
        and (scope.get("archetype") in {
            "lesson_buff",
            "parameter_buff",
            "review",
            "card_play_aggressive",
            "concentration",
        } or (scope.get("archetype") == "full_power" and allowed_flows is not None and _flow(scope) in allowed_flows))
        and scope.get("stage") in {"Mid1", "Mid2", "Final"}
        and _mapping(row.get("join")).get("status")
        not in {"unresolved", "ambiguous"}
        and not row.get("ambiguous_fields")
    ):
        return None
    stage = scope.get("stage")
    split = _mapping(row.get("split")).get("assignment")
    source_id = row.get("capture_source_id")
    if not (
        isinstance(before, Mapping)
        and isinstance(after, Mapping)
        and action is not None
        and reward is not None
        and type(terminal) is bool
        and isinstance(trajectory_id, str)
        and trajectory_id
        and isinstance(step, int)
        and not isinstance(step, bool)
        and isinstance(stage, str)
        and stage
        and isinstance(split, str)
        and split in {"train", "validation", "test"}
        and isinstance(source_id, str)
        and source_id
    ):
        return None
    before_score = _plain_number(before.get("score"))
    after_score = _plain_number(after.get("score"))
    if before_score is None or after_score is None or reward != after_score - before_score:
        return None
    return RLTransition(
        trajectory_id=trajectory_id,
        flow=_flow(scope),
        stage=stage,
        step=step,
        state_before=before,
        action=action,
        legal_candidates=tuple(legal["candidates"]),
        reward=reward,
        state_after=after,
        terminal=terminal,
        split=split,
        source_id=source_id,
    )


def _assemble_group(
    values: Sequence[RLTransition],
) -> tuple[RLStage | None, tuple[str, ...]]:
    blockers: list[str] = []
    ordered = tuple(sorted(values, key=lambda value: value.step))
    if not ordered:
        return None, ("empty-stage",)
    steps = [value.step for value in ordered]
    if len(set(steps)) != len(steps):
        blockers.append("duplicate-step")
    first_state = ordered[0].state_before
    current_turn = first_state.get("current_turn")
    play_count = first_state.get(
        "exam_card_play_count", first_state.get("turn_card_play_count")
    )
    if current_turn != 1 or play_count not in {0, None}:
        blockers.append("stage-does-not-start-at-authoritative-turn-one")
    if steps != list(range(steps[0], steps[0] + len(steps))):
        blockers.append("step-gap")
    if len({value.split for value in ordered}) != 1:
        blockers.append("split-crossing")
    if len({value.flow for value in ordered}) != 1:
        blockers.append("flow-crossing")
    if len({value.source_id for value in ordered}) != 1:
        blockers.append("source-crossing")
    for current, following in zip(ordered, ordered[1:], strict=False):
        if current.after_sha256 != following.before_sha256:
            blockers.append("adjacent-state-discontinuity")
            break
    terminal_positions = [
        index for index, value in enumerate(ordered) if value.terminal
    ]
    if terminal_positions != [len(ordered) - 1]:
        blockers.append("terminal-not-once-last")
    if blockers:
        return None, tuple(sorted(set(blockers)))
    first = ordered[0]
    stage_id = "rl-stage:" + _digest(
        {
            "trajectory_id": first.trajectory_id,
            "flow": first.flow,
            "stage": first.stage,
            "steps": steps,
        }
    )
    return (
        RLStage(
            stage_id=stage_id,
            trajectory_id=first.trajectory_id,
            flow=first.flow,
            stage=first.stage,
            split=first.split,
            source_id=first.source_id,
            transitions=ordered,
        ),
        (),
    )


def audit_offline_rl_readiness(
    *,
    labels_path: Path = DEFAULT_LABELS,
    readiness_path: Path = DEFAULT_READINESS,
    min_transitions: int = 100,
    min_complete_stages: int = 10,
    min_independent_sources: int = 5,
    flow_scope: Sequence[str] | None = None,
    source_id_bindings: Mapping[str, str] | None = None,
) -> tuple[dict[str, object], tuple[RLStage, ...]]:
    labels_path = Path(labels_path)
    readiness_path = Path(readiness_path)
    if not labels_path.is_file():
        raise FileNotFoundError("offline-RL labels input is missing")
    allowed_flows = None
    if flow_scope is not None:
        from .training_spec import declared_training_scope
        allowed_flows = frozenset(declared_training_scope(flow_scope)["flows"])
    if source_id_bindings is not None and (allowed_flows is None or not source_id_bindings
            or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in source_id_bindings.items())):
        raise ValueError("source ID bindings require an explicit scoped source mapping")
    groups: dict[tuple[str, str, str], list[RLTransition]] = defaultdict(list)
    exact_nonfragment_transition_rows = 0
    main_action_exact_transition_rows = 0
    unified_row_level_rows = 0
    for line_number, line in enumerate(
        labels_path.open("r", encoding="utf-8-sig"), start=1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, Mapping) or row.get("schema") != LABEL_SCHEMA:
            raise ValueError(f"invalid canonical label at line {line_number}")
        if (
            row.get("source_schema") == INNER_TRANSITION_SCHEMA
            and _mapping(row.get("dedupe")).get("status") == "canonical"
            and _mapping(row.get("exact")).get("transition") is True
        ):
            exact_nonfragment_transition_rows += 1
        transition = _transition_from_label(row, allowed_flows=allowed_flows)
        if allowed_flows is not None and (transition is None or transition.flow not in allowed_flows):
            raise ValueError(f"scoped offline RL label is ineligible or outside declared flow at line {line_number}")
        if transition is None:
            continue
        if source_id_bindings is not None:
            if transition.source_id not in source_id_bindings:
                raise ValueError("scoped offline RL capture source has no verified original-source binding")
            transition = replace(transition, source_id=source_id_bindings[transition.source_id])
        main_action_exact_transition_rows += 1
        unified_row_level_rows += 1
        groups[
            (transition.trajectory_id, transition.flow, transition.stage)
        ].append(transition)
    stages: list[RLStage] = []
    group_blockers: Counter[str] = Counter()
    for values in groups.values():
        stage, blockers = _assemble_group(values)
        if stage is not None:
            stages.append(stage)
        for blocker in blockers:
            group_blockers[blocker] += 1
    stages.sort(key=lambda value: value.stage_id)
    stage_transition_count = sum(len(value.transitions) for value in stages)
    source_count = len({value.source_id for value in stages})
    split_stage_counts = Counter(value.split for value in stages)
    flow_stage_counts = Counter(value.flow for value in stages)
    readiness = (
        json.loads(readiness_path.read_text(encoding="utf-8-sig"))
        if readiness_path.is_file()
        else {}
    )
    legacy_readiness_blockers = sorted(
        str(value) for value in readiness.get("blockers", [])
    )
    blockers: set[str] = set()
    if allowed_flows is not None:
        blockers.update("scoped-stage:" + key for key in group_blockers)
        seen_cells = {(value.flow, value.stage, value.split) for value in stages}
        for flow in sorted(allowed_flows):
            for stage in ("Mid1", "Mid2", "Final"):
                for split in ("train", "validation", "test"):
                    if (flow, stage, split) not in seen_cells:
                        blockers.add(f"scoped-stage-split-missing:{flow}:{stage}:{split}")
        for attribute in ("trajectory_id", "source_id"):
            memberships: dict[str, set[str]] = defaultdict(set)
            for value in stages:
                memberships[getattr(value, attribute)].add(value.split)
            if any(len(splits) > 1 for splits in memberships.values()):
                blockers.add("scoped-cross-split:" + attribute)
    if stage_transition_count < min_transitions:
        blockers.add(
            "complete-stage-transition-count:"
            f"{stage_transition_count}<{min_transitions}"
        )
    if len(stages) < min_complete_stages:
        blockers.add(f"complete-stage-count:{len(stages)}<{min_complete_stages}")
    if source_count < min_independent_sources:
        blockers.add(
            f"complete-stage-independent-source-count:{source_count}<{min_independent_sources}"
        )
    if int(split_stage_counts.get("train", 0)) == 0:
        blockers.add("train-complete-stage-count:0")
    if int(split_stage_counts.get("validation", 0)) == 0:
        blockers.add("validation-complete-stage-count:0")
    if int(split_stage_counts.get("test", 0)) == 0:
        blockers.add("test-complete-stage-count:0")
    train_ready = not blockers
    report: dict[str, object] = {
        "schema": AUDIT_SCHEMA,
        "train_ready": train_ready,
        "algorithm": {
            "primary": "discrete-IQL",
            "baseline": "discrete-CQL",
            "gamma": 1.0,
            "legal_action_mask_required": True,
            "bc_anchor_required": True,
        },
        "inputs": {
            "labels_path": str(labels_path),
            "labels_sha256": _sha256_file(labels_path),
            "readiness_path": (
                str(readiness_path) if readiness_path.is_file() else None
            ),
            "readiness_sha256": (
                _sha256_file(readiness_path) if readiness_path.is_file() else None
            ),
            "legacy_readiness_blockers_ignored": legacy_readiness_blockers,
            "flow_scope": None if allowed_flows is None else sorted(allowed_flows),
            "capture_to_original_source_bindings": None if source_id_bindings is None else dict(source_id_bindings),
        },
        "counts": {
            "exact_nonfragment_transition_rows": exact_nonfragment_transition_rows,
            "main_action_exact_transition_rows": main_action_exact_transition_rows,
            "legacy_or_noncohort_exact_transition_rows": (
                exact_nonfragment_transition_rows - main_action_exact_transition_rows
            ),
            "unified_row_level_rows": unified_row_level_rows,
            "candidate_stage_groups": len(groups),
            "complete_stage_count": len(stages),
            "complete_stage_transition_count": stage_transition_count,
            "independent_source_count": source_count,
            "split_stage_counts": {
                name: int(split_stage_counts.get(name, 0))
                for name in ("train", "validation", "test")
            },
            "flow_stage_counts": dict(sorted(flow_stage_counts.items())),
            "group_blocker_counts": dict(sorted(group_blockers.items())),
        },
        "thresholds": {
            "min_complete_stage_transitions": min_transitions,
            "min_complete_stages": min_complete_stages,
            "min_independent_sources": min_independent_sources,
        },
        "blockers": sorted(blockers),
        "prohibitions": [
            "terminal score is not per-step reward",
            "fragment transitions are excluded",
            "simulator-predicted intermediate states are not exact",
            "actions outside authoritative legal candidates are impossible",
        ],
        "cohort_policy": (
            "all readiness thresholds are recomputed from the same canonical "
            "main-action exact-stage cohort; legacy mixed card-only blockers "
            "are retained as provenance but do not veto this cohort"
        ),
    }
    report["report_sha256"] = _digest(report)
    return report, tuple(stages)


def write_offline_rl_audit(
    output_path: Path = DEFAULT_AUDIT_OUTPUT,
    **kwargs: Any,
) -> dict[str, object]:
    report, _stages = audit_offline_rl_readiness(**kwargs)
    _atomic_write(
        Path(output_path),
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )
    return report


def build_offline_rl_dataset(
    *,
    output_dir: Path = DEFAULT_DATASET_OUTPUT,
    **kwargs: Any,
) -> dict[str, object]:
    report, stages = audit_offline_rl_readiness(**kwargs)
    if report.get("train_ready") is not True:
        raise OfflineRLNotReadyError(
            "offline RL blocked: " + ", ".join(report.get("blockers", []))
        )
    output_root = Path(output_dir)
    stages_path = output_root / "stages.jsonl"
    manifest_path = output_root / "manifest.json"
    encoded = b"".join(_canonical_bytes(value.to_dict()) + b"\n" for value in stages)
    _atomic_write(stages_path, encoded)
    manifest: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "train_ready": True,
        "stage_count": len(stages),
        "transition_count": sum(len(value.transitions) for value in stages),
        "stages_sha256": _sha256_file(stages_path),
        "audit": report,
    }
    manifest["manifest_content_sha256"] = _digest(manifest)
    _atomic_write(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        + b"\n",
    )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit/build complete GKMS offline-RL stages")
    parser.add_argument("command", choices=("audit", "build"), nargs="?", default="audit")
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--readiness", type=Path, default=DEFAULT_READINESS)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        try:
            result = build_offline_rl_dataset(
                labels_path=args.labels,
                readiness_path=args.readiness,
                output_dir=args.output or DEFAULT_DATASET_OUTPUT,
            )
        except OfflineRLNotReadyError as error:
            report = write_offline_rl_audit(
                args.output or DEFAULT_AUDIT_OUTPUT,
                labels_path=args.labels,
                readiness_path=args.readiness,
            )
            print(
                json.dumps(
                    {"built": False, "error": str(error), "audit": report},
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
            )
            return 2
    else:
        result = write_offline_rl_audit(
            args.output or DEFAULT_AUDIT_OUTPUT,
            labels_path=args.labels,
            readiness_path=args.readiness,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


__all__ = [
    "AUDIT_SCHEMA",
    "MANIFEST_SCHEMA",
    "OfflineRLNotReadyError",
    "RLStage",
    "RLTransition",
    "STAGE_SCHEMA",
    "audit_offline_rl_readiness",
    "build_offline_rl_dataset",
    "main",
    "write_offline_rl_audit",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
