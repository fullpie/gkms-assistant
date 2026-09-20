"""Versioned training contract for frozen GKMS behavior and transition data."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from .canonical_training_labels import (
    MANIFEST_SCHEMA as CANONICAL_MANIFEST_SCHEMA,
    FRAGMENT_TRANSITION_SCHEMA,
    INNER_TRANSITION_SCHEMA,
    LABEL_SCHEMA,
    LEADERBOARD_EPISODE_SCHEMA,
    LEGAL_DECISION_SCHEMA,
    RUNTIME_LEGAL_SCHEMA,
    PLAN_BY_NATIVE_VALUE,
    EFFECT_BY_NATIVE_VALUE,
    STAGE_BY_NATIVE_VALUE,
)
from .frozen_training_dataset import MANIFEST_SCHEMA as FROZEN_MANIFEST_SCHEMA
from .exact_policy_contract import strict_exact_candidate_index
from .offline_rl import (
    MANIFEST_SCHEMA as OFFLINE_RL_MANIFEST_SCHEMA,
    STAGE_SCHEMA as OFFLINE_RL_STAGE_SCHEMA,
)
from .training_artifact_io import (
    atomic_write as _atomic_write,
    canonical_json_bytes as _canonical_bytes,
    sha256_file as _sha256_file,
)
from .training_coverage_matrix import ARCHETYPES, MODES


SPEC_SCHEMA: Final = "gkms.training-spec.v1"
SPEC_V2_SCHEMA: Final = "gkms.training-spec.v2"
SPEC_SCOPED_SCHEMA: Final = "gkms.training-spec.scoped.v1"
TRAINING_SCOPE_SCHEMA: Final = "gkms.training-scope.v1"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]


def validate_shared_primary_training_evidence(spec, *, spec_path, stages_path, loader_audit):
    """Bind existing native/feature/split reports before the opt-in BC fit.

    This is a reference verifier, not a second qualification system. A feature
    version declaration alone cannot admit data. Current diagnostic feature
    reports explicitly have training_admission=false and are rejected here.
    """
    from .shared_bc_grouping import verify_split_assignment
    from .training_quarantine import load_current_training_quarantine
    shared = spec.get('shared_primary_feature_contract')
    if isinstance(shared, Mapping) and shared.get('diagnostic_only') is True:
        raise ValueError('Diagnostic-only shared feature contract cannot qualify fitting evidence')
    refs = spec.get('shared_primary_evidence')
    if not isinstance(shared, Mapping) or not isinstance(refs, Mapping):
        raise ValueError('Shared BC fit requires hash-bound native, feature and player-split evidence')

    def read_ref(name):
        ref = refs.get(name)
        if not isinstance(ref, Mapping) or not isinstance(ref.get('path'), str) or not ref.get('sha256'):
            raise ValueError('Shared BC evidence reference missing: ' + name)
        path = Path(ref['path'])
        if not path.is_absolute():
            path = Path(spec_path).parent / path
        if _sha256_file(path) != ref['sha256']:
            raise ValueError('Shared BC evidence hash differs: ' + name)
        return json.loads(path.read_text(encoding='utf-8-sig'))

    native, feature, split = (read_ref(name) for name in ('native_dataset', 'feature_report', 'player_split'))
    stages_sha = _sha256_file(Path(stages_path))
    exact_lock = _mapping(_mapping(spec.get('dataset_lock')).get('exact_exam'))
    if (refs['native_dataset']['sha256'] != exact_lock.get('manifest_sha256')
            or native.get('schema') != OFFLINE_RL_MANIFEST_SCHEMA or native.get('train_ready') is not True
            or native.get('stages_sha256') != stages_sha
            or native.get('transition_count') != loader_audit['legal_binding_count']):
        raise ValueError('Native qualification does not cover this frozen BC dataset')
    if (feature.get('schema') != 'gkms.shared-bc-six-flow-feature-parity.v1'
            or feature.get('training_admission') is not True
            or feature.get('contract') != shared
            or _mapping(feature.get('dataset')).get('stages_sha256') != stages_sha
            or feature.get('flow_stage_row_counts') != loader_audit['shared_flow_stage_row_counts']
            or feature.get('source_master_compatibility_verified') is not True):
        raise ValueError('Shared feature/mode/stage/source-Master qualification is incomplete or differently bound')
    gaps = loader_audit['shared_feature_gap_counts']
    if gaps and (feature.get('feature_gaps_reviewed') is not True or feature.get('accepted_gap_counts') != gaps):
        raise ValueError('Shared semantic feature gaps have no matching reviewed evidence')
    if split.get('schema') != 'gkms.shared-bc-source-backed-split-preparation.v1':
        raise ValueError('Shared BC requires the existing source-backed player split preparation')
    verify_split_assignment(split['stages'], split['grouping'], split['assignment'])
    quarantine = load_current_training_quarantine()
    if quarantine is None:
        raise ValueError('Current indexed quarantine is unavailable for shared BC fitting')
    assigned = {row['stage_id']: row for row in split['assignment']['stages']}
    sources = {row['stage_id']: row for row in split['stages']}
    seen = set()
    with Path(stages_path).open(encoding='utf-8-sig') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            source_id = row.get('source_stage_id')
            if source_id not in assigned or source_id in seen:
                raise ValueError('BC stage lacks a unique original stage/player split witness')
            seen.add(source_id)
            assignment, source = assigned[source_id], sources[source_id]
            matched = quarantine.match({'identity':{'episode_id':source.get('original_episode_id')}},
                row, {'file_sha256':source.get('replay_source_sha256')})
            if matched:
                raise ValueError('Current indexed quarantine excludes shared BC source: ' + ','.join(matched))
            if (assignment['new_split'] not in ('train', 'validation', 'test')
                    or row['split'] != assignment['new_split']
                    or row['trajectory_id'] != source['source_trajectory_id']
                    or row.get('whole_trajectory_id') != assignment['whole_trajectory_id']
                    or not assignment.get('player_sha256') or source.get('quarantine_excluded') is True):
                raise ValueError('BC stage contradicts source/player/cultivation/quarantine restrictions')
    return {'native_dataset_sha256': refs['native_dataset']['sha256'],
        'feature_report_sha256': refs['feature_report']['sha256'],
        'player_split_sha256': refs['player_split']['sha256'], 'stages_sha256': stages_sha,
        'shared_contract_sha256': shared['contract_sha256'], 'source_stage_count': len(seen),
        'current_quarantine': quarantine.reference(),
        'fit_executed_by_this_verifier': False}


DEFAULT_FROZEN_ROOT: Final = (
    PROJECT_ROOT / "var" / "training_dataset" / "frozen_v1"
)
DEFAULT_COVERAGE: Final = (
    PROJECT_ROOT / "var" / "coverage" / "training_coverage_matrix_v1.json"
)
DEFAULT_INNER_READINESS: Final = (
    PROJECT_ROOT / "var" / "nia_training" / "full_rl_readiness.json"
)
DEFAULT_OUTPUT: Final = (
    PROJECT_ROOT / "var" / "training_specs" / "frozen_v1" / "training_spec.json"
)
DEFAULT_EXACT_RL_ROOT: Final = (
    PROJECT_ROOT
    / "var"
    / "offline_rl"
    / "exact_main_action_baseline_20260829_v2_frozen"
)
DEFAULT_V2_OUTPUT: Final = (
    PROJECT_ROOT / "var" / "training_specs" / "exact_v2" / "training_spec.json"
)
DEFAULT_SECONDARY_ACTION_EVIDENCE: Final = (
    PROJECT_ROOT
    / "var"
    / "coverage"
    / "runtime_replay_comparator_unified_addplaylog_fktn3011_rank2_mid2_29_sealed.json"
)

_DECLARED_ARCHETYPES: Final = frozenset(value[0] for value in ARCHETYPES)
_PRODUCE_IDS: Final = frozenset(value[0] for value in MODES)
_SPLITS: Final = ("train", "validation", "test")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _is_declared_flow(row: Mapping[str, Any]) -> bool:
    scope = _mapping(row.get("scope"))
    return (
        scope.get("produce_id") in _PRODUCE_IDS
        and scope.get("archetype") in _DECLARED_ARCHETYPES
    )


def _cohort_inventory(labels_path: Path) -> dict[str, object]:
    counters: Counter[str] = Counter()
    split_counts: dict[str, Counter[str]] = {
        name: Counter() for name in (
            "replay_all",
            "replay_primary",
            "replay_low_trust",
            "outer_bc",
            "unified_state_bc",
            "row_level_rl",
        )
    }
    flow_counts: dict[str, Counter[str]] = {
        name: Counter() for name in split_counts
    }
    for line_number, line in enumerate(
        labels_path.open("r", encoding="utf-8-sig"), start=1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, Mapping) or row.get("schema") != LABEL_SCHEMA:
            raise ValueError(f"invalid canonical label at line {line_number}")
        if _mapping(row.get("dedupe")).get("status") != "canonical":
            continue
        if not _is_declared_flow(row):
            continue
        scope = _mapping(row.get("scope"))
        flow = "|".join(
            str(scope.get(key))
            for key in ("produce_id", "plan_type", "exam_effect_type")
        )
        split = str(_mapping(row.get("split")).get("assignment", "unknown"))
        schema = row.get("source_schema")
        legal = _mapping(row.get("legal"))
        exact = _mapping(row.get("exact"))
        transition = _mapping(row.get("transition"))
        source = _mapping(row.get("source"))
        compatibility = row.get("compatibility")
        compatibility_values = (
            {str(value) for value in compatibility}
            if isinstance(compatibility, list)
            else set()
        )

        if (
            schema == LEADERBOARD_EPISODE_SCHEMA
            and "leaderboard-frozen-union-v1" in compatibility_values
        ):
            cohort = "replay_primary" if source.get("provenance") else "replay_low_trust"
            action_payload = _mapping(transition.get("action"))
            action_rows = action_payload.get("actions")
            action_count = len(action_rows) if isinstance(action_rows, list) else 0
            counters["replay_all"] += 1
            counters[cohort] += 1
            counters["replay_all_actions"] += action_count
            counters[f"{cohort}_actions"] += action_count
            split_counts["replay_all"][split] += 1
            split_counts[cohort][split] += 1
            flow_counts["replay_all"][flow] += 1
            flow_counts[cohort][flow] += 1

        if (
            row.get("granularity") == "decision"
            and legal.get("candidate_scope") == "outer"
            and _mapping(row.get("quality")).get("training_eligible") is True
        ):
            counters["outer_bc"] += 1
            split_counts["outer_bc"][split] += 1
            flow_counts["outer_bc"][flow] += 1

        exact_complete_unified = (
            legal.get("candidate_scope") == "unified"
            and legal.get("complete") is True
            and legal.get("exact") is True
            and legal.get("chosen_member") is True
        )
        if exact_complete_unified:
            counters["unified_legal_all"] += 1
            if schema == FRAGMENT_TRANSITION_SCHEMA:
                counters["unified_fragment"] += 1
            if schema == RUNTIME_LEGAL_SCHEMA:
                counters["unified_legal_no_state"] += 1
            if (
                schema in {INNER_TRANSITION_SCHEMA, LEGAL_DECISION_SCHEMA}
                and isinstance(transition.get("state_before"), Mapping)
            ):
                counters["unified_state_bc"] += 1
                split_counts["unified_state_bc"][split] += 1
                flow_counts["unified_state_bc"][flow] += 1

        if schema == INNER_TRANSITION_SCHEMA and exact.get("transition") is True:
            counters["exact_nonfragment_transition"] += 1
        if (
            schema == INNER_TRANSITION_SCHEMA
            and row.get("full_rl_policy_ready") is True
        ):
            counters["row_level_rl"] += 1
            split_counts["row_level_rl"][split] += 1
            flow_counts["row_level_rl"][flow] += 1

    return {
        "counts": dict(sorted(counters.items())),
        "split_counts": {
            cohort: {split: int(values.get(split, 0)) for split in _SPLITS}
            for cohort, values in sorted(split_counts.items())
        },
        "flow_counts": {
            cohort: dict(sorted(values.items()))
            for cohort, values in sorted(flow_counts.items())
        },
    }


def _plan_from_flow(flow: str) -> str:
    parts = flow.split("|")
    if len(parts) != 3 or parts[1] not in {
        "ProducePlanType_Plan1",
        "ProducePlanType_Plan2",
        "ProducePlanType_Plan3",
    }:
        raise ValueError(f"invalid exact-RL flow: {flow!r}")
    return parts[1]


def declared_training_scope(flows: Sequence[str]) -> dict[str, object]:
    """Name exact mode/plan/effect cohorts without weakening their data gates."""
    if isinstance(flows, (str, bytes)) or not flows:
        raise ValueError("training scope requires an explicit nonempty flow list")
    if any(not isinstance(flow, str) or not flow for flow in flows):
        raise ValueError("training scope flows must be nonempty strings")
    if len(set(flows)) != len(flows):
        raise ValueError("training scope contains duplicate flows")
    valid = {
        f"{produce}|{plan}|{effect}"
        for produce in _PRODUCE_IDS
        for _, plan, effect in ARCHETYPES
    }
    unknown = set(flows) - valid
    if unknown:
        raise ValueError(f"unknown training scope flows: {sorted(unknown)}")
    return {
        "schema": TRAINING_SCOPE_SCHEMA,
        "flows": sorted(flows),
        "plan_types": sorted({_plan_from_flow(flow) for flow in flows}),
        "split_unit": "trajectory-and-capture-source",
        "outside_scope_training_allowed": False,
        "cross_split_trajectory_allowed": False,
        "cross_split_capture_source_allowed": False,
    }


def _exact_rl_inventory(
    exact_rl_root: Path, *, flow_scope: frozenset[str] | None = None,
) -> tuple[dict[str, object], Mapping[str, Any]]:
    """Validate and summarize the small frozen exact cohort one stage at a time."""

    exact_rl_root = Path(exact_rl_root)
    manifest_path = exact_rl_root / "manifest.json"
    stages_path = exact_rl_root / "stages.jsonl"
    canonical_manifest_path = exact_rl_root / "canonical_labels_manifest.json"
    split_manifest_path = exact_rl_root / "trajectory_split_manifest.json"
    for path in (
        manifest_path,
        stages_path,
        canonical_manifest_path,
        split_manifest_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    manifest = _read_json(manifest_path)
    if (
        manifest.get("schema") != OFFLINE_RL_MANIFEST_SCHEMA
        or manifest.get("train_ready") is not True
    ):
        raise ValueError("training spec v2 requires a train-ready exact-RL dataset")
    if manifest.get("stages_sha256") != _sha256_file(stages_path):
        raise ValueError("exact-RL stages SHA-256 mismatch")
    canonical_manifest = _read_json(canonical_manifest_path)
    if canonical_manifest.get("schema") != CANONICAL_MANIFEST_SCHEMA:
        raise ValueError("exact-RL canonical label manifest schema mismatch")
    split_manifest = _read_json(split_manifest_path)
    assignments = split_manifest.get("trajectory_splits")
    if split_manifest.get("policy_version") != 1 or not isinstance(
        assignments, Mapping
    ):
        raise ValueError("exact-RL trajectory split manifest is invalid")
    if any(
        not isinstance(key, str)
        or not key
        or value not in _SPLITS
        for key, value in assignments.items()
    ):
        raise ValueError("exact-RL trajectory split assignment is invalid")
    canonical_split = _mapping(canonical_manifest.get("trajectory_split_policy"))
    if (
        canonical_split.get("policy_version") != 1
        or canonical_split.get("mapping_count") != len(assignments)
        or canonical_split.get("source_sha256") != _sha256_file(split_manifest_path)
    ):
        raise ValueError("exact-RL canonical/split manifest binding mismatch")
    audit = _mapping(manifest.get("audit"))
    audit_inputs = _mapping(audit.get("inputs"))
    canonical_artifacts = _mapping(canonical_manifest.get("artifacts"))
    if canonical_artifacts.get("labels_sha256") != audit_inputs.get("labels_sha256"):
        raise ValueError("exact-RL canonical labels SHA-256 binding mismatch")
    if flow_scope is not None:
        labels_sha = canonical_artifacts.get("labels_sha256")
        if (not isinstance(labels_sha, str) or len(labels_sha) != 64
                or any(character not in "0123456789abcdef" for character in labels_sha)):
            raise ValueError("scoped exact canonical labels SHA-256 binding is missing or invalid")
        if audit.get("blockers") != []:
            raise ValueError("scoped exact dataset audit still has blockers or no blocker verdict")

    stage_count = 0
    transition_count = 0
    terminal_count = 0
    source_ids: set[str] = set()
    state_field_counts: Counter[int] = Counter()
    action_kinds: Counter[str] = Counter()
    trajectory_splits: dict[str, str] = {}
    source_splits: dict[str, str] = {}
    plan_trajectories: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {name: set() for name in _SPLITS}
    )
    plan_stage_counts: dict[str, Counter[str]] = defaultdict(Counter)
    plan_transition_counts: dict[str, Counter[str]] = defaultdict(Counter)
    flow_trajectories: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {name: set() for name in _SPLITS}
    )
    flow_stage_counts: Counter[tuple[str, str, str]] = Counter()
    flow_transition_counts: Counter[tuple[str, str]] = Counter()

    with stages_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, Mapping) or row.get("schema") != OFFLINE_RL_STAGE_SCHEMA:
                raise ValueError(f"invalid exact-RL stage at line {line_number}")
            trajectory_id = row.get("trajectory_id")
            split = row.get("split")
            flow = row.get("flow")
            stage_name = row.get("stage")
            source_id = row.get("source_id")
            transitions = row.get("transitions")
            if not (
                isinstance(trajectory_id, str)
                and trajectory_id
                and split in _SPLITS
                and isinstance(flow, str)
                and flow
                and stage_name in {"Mid1", "Mid2", "Final"}
                and isinstance(source_id, str)
                and source_id
                and isinstance(transitions, list)
                and transitions
            ):
                raise ValueError(f"incomplete exact-RL stage at line {line_number}")
            previous = trajectory_splits.setdefault(trajectory_id, str(split))
            if previous != split:
                raise ValueError("one exact trajectory crosses dataset splits")
            if flow_scope is not None:
                if flow not in flow_scope:
                    raise ValueError(f"exact dataset flow outside declared training scope: {flow}")
                source_split = source_splits.setdefault(source_id, str(split))
                if source_split != split:
                    raise ValueError("one exact capture source crosses dataset splits")
            plan = _plan_from_flow(flow)
            plan_trajectories[plan][str(split)].add(trajectory_id)
            plan_stage_counts[plan][str(split)] += 1
            plan_transition_counts[plan][str(split)] += len(transitions)
            flow_trajectories[flow][str(split)].add(trajectory_id)
            flow_stage_counts[(flow, str(stage_name), str(split))] += 1
            flow_transition_counts[(flow, str(split))] += len(transitions)
            source_ids.add(source_id)
            stage_count += 1
            transition_count += len(transitions)
            if row.get("transition_count") != len(transitions):
                raise ValueError("exact-RL stage transition count mismatch")
            terminal_positions: list[int] = []
            for position, transition in enumerate(transitions):
                if not isinstance(transition, Mapping):
                    raise ValueError("exact-RL transition must be an object")
                before = transition.get("state_before")
                after = transition.get("state_after")
                action = transition.get("action")
                candidates = transition.get("legal_candidates")
                reward = transition.get("reward")
                terminal = transition.get("terminal")
                if not (
                    isinstance(before, Mapping)
                    and isinstance(after, Mapping)
                    and action is not None
                    and isinstance(candidates, list)
                    and candidates
                    and isinstance(reward, (int, float))
                    and not isinstance(reward, bool)
                    and type(terminal) is bool
                ):
                    raise ValueError("incomplete exact-RL transition")
                if flow_scope is not None:
                    for state in (before, after):
                        raw_plan, raw_effect, raw_stage = (
                            state.get("planType"), state.get("mainEffectType"), state.get("stepType"),
                        )
                        native_plan = PLAN_BY_NATIVE_VALUE.get(raw_plan) if type(raw_plan) is int else raw_plan
                        native_effect = EFFECT_BY_NATIVE_VALUE.get(raw_effect) if type(raw_effect) is int else raw_effect
                        native_stage = STAGE_BY_NATIVE_VALUE.get(raw_stage) if type(raw_stage) is int else raw_stage
                        native_flow = f"{state.get('produceId')}|{native_plan}|{native_effect}"
                        if native_flow != flow or native_stage != stage_name:
                            raise ValueError("scoped exact row disagrees with native flow/stage identity")
                before_score = before.get("score")
                after_score = after.get("score")
                if not (
                    isinstance(before_score, (int, float))
                    and not isinstance(before_score, bool)
                    and isinstance(after_score, (int, float))
                    and not isinstance(after_score, bool)
                    and reward == after_score - before_score
                ):
                    raise ValueError("exact-RL reward is not native score delta")
                if not isinstance(action, Mapping):
                    raise ValueError("exact-RL action must be an object")
                strict_exact_candidate_index(before, action, candidates)
                kind = _mapping(action).get("kind", _mapping(action).get("action_type"))
                action_kinds[str(kind)] += 1
                state_field_counts[len(before)] += 1
                state_field_counts[len(after)] += 1
                if terminal:
                    terminal_positions.append(position)
                    terminal_count += 1
            if terminal_positions != [len(transitions) - 1]:
                raise ValueError("exact-RL terminal must occur once at stage end")

    if manifest.get("stage_count") != stage_count:
        raise ValueError("exact-RL manifest stage count mismatch")
    if manifest.get("transition_count") != transition_count:
        raise ValueError("exact-RL manifest transition count mismatch")
    audit_counts = _mapping(audit.get("counts"))
    if (
        audit.get("train_ready") is not True
        or audit_counts.get("complete_stage_count") != stage_count
        or audit_counts.get("main_action_exact_transition_rows") != transition_count
    ):
        raise ValueError("exact-RL audit cohort count mismatch")
    if dict(trajectory_splits) != {
        str(key): str(value) for key, value in assignments.items()
    }:
        raise ValueError("exact-RL stages do not exactly match the split manifest")
    required_plans = {
        "ProducePlanType_Plan1",
        "ProducePlanType_Plan2",
        "ProducePlanType_Plan3",
    }
    if flow_scope is not None:
        required_plans = {_plan_from_flow(flow) for flow in flow_scope}
        if set(flow_trajectories) != flow_scope:
            raise ValueError("exact dataset does not cover every declared flow")
        for flow in sorted(flow_scope):
            missing = [split for split in _SPLITS if not flow_trajectories[flow][split]]
            if missing:
                raise ValueError(
                    f"scoped exact flow lacks independent trajectory splits: {flow}; "
                    f"missing={','.join(missing)}; minimum_additional_trajectories={len(missing)}"
                )
    elif set(plan_trajectories) != required_plans:
        raise ValueError("exact-RL dataset does not cover all three Plans")
    for plan in required_plans:
        if any(not plan_trajectories[plan][split] for split in _SPLITS):
            raise ValueError(f"exact-RL {plan} lacks a trajectory split")

    per_plan = {
        plan: {
            "trajectory_counts": {
                split: len(plan_trajectories[plan][split]) for split in _SPLITS
            },
            "stage_counts": {
                split: int(plan_stage_counts[plan].get(split, 0)) for split in _SPLITS
            },
            "transition_counts": {
                split: int(plan_transition_counts[plan].get(split, 0))
                for split in _SPLITS
            },
        }
        for plan in sorted(required_plans)
    }
    missing_flow_stage_split_cells = [
        {"flow": flow, "stage": stage, "split": split}
        for flow in sorted(flow_trajectories)
        for split in _SPLITS
        for stage in ("Mid1", "Mid2", "Final")
        if flow_stage_counts[(flow, stage, split)] == 0
    ]
    per_flow = {
        flow: {
            "trajectory_counts": {
                split: len(flow_trajectories[flow][split]) for split in _SPLITS
            },
            "stage_counts": {
                split: sum(
                    flow_stage_counts[(flow, stage, split)]
                    for stage in ("Mid1", "Mid2", "Final")
                )
                for split in _SPLITS
            },
            "transition_counts": {
                split: int(flow_transition_counts[(flow, split)])
                for split in _SPLITS
            },
            "stage_split_counts": {
                stage: {
                    split: int(flow_stage_counts[(flow, stage, split)])
                    for split in _SPLITS
                }
                for stage in ("Mid1", "Mid2", "Final")
            },
        }
        for flow in sorted(flow_trajectories)
    }
    inventory = {
        "stage_count": stage_count,
        "transition_count": transition_count,
        "terminal_count": terminal_count,
        "independent_source_count": len(source_ids),
        "trajectory_count": len(trajectory_splits),
        "flow_count": len(flow_trajectories),
        "split_transition_counts": {
            split: sum(
                int(plan_transition_counts[plan].get(split, 0))
                for plan in required_plans
            )
            for split in _SPLITS
        },
        "action_kind_counts": dict(sorted(action_kinds.items())),
        "state_top_level_field_counts": {
            str(key): int(value) for key, value in sorted(state_field_counts.items())
        },
        "per_plan": per_plan,
        "per_flow": per_flow,
        "missing_flow_stage_split_cells": missing_flow_stage_split_cells,
        "all_flow_trajectory_splits_complete": all(
            flow_trajectories[flow][split]
            for flow in flow_trajectories
            for split in _SPLITS
        ),
        "all_flow_stage_splits_complete": not missing_flow_stage_split_cells,
    }
    return inventory, manifest


def build_scoped_training_spec(
    *, exact_rl_root: Path, flows: Sequence[str], enable_offline_rl: bool = False,
) -> dict[str, object]:
    """A single exact Exam cohort need not contain unrelated Plans or outer data.

    The source manifest must already be train-ready. Every declared flow still
    needs independent train/validation/test trajectories, with no capture source
    crossing splits. This function never assigns or repairs a dataset split.
    """
    scope = declared_training_scope(flows)
    if type(enable_offline_rl) is not bool:
        raise TypeError("enable_offline_rl must be bool")
    root = Path(exact_rl_root)
    inventory, manifest = _exact_rl_inventory(root, flow_scope=frozenset(flows))
    if enable_offline_rl and inventory["all_flow_stage_splits_complete"] is not True:
        raise ValueError("scoped offline RL requires every flow/stage/split cell")
    names = {
        "manifest": "manifest.json",
        "stages": "stages.jsonl",
        "canonical_labels_manifest": "canonical_labels_manifest.json",
        "trajectory_split_manifest": "trajectory_split_manifest.json",
    }
    lock: dict[str, object] = {}
    for key, name in names.items():
        path = root / name
        lock[f"{key}_path"] = str(path)
        lock[f"{key}_sha256"] = _sha256_file(path)
    lock.update({
        "split_owner": "unchanged frozen trajectory manifest",
        "state_authority": "frozen native runtime S,A,S'",
        "player_identity_available": False,
        "player_leakage_risk": True,
    })
    rows = inventory["transition_count"]
    bc = {
        "enabled": True, "dataset_ready": True, "learner_enabled": True,
        "model_ready": False, "shadow_ready": False, "promotion_ready": False,
        "policy_domain": "exam_policy", "rows": rows,
        "split_counts": inventory["split_transition_counts"],
        "input": "native state_before + ordered authoritative legal candidates",
        "target": "unique chosen member of native main-action candidate set",
        "loss": "candidate-set masked cross entropy with trajectory/flow balance",
        "limitations": [
            "scope is explicit; no coverage outside the declared flows is claimed",
            "missing stage-by-split cells remain unobserved; no terminal score broadcast",
            "capture and trajectory separation does not establish independent player identity",
        ],
    }
    spec: dict[str, object] = {
        "schema": SPEC_SCOPED_SCHEMA, "version": 1, "random_seed": 20260908,
        "training_scope": scope,
        "dataset_lock": {"exact_exam": lock},
        "inventory": {"exact_exam": inventory},
        "objectives": {
            "exact_main_action_bc": bc,
            "offline_rl": {
                "dataset_ready": manifest.get("train_ready") is True,
                "learner_enabled": enable_offline_rl, "model_ready": False,
                "shadow_ready": False, "promotion_ready": False,
                "bc_anchor": "exact_main_action_bc only",
                "row_level_transitions": rows,
                "complete_stage_count": inventory["stage_count"],
                "split_counts": inventory["split_transition_counts"],
                "reward": "state_after.score-state_before.score",
                "terminal": "authoritative native terminal once at stage end",
                "blockers": [] if enable_offline_rl else ["separate learner and held-out evaluation required"],
            },
        },
        "policy_domains": {"exam_policy": {
            "flows": scope["flows"],
            "action": {"surface": "main-action-v1", "guid_policy": "binding-only"},
            "split": {"unit": "trajectory-and-capture-source", "cross_split_trajectory_allowed": False},
        }},
        "runtime_promotion": {"default_enabled": False, "scope_only": True},
    }
    spec["spec_sha256"] = hashlib.sha256(_canonical_bytes(spec)).hexdigest()
    return spec


def build_training_spec(
    *,
    frozen_root: Path = DEFAULT_FROZEN_ROOT,
    coverage_path: Path = DEFAULT_COVERAGE,
    inner_readiness_path: Path = DEFAULT_INNER_READINESS,
) -> dict[str, object]:
    frozen_root = Path(frozen_root)
    manifest_path = frozen_root / "manifest.json"
    labels_path = frozen_root / "canonical" / "labels.jsonl"
    canonical_manifest_path = frozen_root / "canonical" / "manifest.json"
    for path in (
        manifest_path,
        labels_path,
        canonical_manifest_path,
        coverage_path,
        inner_readiness_path,
    ):
        if not Path(path).is_file():
            raise FileNotFoundError(path)
    frozen = _read_json(manifest_path)
    if frozen.get("schema") != FROZEN_MANIFEST_SCHEMA or frozen.get(
        "freeze_ready"
    ) is not True:
        raise ValueError("training spec requires a verified-ready frozen dataset")
    canonical_manifest = _read_json(canonical_manifest_path)
    coverage = _read_json(Path(coverage_path))
    readiness = _read_json(Path(inner_readiness_path))
    inventory = _cohort_inventory(labels_path)
    counts = _mapping(inventory.get("counts"))
    splits = _mapping(inventory.get("split_counts"))

    offline_blocker_values = {
        str(value) for value in readiness.get("blockers", [])
    }
    row_rl_splits = _mapping(splits.get("row_level_rl"))
    for split_name in ("validation", "test"):
        if int(row_rl_splits.get(split_name, 0)) == 0:
            offline_blocker_values.add(f"row-level-rl-{split_name}-count:0")
    if readiness.get("complete_stage_count") == 0:
        offline_blocker_values.add("complete-stage-count:0")
    offline_blockers = sorted(offline_blocker_values)
    spec: dict[str, object] = {
        "schema": SPEC_SCHEMA,
        "version": 1,
        "random_seed": 20260828,
        "dataset_lock": {
            "frozen_manifest_path": str(manifest_path),
            "frozen_manifest_sha256": _sha256_file(manifest_path),
            "frozen_checksums_sha256": _sha256_file(
                frozen_root / "checksums.sha256"
            ),
            "frozen_episode_sha256": _mapping(
                _mapping(frozen.get("artifacts")).get("episodes")
            ).get("sha256"),
            "canonical_labels_path": str(labels_path),
            "canonical_labels_sha256": _mapping(
                _mapping(_mapping(frozen.get("artifacts")).get("canonical")).get(
                    "artifact_hashes"
                )
            ).get("labels.jsonl"),
            "coverage_sha256": _sha256_file(Path(coverage_path)),
            "inner_readiness_sha256": _sha256_file(Path(inner_readiness_path)),
            "split_owner": "frozen trajectory-grouped split",
            "player_identity_available": False,
            "player_leakage_risk": True,
        },
        "inventory": inventory,
        "shared_features": {
            "categorical_hash": {
                "algorithm": "sha256-feature-hashing-v1",
                "context_dimensions": 2048,
                "candidate_dimensions": 512,
                "signed": True,
            },
            "numeric": {
                "missing_mask": True,
                "normalization": "train-split robust median/IQR, clipped [-8,8]",
                "fields": [
                    "week",
                    "phase",
                    "current_turn",
                    "remain_turn",
                    "stamina",
                    "max_stamina",
                    "vocal",
                    "dance",
                    "visual",
                    "produce_point",
                    "score",
                    "block",
                    "candidate_count",
                ],
            },
        },
        "objectives": {
            "outer_bc": {
                "enabled": counts.get("outer_bc", 0) > 0,
                "rows": counts.get("outer_bc", 0),
                "split_counts": splits.get("outer_bc"),
                "filter": "canonical declared-flow outer decision with exact complete candidate set",
                "model": "shared candidate scorer MLP [128,64] ReLU",
                "input": "mode/flow/stage/week/phase/state_before/presence-mask/history + candidate identity/is_sp",
                "target": "masked pointer to the unique chosen member of the row's exact candidates",
                "loss": "candidate-set masked cross entropy",
                "weighting": "equal trajectory mass then inverse-sqrt 10-cell balance; terminal score is train-only percentile weight, never input or immediate reward",
                "metrics": [
                    "macro top-1 by flow/stage",
                    "macro top-3 by flow/stage",
                    "MRR",
                    "NLL",
                    "ECE",
                    "legal candidate membership",
                ],
            },
            "replay_sequence_bc": {
                "enabled": counts.get("replay_primary", 0) > 0,
                "primary_episodes": counts.get("replay_primary", 0),
                "primary_actions": counts.get("replay_primary_actions", 0),
                "primary_split_counts": splits.get("replay_primary"),
                "low_trust_episodes": counts.get("replay_low_trust", 0),
                "low_trust_default_enabled": False,
                "filter": "frozen declared-flow official replay with original semantic provenance",
                "model": "action-prefix MLP [256,128] with factorized action-type/index heads",
                "input": "static episode/loadout/deck context + stage + ordered action prefix; excludes terminal score/rank/future length",
                "target": "hierarchical next action family, selector arity, and original ordered index tuple",
                "loss": "action-type CE + valid-index CE",
                "weighting": "within-flow/stage terminal-score percentile may weight [0.5,1.5] only",
                "metrics": [
                    "action-type top-1",
                    "index top-1 conditioned on action type",
                    "ordered tuple exact accuracy",
                    "prefix NLL",
                    "normalized sequence edit distance",
                    "per-flow/stage macro accuracy",
                ],
                "limitations": [
                    "action index is not card identity",
                    "no exact runtime state or legal set",
                    "never directly promotes a live card decision",
                ],
            },
            "unified_state_bc": {
                "enabled": counts.get("unified_state_bc", 0) > 0,
                "mode": "diagnostic-only",
                "rows": counts.get("unified_state_bc", 0),
                "split_counts": splits.get("unified_state_bc"),
                "model": "shared legal-candidate scorer MLP [128,64] ReLU",
                "target": "masked pointer to unique chosen member; GUID/slot bind identity but are not semantic features",
                "promotion_enabled": False,
                "allowed_uses": [
                    "schema/DataLoader validation",
                    "all-row overfit smoke",
                    "candidate permutation invariance test",
                ],
                "blockers": [
                    "only two flows have rows",
                    "runtime legal set verified for only one flow",
                    "sample count is insufficient for live promotion",
                ],
            },
            "offline_rl": {
                "train_enabled": False,
                "algorithm_when_ready": "discrete IQL with legal-action mask and BC anchor; CQL conservative baseline",
                "row_level_transitions": counts.get("row_level_rl", 0),
                "split_counts": splits.get("row_level_rl"),
                "complete_stage_count": readiness.get("complete_stage_count"),
                "reward": "state_after.score-state_before.score, gamma=1.0 finite stage; never terminal score broadcast",
                "terminal": "authoritative transition terminal only",
                "blockers": offline_blockers,
            },
        },
        "optimizer": {
            "name": "Adam",
            "learning_rate": 0.001,
            "batch_size": 256,
            "max_epochs": 50,
            "early_stopping": {"metric": "validation macro NLL", "patience": 5},
            "gradient_clip_norm": 5.0,
        },
        "evaluation": {
            "grouping": "trajectory split only; never random action-row split",
            "report_slices": ["mode", "flow", "stage", "action_type", "provenance"],
            "bc_acceptance": {
                "macro_top1_improvement_over_frequency_baseline_pp": 2.0,
                "outer_minimum_macro_top1": 0.60,
                "maximum_per_flow_top1_drop_pp": 5.0,
                "chosen_action_legal_rate": 1.0,
            },
        },
        "runtime_promotion": {
            "default_enabled": False,
            "shadow_only": True,
            "requirements": [
                "same-native-state legal candidates",
                "10/10 flow legal-set verification",
                "objective and terminal-value preservation",
                "held-out per-flow improvement",
                "no candidate-set or action-identity change",
            ],
        },
        "prohibitions": [
            "do not infer card identity from replay slot index",
            "do not label simulator-predicted intermediate state as exact",
            "do not use terminal score as every-step reward",
            "do not use fragment transitions for offline RL",
            "do not treat runtime legal probes without state as policy samples",
            "do not infer one flow's training or promotion readiness from another flow",
        ],
        "source_status": {
            "coverage_summary": coverage.get("summary"),
            "inner_readiness": {
                "training_ready": readiness.get("training_ready"),
                "full_rl_policy_ready": readiness.get("full_rl_policy_ready"),
                "complete_stage_count": readiness.get("complete_stage_count"),
            },
            "canonical_manifest_sha256": _sha256_file(canonical_manifest_path),
        },
    }
    spec["spec_sha256"] = hashlib.sha256(_canonical_bytes(spec)).hexdigest()
    return spec


def build_training_spec_v2(
    *,
    frozen_root: Path = DEFAULT_FROZEN_ROOT,
    exact_rl_root: Path = DEFAULT_EXACT_RL_ROOT,
    coverage_path: Path = DEFAULT_COVERAGE,
    inner_readiness_path: Path = DEFAULT_INNER_READINESS,
    secondary_action_evidence_path: Path | None = DEFAULT_SECONDARY_ACTION_EVIDENCE,
) -> dict[str, object]:
    """Build the two-domain BC/offline-RL contract without copying frozen data."""

    base = build_training_spec(
        frozen_root=frozen_root,
        coverage_path=coverage_path,
        inner_readiness_path=inner_readiness_path,
    )
    base.pop("spec_sha256", None)
    exact_rl_root = Path(exact_rl_root)
    exact_inventory, exact_manifest = _exact_rl_inventory(exact_rl_root)
    exact_manifest_path = exact_rl_root / "manifest.json"
    exact_stages_path = exact_rl_root / "stages.jsonl"
    exact_canonical_manifest_path = exact_rl_root / "canonical_labels_manifest.json"
    exact_split_manifest_path = exact_rl_root / "trajectory_split_manifest.json"

    secondary_evidence: dict[str, object] | None = None
    if secondary_action_evidence_path is not None:
        evidence_path = Path(secondary_action_evidence_path)
        if not evidence_path.is_file():
            raise FileNotFoundError(evidence_path)
        evidence = _read_json(evidence_path)
        action_comparison = _mapping(evidence.get("action_comparison"))
        if evidence.get("passed") is not True:
            raise ValueError("secondary-action evidence is not sealed/passing")
        secondary_evidence = {
            "path": str(evidence_path),
            "sha256": _sha256_file(evidence_path),
            "runtime_main_action_count": action_comparison.get(
                "runtime_main_action_count"
            ),
            "runtime_secondary_action_count": action_comparison.get(
                "runtime_secondary_action_count"
            ),
            "expected_action_count": action_comparison.get("expected_action_count"),
            "passed": True,
        }

    behavior_inventory = _mapping(base.get("inventory"))
    behavior_counts = _mapping(behavior_inventory.get("counts"))
    behavior_lock = dict(_mapping(base.get("dataset_lock")))
    base_objectives = _mapping(base.get("objectives"))
    outer_bc = dict(_mapping(base_objectives.get("outer_bc")))
    outer_bc.update(
        {
            "dataset_ready": True,
            "learner_enabled": True,
            "model_ready": False,
            "shadow_ready": False,
            "promotion_ready": False,
            "policy_domain": "outer_policy",
        }
    )
    replay_bc = dict(_mapping(base_objectives.get("replay_sequence_bc")))
    replay_bc.update(
        {
            "dataset_ready": True,
            "learner_enabled": True,
            "model_ready": False,
            "shadow_ready": False,
            "promotion_ready": False,
            "policy_domain": "exam_syntax_prior",
        }
    )
    exact_transition_count = int(exact_inventory["transition_count"])
    exact_exam_bc = {
        "enabled": exact_transition_count > 0,
        "dataset_ready": exact_manifest.get("train_ready") is True,
        "learner_enabled": True,
        "model_ready": False,
        "shadow_ready": False,
        "promotion_ready": False,
        "policy_domain": "exam_policy",
        "rows": exact_transition_count,
        "split_counts": exact_inventory["split_transition_counts"],
        "filter": (
            "frozen exact main-action-v1 native transitions only; excludes "
            "legacy exact, legal-only and secondary rows"
        ),
        "model": "shared authoritative-legal-candidate pointer MLP [128,64] ReLU",
        "candidate_encoding": (
            "sqrt-normalized field-token bag: kind/slot/kind-slot/upgrade/"
            "play-count/card-class/card-or-drink-id"
        ),
        "input": (
            "native state_before + ordered authoritative legal candidates; "
            "excludes state_after/reward/terminal/future information"
        ),
        "target": (
            "unique chosen member; GUID binds row-local identity but is never "
            "a learned semantic feature"
        ),
        "loss": "candidate-set masked cross entropy with trajectory/Plan balance",
        "metrics": [
            "top-1/top-3/MRR/NLL",
            "macro top-1 by Plan/flow/stage/action family",
            "chosen action authoritative-legal membership",
        ],
        "limitations": [
            (
                f"{exact_transition_count} transitions across "
                f"{exact_inventory['trajectory_count']} trajectories and "
                f"{exact_inventory['stage_count']} complete stages"
            ),
            (
                "every observed flow has train/validation/test trajectory coverage"
                if exact_inventory.get("all_flow_trajectory_splits_complete") is True
                else "at least one observed flow lacks a trajectory split"
            ),
            (
                "all flow-by-stage-by-split cells are observed"
                if exact_inventory.get("all_flow_stage_splits_complete") is True
                else (
                    f"{len(exact_inventory.get('missing_flow_stage_split_cells', []))} "
                    "flow-by-stage-by-split cells remain unobserved"
                )
            ),
            "NIA Pro exact state only; Master is behavior-prior-only",
        ],
    }
    offline_rl = {
        "dataset_ready": exact_manifest.get("train_ready") is True,
        "learner_enabled": False,
        "model_ready": False,
        "shadow_ready": False,
        "promotion_ready": False,
        "policy_domain": "exam_policy",
        "algorithm": "discrete IQL; discrete CQL conservative baseline",
        "bc_anchor": "exact_main_action_bc only",
        "row_level_transitions": exact_transition_count,
        "complete_stage_count": exact_inventory["stage_count"],
        "split_counts": exact_inventory["split_transition_counts"],
        "reward": "state_after.score-state_before.score; gamma=1.0 finite stage",
        "terminal": "authoritative native terminal once at stage end",
        "blockers": [],
    }

    base.update(
        {
            "schema": SPEC_V2_SCHEMA,
            "version": 2,
            "random_seed": 20260829,
            "dataset_lock": {
                "behavior": behavior_lock,
                "exact_exam": {
                    "manifest_path": str(exact_manifest_path),
                    "manifest_sha256": _sha256_file(exact_manifest_path),
                    "stages_path": str(exact_stages_path),
                    "stages_sha256": exact_manifest.get("stages_sha256"),
                    "canonical_labels_manifest_path": str(
                        exact_canonical_manifest_path
                    ),
                    "canonical_labels_manifest_sha256": _sha256_file(
                        exact_canonical_manifest_path
                    ),
                    "trajectory_split_manifest_path": str(exact_split_manifest_path),
                    "trajectory_split_manifest_sha256": _sha256_file(
                        exact_split_manifest_path
                    ),
                    "split_owner": "frozen candidate trajectory manifest",
                    "state_authority": "frozen native runtime S,A,S'",
                    "player_identity_available": False,
                    "player_leakage_risk": True,
                },
            },
            "inventory": {
                "behavior": behavior_inventory,
                "exact_exam": exact_inventory,
            },
            "shared_features": {
                "categorical_hash": {
                    "algorithm": "sha256-feature-hashing-v1",
                    "context_dimensions": 4096,
                    "candidate_dimensions_by_objective": {
                        "outer_bc": 1024,
                        "exact_main_action_bc": 65536,
                    },
                    "signed": False,
                },
                "numeric": {
                    "encoding": "deterministic magnitude buckets as categorical tokens",
                    "missing_values": "explicit missing/presence tokens where available",
                    "future_normalization": "requires a new feature-schema version",
                },
            },
            "policy_domains": {
                "outer_policy": {
                    "scope": "cultivation choices outside Exam",
                    "state_authority": "bounded controller observation",
                    "action_identity": "one exact member of the current outer candidate set",
                    "model_role": "outer_bc",
                },
                "exam_syntax_prior": {
                    "scope": "official replay action sequence syntax",
                    "state_authority": "static replay episode plus past action prefix",
                    "action_identity": "official ordered action family/index tuple",
                    "model_role": "replay_sequence_bc",
                    "live_action_authority": False,
                },
                "exam_policy": {
                    "scope": "NIA Exam main actions",
                    "state": {
                        "authority": "frozen DLL native runtime snapshot",
                        "representation": (
                            "fixed native top-level field set; observed counts="
                            + ",".join(
                                sorted(
                                    str(value)
                                    for value in _mapping(
                                        exact_inventory.get(
                                            "state_top_level_field_counts"
                                        )
                                    )
                                )
                            )
                        ),
                        "required_pair": "same-runtime state_before/state_after",
                    },
                    "action": {
                        "surface": "main-action-v1",
                        "families": ["use-hand", "use-drink", "turn-end"],
                        "identity": "unique member of ordered authoritative legal candidates",
                        "runtime_guid_policy": "binding-only; never a semantic feature",
                    },
                    "legal_mask": {
                        "authority": "same native state_before",
                        "ordered": True,
                        "complete": True,
                        "chosen_membership": "exactly one",
                    },
                    "reward": {
                        "definition": "state_after.score-state_before.score",
                        "gamma": 1.0,
                        "terminal_score_broadcast": False,
                    },
                    "terminal": "native terminal; exactly once at final transition",
                    "secondary_action": {
                        "surface": "secondary-effect-card-select-v1",
                        "treatment": "parent-bound auxiliary evidence",
                        "flat_transition": False,
                        "reward_assigned": False,
                        "future_model_head": "separate conditional selector",
                        "evidence": secondary_evidence,
                    },
                    "split": {
                        "unit": "trajectory",
                        "per_plan_train_validation_test": True,
                        "cross_split_trajectory_allowed": False,
                    },
                    "model_roles": ["exact_main_action_bc", "offline_rl"],
                },
            },
            "objectives": {
                "outer_bc": outer_bc,
                "replay_sequence_bc": replay_bc,
                "exact_main_action_bc": exact_exam_bc,
                "offline_rl": offline_rl,
            },
            "runtime_promotion": {
                "outer_bc": "shadow-only after held-out acceptance",
                "replay_sequence_bc": "syntax-prior-only; never selects a live action",
                "exact_main_action_bc": "shadow-only after held-out acceptance",
                "offline_rl": "not available until learner/model/shadow are complete",
                "default_enabled": False,
            },
            "source_status": {
                **dict(_mapping(base.get("source_status"))),
                "behavior_replay_primary_episodes": behavior_counts.get(
                    "replay_primary"
                ),
                "behavior_replay_primary_actions": behavior_counts.get(
                    "replay_primary_actions"
                ),
                "exact_exam_dataset_ready": exact_manifest.get("train_ready") is True,
                "exact_exam_stage_count": exact_inventory["stage_count"],
                "exact_exam_transition_count": exact_transition_count,
                "v1_coverage_is_provenance_only": True,
            },
        }
    )
    spec = dict(base)
    spec["spec_sha256"] = hashlib.sha256(_canonical_bytes(spec)).hexdigest()
    return spec


def write_training_spec(
    output_path: Path = DEFAULT_OUTPUT,
    **kwargs: Any,
) -> dict[str, object]:
    spec = build_training_spec(**kwargs)
    _atomic_write(
        Path(output_path),
        json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )
    return spec


def write_training_spec_v2(
    output_path: Path = DEFAULT_V2_OUTPUT,
    **kwargs: Any,
) -> dict[str, object]:
    spec = build_training_spec_v2(**kwargs)
    _atomic_write(
        Path(output_path),
        json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )
    return spec


def write_scoped_training_spec(output_path: Path, **kwargs: Any) -> dict[str, object]:
    spec = build_scoped_training_spec(**kwargs)
    _atomic_write(Path(output_path), json.dumps(
        spec, ensure_ascii=False, sort_keys=True, indent=2,
    ).encode("utf-8") + b"\n")
    return spec


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a frozen GKMS training spec")
    parser.add_argument("--frozen-root", type=Path, default=DEFAULT_FROZEN_ROOT)
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument(
        "--inner-readiness", type=Path, default=DEFAULT_INNER_READINESS
    )
    parser.add_argument(
        "--exact-rl-root",
        type=Path,
        help="build spec v2 and lock this frozen exact-RL dataset",
    )
    parser.add_argument(
        "--secondary-action-evidence",
        type=Path,
        default=DEFAULT_SECONDARY_ACTION_EVIDENCE,
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--exact-flow", action="append", help=(
        "explicit mode|plan|effect scope; repeat for multiple flows; requires --exact-rl-root and --output"
    ))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    common = {
        "frozen_root": args.frozen_root,
        "coverage_path": args.coverage,
        "inner_readiness_path": args.inner_readiness,
    }
    if args.exact_flow:
        if args.exact_rl_root is None or args.output is None:
            raise ValueError("--exact-flow requires --exact-rl-root and --output")
        output = args.output
        spec = write_scoped_training_spec(output, exact_rl_root=args.exact_rl_root, flows=args.exact_flow)
    elif args.exact_rl_root is not None:
        output = args.output or DEFAULT_V2_OUTPUT
        spec = write_training_spec_v2(
            output,
            **common,
            exact_rl_root=args.exact_rl_root,
            secondary_action_evidence_path=args.secondary_action_evidence,
        )
    else:
        output = args.output or DEFAULT_OUTPUT
        spec = write_training_spec(output, **common)
    print(
        json.dumps(
            {
                "schema": spec["schema"],
                "inventory": spec["inventory"],
                "offline_rl": _mapping(spec["objectives"]).get("offline_rl"),
                "output": str(output),
                "spec_sha256": spec["spec_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


__all__ = [
    "DEFAULT_EXACT_RL_ROOT",
    "DEFAULT_FROZEN_ROOT",
    "DEFAULT_OUTPUT",
    "DEFAULT_V2_OUTPUT",
    "SPEC_SCHEMA",
    "SPEC_V2_SCHEMA",
    "build_training_spec",
    "build_training_spec_v2",
    "main",
    "write_training_spec",
    "write_training_spec_v2",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
