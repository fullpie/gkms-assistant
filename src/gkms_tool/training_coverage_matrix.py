"""Deterministic two-mode x six-archetype training coverage matrix.

The report consumes canonical training-label v1 rows without rewriting them.
Only rows whose dedupe status is ``canonical`` and whose complete flow identity
matches one of the declared N.I.A. cells contribute to the matrix.  Data
availability and runtime/promotion evidence remain separate axes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Final

from .canonical_training_labels import (
    INNER_TRANSITION_SCHEMA,
    FRAGMENT_TRANSITION_SCHEMA,
    LABEL_SCHEMA,
    LEADERBOARD_EPISODE_SCHEMA,
    MANIFEST_SCHEMA,
    RUNTIME_LEGAL_SCHEMA,
)
from .training_artifact_io import (
    atomic_write as _atomic_write,
    canonical_json_bytes as _canonical_bytes,
    sha256_file as _sha256_file,
)


REPORT_SCHEMA: Final = "gkms.training-coverage-matrix.v1"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_LABELS: Final = (
    PROJECT_ROOT / "var" / "training_labels" / "canonical_v1" / "labels.jsonl"
)
DEFAULT_MANIFEST: Final = DEFAULT_LABELS.with_name("manifest.json")
DEFAULT_READINESS: Final = (
    PROJECT_ROOT
    / "var"
    / "nia_training"
    / "native_search_imitation_promotion_readiness_v5.json"
)
DEFAULT_INNER_READINESS: Final = (
    PROJECT_ROOT / "var" / "nia_training" / "full_rl_readiness.json"
)
DEFAULT_OUTPUT: Final = (
    PROJECT_ROOT / "var" / "coverage" / "training_coverage_matrix_v1.json"
)
DEFAULT_LEADERBOARD_ROOT: Final = PROJECT_ROOT / "var" / "leaderboard_dataset"

MODES: Final = (
    ("produce-004", "nia_pro"),
    ("produce-005", "nia_master"),
)
ARCHETYPES: Final = (
    (
        "lesson_buff",
        "ProducePlanType_Plan1",
        "ProduceExamEffectType_ExamLessonBuff",
    ),
    (
        "parameter_buff",
        "ProducePlanType_Plan1",
        "ProduceExamEffectType_ExamParameterBuff",
    ),
    (
        "review",
        "ProducePlanType_Plan2",
        "ProduceExamEffectType_ExamReview",
    ),
    (
        "card_play_aggressive",
        "ProducePlanType_Plan2",
        "ProduceExamEffectType_ExamCardPlayAggressive",
    ),
    (
        "concentration",
        "ProducePlanType_Plan3",
        "ProduceExamEffectType_ExamConcentration",
    ),
    (
        "full_power",
        "ProducePlanType_Plan3",
        "ProduceExamEffectType_ExamFullPower",
    ),
)
STAGES: Final = ("Mid1", "Mid2", "Final")
_STAGE_BY_AUDITION_INDEX: Final = {0: "Mid1", 1: "Mid2", 2: "Final"}
_ARCHETYPE_BY_PLAN_EFFECT: Final = {
    (plan_type, effect): archetype
    for archetype, plan_type, effect in ARCHETYPES
}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _plain_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _flow_key(produce_id: str, plan_type: str, exam_effect_type: str) -> str:
    return f"{produce_id}|{plan_type}|{exam_effect_type}"


@dataclass
class _Cell:
    produce_id: str
    mode: str
    archetype: str
    plan_type: str
    exam_effect_type: str
    label_rows: int = 0
    training_eligible_rows: int = 0
    behavior_only_rows: int = 0
    exact_transition_rows: int = 0
    fragment_exact_transition_rows: int = 0
    full_rl_policy_ready_rows: int = 0
    exact_complete_unified_legal_rows: int = 0
    fragment_exact_complete_unified_legal_rows: int = 0
    runtime_legal_rows: int = 0
    official_replay_episode_rows: int = 0
    official_replay_action_count: int = 0
    official_replay_provenance_missing_rows: int = 0
    outer_decision_rows: int = 0
    live_rows: int = 0
    live_exact_transition_rows: int = 0
    quality_tiers: Counter[str] = field(default_factory=Counter)
    source_primary: Counter[str] = field(default_factory=Counter)
    source_schemas: Counter[str] = field(default_factory=Counter)
    stage_rows: Counter[str] = field(default_factory=Counter)
    official_stage_rows: Counter[str] = field(default_factory=Counter)
    official_action_types: Counter[str] = field(default_factory=Counter)
    idol_card_ids: set[str] = field(default_factory=set)
    trajectories: set[str] = field(default_factory=set)
    official_trajectories: set[str] = field(default_factory=set)
    official_trajectory_auditions: dict[str, list[int]] = field(
        default_factory=dict
    )
    official_episodes: set[str] = field(default_factory=set)
    outer_trajectories: set[str] = field(default_factory=set)
    live_trajectories: set[str] = field(default_factory=set)
    official_scores: list[int | float] = field(default_factory=list)

    @property
    def flow(self) -> str:
        return _flow_key(self.produce_id, self.plan_type, self.exam_effect_type)

    def add(self, row: Mapping[str, Any]) -> None:
        self.label_rows += 1
        quality = _mapping(row.get("quality"))
        exact = _mapping(row.get("exact"))
        legal = _mapping(row.get("legal"))
        identity = _mapping(row.get("identity"))
        scope = _mapping(row.get("scope"))
        source = _mapping(row.get("source"))
        transition = _mapping(row.get("transition"))
        reward = _mapping(row.get("reward"))
        source_schema = str(row.get("source_schema", "unknown"))
        compatibility = row.get("compatibility")
        compatibility_values = (
            {str(value) for value in compatibility}
            if isinstance(compatibility, list)
            else set()
        )
        source_primary = str(source.get("primary", "unknown"))
        stage = str(scope.get("stage", "unknown"))

        self.quality_tiers[str(quality.get("tier", "D"))] += 1
        self.source_primary[source_primary] += 1
        self.source_schemas[source_schema] += 1
        self.stage_rows[stage] += 1
        if quality.get("training_eligible") is True:
            self.training_eligible_rows += 1
        if row.get("behavior_only") is True:
            self.behavior_only_rows += 1
        if exact.get("transition") is True:
            if source_schema == FRAGMENT_TRANSITION_SCHEMA:
                self.fragment_exact_transition_rows += 1
            else:
                self.exact_transition_rows += 1
        if row.get("full_rl_policy_ready") is True:
            self.full_rl_policy_ready_rows += 1
        if (
            legal.get("candidate_scope") == "unified"
            and legal.get("complete") is True
            and legal.get("exact") is True
            and legal.get("chosen_member") is True
        ):
            if source_schema == FRAGMENT_TRANSITION_SCHEMA:
                self.fragment_exact_complete_unified_legal_rows += 1
            else:
                self.exact_complete_unified_legal_rows += 1
        if source_schema == RUNTIME_LEGAL_SCHEMA:
            self.runtime_legal_rows += 1

        idol_card_id = scope.get("idol_card_id")
        if isinstance(idol_card_id, str) and idol_card_id:
            self.idol_card_ids.add(idol_card_id)
        trajectory_id = identity.get("trajectory_id")
        if isinstance(trajectory_id, str) and trajectory_id:
            self.trajectories.add(trajectory_id)

        if (
            source_schema == LEADERBOARD_EPISODE_SCHEMA
            and "leaderboard-v6-corpus" in compatibility_values
        ):
            self.official_replay_episode_rows += 1
            self.official_stage_rows[stage] += 1
            if isinstance(trajectory_id, str) and trajectory_id:
                self.official_trajectories.add(trajectory_id)
                audition_index = identity.get("audition_index")
                if isinstance(audition_index, int) and not isinstance(
                    audition_index, bool
                ):
                    self.official_trajectory_auditions.setdefault(
                        trajectory_id, []
                    ).append(audition_index)
            episode_id = identity.get("episode_id")
            if isinstance(episode_id, str) and episode_id:
                self.official_episodes.add(episode_id)
            action = _mapping(transition.get("action"))
            actions = action.get("actions")
            if isinstance(actions, list):
                self.official_replay_action_count += len(actions)
                for value in actions:
                    if isinstance(value, Mapping):
                        self.official_action_types[
                            str(value.get("action_type", "unknown"))
                        ] += 1
            provenance = source.get("provenance")
            if not isinstance(provenance, list) or not provenance:
                self.official_replay_provenance_missing_rows += 1
            score = _plain_number(reward.get("score"))
            if score is not None:
                self.official_scores.append(score)

        if row.get("granularity") == "decision" and legal.get(
            "candidate_scope"
        ) == "outer":
            self.outer_decision_rows += 1
            if isinstance(trajectory_id, str) and trajectory_id:
                self.outer_trajectories.add(trajectory_id)

        if source_primary == "live":
            self.live_rows += 1
            if (
                exact.get("transition") is True
                and source_schema != FRAGMENT_TRANSITION_SCHEMA
            ):
                self.live_exact_transition_rows += 1
            if isinstance(trajectory_id, str) and trajectory_id:
                self.live_trajectories.add(trajectory_id)

    def finish(
        self,
        readiness: Mapping[str, Any] | None,
        overlay: Mapping[str, int],
    ) -> dict[str, object]:
        runtime = _runtime_projection(readiness)
        score_summary: dict[str, int | float | None] = {
            "count": len(self.official_scores),
            "minimum": min(self.official_scores) if self.official_scores else None,
            "median": median(self.official_scores) if self.official_scores else None,
            "maximum": max(self.official_scores) if self.official_scores else None,
        }
        official_stages = {
            stage: int(self.official_stage_rows.get(stage, 0)) for stage in STAGES
        }
        complete_trajectories = sum(
            1
            for auditions in self.official_trajectory_auditions.values()
            if sorted(auditions) == [0, 1, 2]
        )
        return {
            "flow": self.flow,
            "produce_id": self.produce_id,
            "mode": self.mode,
            "archetype": self.archetype,
            "plan_type": self.plan_type,
            "exam_effect_type": self.exam_effect_type,
            "data": {
                "canonical_label_rows": self.label_rows,
                "training_eligible_rows": self.training_eligible_rows,
                "behavior_only_rows": self.behavior_only_rows,
                "quality_tiers": dict(sorted(self.quality_tiers.items())),
                "source_primary": dict(sorted(self.source_primary.items())),
                "source_schemas": dict(sorted(self.source_schemas.items())),
                "stage_rows": dict(sorted(self.stage_rows.items())),
                "idol_card_count": len(self.idol_card_ids),
                "trajectory_count": len(self.trajectories),
                "official_replay": {
                    "episode_rows": self.official_replay_episode_rows,
                    "episode_count": len(self.official_episodes),
                    "trajectory_count": len(self.official_trajectories),
                    "complete_trajectory_count": complete_trajectories,
                    "incomplete_trajectory_count": (
                        len(self.official_trajectories) - complete_trajectories
                    ),
                    "action_count": self.official_replay_action_count,
                    "action_type_counts": dict(
                        sorted(self.official_action_types.items())
                    ),
                    "provenance_missing_episode_rows": (
                        self.official_replay_provenance_missing_rows
                    ),
                    "stage_episode_rows": official_stages,
                    "all_three_stages_present": all(
                        official_stages[stage] > 0 for stage in STAGES
                    ),
                    "score": score_summary,
                },
                "native_verified_overlay": dict(overlay),
                "outer_behavior": {
                    "decision_rows": self.outer_decision_rows,
                    "trajectory_count": len(self.outer_trajectories),
                },
                "live": {
                    "rows": self.live_rows,
                    "trajectory_count": len(self.live_trajectories),
                    "exact_transition_rows": self.live_exact_transition_rows,
                },
                "exact_transition_rows": self.exact_transition_rows,
                "fragment_exact_transition_rows": self.fragment_exact_transition_rows,
                "unified_policy": {
                    "exact_complete_legal_rows": self.exact_complete_unified_legal_rows,
                    "fragment_exact_complete_legal_rows": (
                        self.fragment_exact_complete_unified_legal_rows
                    ),
                    "runtime_legal_rows": self.runtime_legal_rows,
                    "row_level_full_rl_policy_ready_rows": self.full_rl_policy_ready_rows,
                },
            },
            "runtime_evidence": runtime,
        }


def _runtime_projection(row: Mapping[str, Any] | None) -> dict[str, object]:
    if row is None:
        return {
            "readiness_row_present": False,
            "evaluation_status": "not_evaluated",
            "simulator_transition_verified": None,
            "legal_set_verified": None,
            "strict_leaderboard_join_verified": None,
            "dll_legal_set_promotion_allowed": None,
            "strategy_promotion_allowed": None,
            "default_enabled": None,
            "blockers": ["readiness-flow-not-present"],
        }
    blockers = row.get("blockers")
    return {
        "readiness_row_present": True,
        "evaluation_status": "evaluated",
        "simulator_transition_verified": row.get(
            "simulator_transition_verified"
        )
        is True,
        "legal_set_verified": row.get("legal_set_verified") is True,
        "strict_leaderboard_join_verified": row.get(
            "strict_leaderboard_join_verified"
        )
        is True,
        "dll_legal_set_promotion_allowed": row.get(
            "dll_legal_set_promotion_allowed"
        )
        is True,
        "strategy_promotion_allowed": row.get("strategy_promotion_allowed") is True,
        "default_enabled": row.get("default_enabled") is True,
        "blockers": sorted(str(value) for value in blockers)
        if isinstance(blockers, list)
        else [],
    }


def _inner_readiness_projection(
    row: Mapping[str, Any] | None,
) -> dict[str, object]:
    if row is None:
        return {
            "present": False,
            "training_ready": None,
            "full_rl_policy_ready": None,
            "transition_count": None,
            "policy_transition_count": None,
            "complete_stage_count": None,
            "complete_stage_independent_source_count": None,
            "blockers": ["inner-training-readiness-not-present"],
        }
    blockers = row.get("blockers")
    return {
        "present": True,
        "schema": row.get("schema"),
        "training_ready": row.get("training_ready") is True,
        "full_rl_policy_ready": row.get("full_rl_policy_ready") is True,
        "transition_count": row.get("transition_count"),
        "policy_transition_count": row.get("policy_transition_count"),
        "stage_count": row.get("stage_count"),
        "continuous_segment_count": row.get("continuous_segment_count"),
        "exact_dynamics_stage_count": row.get("exact_dynamics_stage_count"),
        "complete_stage_count": row.get("complete_stage_count"),
        "complete_stage_independent_source_count": row.get(
            "complete_stage_independent_source_count"
        ),
        "blockers": sorted(str(value) for value in blockers)
        if isinstance(blockers, list)
        else [],
    }


def _read_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _available_episode_union(
    root: Path | None,
    *,
    current_corpus_episode_ids: set[str],
) -> dict[str, object]:
    if root is None or not Path(root).is_dir():
        return {
            "present": False,
            "source_file_count": 0,
            "blockers": ["leaderboard-dataset-root-not-present"],
        }
    root = Path(root)
    sources = sorted(
        {
            *root.rglob("episodes.jsonl"),
            *root.glob("runtime_replay_*.jsonl"),
        },
        key=lambda value: value.as_posix().casefold(),
    )
    # episode_id -> (flow, produce_id, trajectory_id, audition_index,
    #                 action_count, action+flow fingerprint)
    episodes: dict[
        str, tuple[str | None, str | None, str, int, int, str]
    ] = {}
    conflicts: set[str] = set()
    raw_rows = 0
    non_episode_rows = 0
    invalid_rows = 0
    duplicate_identity_rows = 0
    for path in sources:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                raw_rows += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    invalid_rows += 1
                    continue
                if not isinstance(row, Mapping) or row.get("schema") != LEADERBOARD_EPISODE_SCHEMA:
                    non_episode_rows += 1
                    continue
                trajectory_id = row.get("trajectory_id")
                audition_index = row.get("audition_index")
                actions = row.get("actions")
                if (
                    not isinstance(trajectory_id, str)
                    or not trajectory_id
                    or not isinstance(audition_index, int)
                    or isinstance(audition_index, bool)
                    or audition_index not in _STAGE_BY_AUDITION_INDEX
                    or not isinstance(actions, list)
                ):
                    invalid_rows += 1
                    continue
                produce_id = row.get("produce_id")
                plan_type = row.get("plan_type")
                effect = row.get("exam_effect_type")
                archetype = _ARCHETYPE_BY_PLAN_EFFECT.get((plan_type, effect))
                flow = (
                    _flow_key(produce_id, plan_type, effect)
                    if isinstance(produce_id, str)
                    and produce_id in {value[0] for value in MODES}
                    and isinstance(plan_type, str)
                    and isinstance(effect, str)
                    and archetype is not None
                    else None
                )
                episode_id = f"{trajectory_id}:audition:{audition_index}"
                fingerprint = hashlib.sha256(
                    _canonical_bytes(
                        {
                            "produce_id": produce_id,
                            "plan_type": plan_type,
                            "exam_effect_type": effect,
                            "actions": actions,
                        }
                    )
                ).hexdigest()
                compact = (
                    flow,
                    produce_id if isinstance(produce_id, str) else None,
                    trajectory_id,
                    audition_index,
                    len(actions),
                    fingerprint,
                )
                previous = episodes.get(episode_id)
                if previous is None:
                    episodes[episode_id] = compact
                elif previous[-1] == fingerprint:
                    duplicate_identity_rows += 1
                else:
                    conflicts.add(episode_id)

    for episode_id in conflicts:
        episodes.pop(episode_id, None)

    def summarize(
        selected: Mapping[
            str, tuple[str | None, str | None, str, int, int, str]
        ],
    ) -> dict[str, object]:
        trajectories: dict[str, list[int]] = defaultdict(list)
        action_count = 0
        for _episode_id, (
            _flow,
            _produce_id,
            trajectory_id,
            audition_index,
            actions,
            _fingerprint,
        ) in selected.items():
            trajectories[trajectory_id].append(audition_index)
            action_count += actions
        complete = sum(
            1 for auditions in trajectories.values() if sorted(auditions) == [0, 1, 2]
        )
        return {
            "episode_count": len(selected),
            "trajectory_count": len(trajectories),
            "complete_trajectory_count": complete,
            "incomplete_trajectory_count": len(trajectories) - complete,
            "action_count": action_count,
        }

    ten_flow = {key: value for key, value in episodes.items() if value[0] is not None}
    out_of_scope = {key: value for key, value in episodes.items() if value[0] is None}
    additional = {
        key: value
        for key, value in episodes.items()
        if key not in current_corpus_episode_ids
    }
    additional_ten_flow = {
        key: value for key, value in additional.items() if value[0] is not None
    }
    by_flow: dict[str, dict[str, object]] = {}
    additional_by_flow: dict[str, dict[str, object]] = {}
    for produce_id, _mode in MODES:
        for archetype, plan_type, effect in ARCHETYPES:
            flow = _flow_key(produce_id, plan_type, effect)
            by_flow[flow] = summarize(
                {key: value for key, value in ten_flow.items() if value[0] == flow}
            )
            additional_by_flow[flow] = summarize(
                {
                    key: value
                    for key, value in additional_ten_flow.items()
                    if value[0] == flow
                }
            )
    return {
        "present": True,
        "identity": "trajectory_id + audition_index",
        "conflict_policy": "exclude same episode identity when action stream or flow conflicts",
        "source_file_count": len(sources),
        "source_files": [
            {
                "path": str(path),
                "bytes": path.stat().st_size,
            }
            for path in sources
        ],
        "raw_row_count": raw_rows,
        "non_episode_row_count": non_episode_rows,
        "invalid_row_count": invalid_rows,
        "duplicate_identity_row_count": duplicate_identity_rows,
        "conflict_episode_count": len(conflicts),
        "all_archetypes": summarize(episodes),
        "ten_cell_union": summarize(ten_flow),
        "out_of_scope_union": summarize(out_of_scope),
        "out_of_scope_by_mode": {
            produce_id: summarize(
                {
                    key: value
                    for key, value in out_of_scope.items()
                    if value[1] == produce_id
                }
            )
            for produce_id, _mode in MODES
        },
        "additional_beyond_current_v6": {
            **summarize(additional),
            "ten_cell": summarize(additional_ten_flow),
            "by_flow": additional_by_flow,
        },
        "by_flow": by_flow,
    }


def build_training_coverage_matrix(
    *,
    labels_path: Path = DEFAULT_LABELS,
    manifest_path: Path = DEFAULT_MANIFEST,
    readiness_path: Path | None = DEFAULT_READINESS,
    inner_readiness_path: Path | None = DEFAULT_INNER_READINESS,
    leaderboard_root: Path | None = DEFAULT_LEADERBOARD_ROOT,
    verify_input_hash: bool = False,
) -> dict[str, object]:
    labels_path = Path(labels_path)
    manifest_path = Path(manifest_path)
    if not labels_path.is_file():
        raise FileNotFoundError(labels_path)
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = _read_json(manifest_path)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("canonical label manifest schema mismatch")
    if manifest.get("label_schema") != LABEL_SCHEMA:
        raise ValueError("canonical label schema mismatch")
    artifacts = _mapping(manifest.get("artifacts"))
    declared_sha = artifacts.get("labels_sha256")
    if not isinstance(declared_sha, str) or len(declared_sha) != 64:
        raise ValueError("canonical label manifest has no labels SHA-256")
    actual_sha = _sha256_file(labels_path) if verify_input_hash else None
    if actual_sha is not None and actual_sha != declared_sha:
        raise ValueError("canonical label SHA-256 mismatch")

    cells: dict[tuple[str, str], _Cell] = {}
    for produce_id, mode in MODES:
        for archetype, plan_type, effect in ARCHETYPES:
            cells[(produce_id, archetype)] = _Cell(
                produce_id=produce_id,
                mode=mode,
                archetype=archetype,
                plan_type=plan_type,
                exam_effect_type=effect,
            )

    exclusions: Counter[str] = Counter()
    corpus_actions_by_episode: dict[str, str] = {}
    current_corpus_records: dict[
        str, tuple[str | None, str | None, str, int, int]
    ] = {}
    overlay_entries: list[tuple[str | None, str | None, str]] = []
    input_rows = 0
    with labels_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            input_rows += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"invalid canonical label JSON at line {line_number}"
                ) from error
            if not isinstance(row, Mapping) or row.get("schema") != LABEL_SCHEMA:
                raise ValueError(
                    f"canonical label schema mismatch at line {line_number}"
                )
            scope = _mapping(row.get("scope"))
            produce_id = scope.get("produce_id")
            archetype = scope.get("archetype")
            cell = (
                cells.get((produce_id, archetype))
                if isinstance(produce_id, str) and isinstance(archetype, str)
                else None
            )
            flow = cell.flow if cell is not None else None
            if row.get("source_schema") == LEADERBOARD_EPISODE_SCHEMA:
                compatibility = row.get("compatibility")
                compatibility_values = (
                    {str(value) for value in compatibility}
                    if isinstance(compatibility, list)
                    else set()
                )
                identity = _mapping(row.get("identity"))
                episode_id = identity.get("episode_id")
                episode_identity = (
                    episode_id if isinstance(episode_id, str) and episode_id else None
                )
                action_digest = hashlib.sha256(
                    _canonical_bytes(_mapping(_mapping(row.get("transition")).get("action")))
                ).hexdigest()
                if (
                    "leaderboard-v6-corpus" in compatibility_values
                    and episode_identity is not None
                ):
                    corpus_actions_by_episode[episode_identity] = action_digest
                    trajectory_id = identity.get("trajectory_id")
                    audition_index = identity.get("audition_index")
                    raw_actions = _mapping(
                        _mapping(row.get("transition")).get("action")
                    ).get("actions")
                    if (
                        isinstance(trajectory_id, str)
                        and trajectory_id
                        and isinstance(audition_index, int)
                        and not isinstance(audition_index, bool)
                        and isinstance(raw_actions, list)
                    ):
                        current_corpus_records[episode_identity] = (
                            flow,
                            produce_id if isinstance(produce_id, str) else None,
                            trajectory_id,
                            audition_index,
                            len(raw_actions),
                        )
                else:
                    source_record = _mapping(row.get("source_record"))
                    source_path = str(source_record.get("path", "")).replace("\\", "/")
                    if "v330_nia_native_verified_v1/episodes.jsonl" in source_path:
                        overlay_entries.append((flow, episode_identity, action_digest))

            dedupe = _mapping(row.get("dedupe"))
            dedupe_status = dedupe.get("status")
            if dedupe_status != "canonical":
                exclusions[f"dedupe:{dedupe_status or 'unknown'}"] += 1
                continue
            if not isinstance(produce_id, str) or not isinstance(archetype, str):
                exclusions["unknown-flow-identity"] += 1
                continue
            if cell is None:
                exclusions["outside-ten-cell-matrix"] += 1
                continue
            if (
                scope.get("mode") != cell.mode
                or scope.get("plan_type") != cell.plan_type
                or scope.get("exam_effect_type") != cell.exam_effect_type
            ):
                exclusions["flow-identity-mismatch"] += 1
                continue
            cell.add(row)

    overlay_totals: Counter[str] = Counter()
    overlay_by_flow: dict[str, Counter[str]] = defaultdict(Counter)
    for flow, episode_id, action_digest in overlay_entries:
        overlay_totals["total"] += 1
        if episode_id is None:
            status = "identity_missing"
        elif episode_id not in corpus_actions_by_episode:
            status = "historical_unmatched"
        elif corpus_actions_by_episode[episode_id] == action_digest:
            status = "current_exact_join"
        else:
            status = "action_mismatch"
        overlay_totals[status] += 1
        if flow is not None:
            overlay_by_flow[flow]["total"] += 1
            overlay_by_flow[flow][status] += 1

    def summarize_current_corpus(
        selected: Mapping[
            str, tuple[str | None, str | None, str, int, int]
        ],
    ) -> dict[str, int]:
        trajectories: dict[str, list[int]] = defaultdict(list)
        action_count = 0
        for _flow, _produce_id, trajectory_id, audition_index, actions in selected.values():
            trajectories[trajectory_id].append(audition_index)
            action_count += actions
        complete = sum(
            1 for values in trajectories.values() if sorted(values) == [0, 1, 2]
        )
        return {
            "episode_count": len(selected),
            "trajectory_count": len(trajectories),
            "complete_trajectory_count": complete,
            "incomplete_trajectory_count": len(trajectories) - complete,
            "action_count": action_count,
        }

    current_ten = {
        key: value for key, value in current_corpus_records.items() if value[0] is not None
    }
    current_out_of_scope = {
        key: value for key, value in current_corpus_records.items() if value[0] is None
    }
    current_v6_corpus = {
        "all_archetypes": summarize_current_corpus(current_corpus_records),
        "ten_cell": summarize_current_corpus(current_ten),
        "out_of_scope": summarize_current_corpus(current_out_of_scope),
        "out_of_scope_by_mode": {
            produce_id: summarize_current_corpus(
                {
                    key: value
                    for key, value in current_out_of_scope.items()
                    if value[1] == produce_id
                }
            )
            for produce_id, _mode in MODES
        },
    }

    available_episode_union = _available_episode_union(
        leaderboard_root,
        current_corpus_episode_ids=set(corpus_actions_by_episode),
    )

    readiness_rows: dict[str, Mapping[str, Any]] = {}
    readiness_sha: str | None = None
    readiness_schema: str | None = None
    if readiness_path is not None and Path(readiness_path).is_file():
        readiness_path = Path(readiness_path)
        readiness = _read_json(readiness_path)
        readiness_schema = (
            str(readiness.get("schema")) if readiness.get("schema") is not None else None
        )
        readiness_sha = _sha256_file(readiness_path)
        raw_flows = readiness.get("flows")
        if not isinstance(raw_flows, list):
            raise ValueError("readiness report flows must be a list")
        for value in raw_flows:
            if not isinstance(value, Mapping):
                continue
            flow = value.get("flow")
            if isinstance(flow, str) and flow:
                readiness_rows[flow] = value

    inner_readiness: Mapping[str, Any] | None = None
    inner_readiness_sha: str | None = None
    if inner_readiness_path is not None and Path(inner_readiness_path).is_file():
        inner_readiness_path = Path(inner_readiness_path)
        inner_readiness = _read_json(inner_readiness_path)
        inner_readiness_sha = _sha256_file(inner_readiness_path)
    inner_readiness_summary = _inner_readiness_projection(inner_readiness)

    matrix = [
        cells[(produce_id, archetype)].finish(
            readiness_rows.get(cells[(produce_id, archetype)].flow),
            {
                key: int(overlay_by_flow[cells[(produce_id, archetype)].flow].get(key, 0))
                for key in (
                    "total",
                    "current_exact_join",
                    "historical_unmatched",
                    "action_mismatch",
                    "identity_missing",
                )
            },
        )
        for produce_id, _mode in MODES
        for archetype, _plan, _effect in ARCHETYPES
    ]

    def count_cells(predicate: Any) -> int:
        return sum(1 for row in matrix if predicate(row))

    summary = {
        "matrix_cell_count": len(matrix),
        "cells_with_canonical_data": count_cells(
            lambda row: _mapping(row["data"]).get("canonical_label_rows", 0) > 0
        ),
        "cells_with_official_replay": count_cells(
            lambda row: _mapping(_mapping(row["data"])["official_replay"]).get(
                "episode_rows", 0
            )
            > 0
        ),
        "cells_with_all_three_official_stages": count_cells(
            lambda row: _mapping(_mapping(row["data"])["official_replay"]).get(
                "all_three_stages_present"
            )
            is True
        ),
        "cells_with_outer_behavior": count_cells(
            lambda row: _mapping(_mapping(row["data"])["outer_behavior"]).get(
                "decision_rows", 0
            )
            > 0
        ),
        "cells_with_live_data": count_cells(
            lambda row: _mapping(_mapping(row["data"])["live"]).get("rows", 0) > 0
        ),
        "cells_with_exact_transition": count_cells(
            lambda row: _mapping(row["data"]).get("exact_transition_rows", 0)
            > 0
        ),
        "cells_with_row_level_full_rl_policy_ready_rows": count_cells(
            lambda row: _mapping(_mapping(row["data"])["unified_policy"]).get(
                "row_level_full_rl_policy_ready_rows", 0
            )
            > 0
        ),
        "simulator_transition_verified_cells": count_cells(
            lambda row: _mapping(row["runtime_evidence"]).get(
                "simulator_transition_verified"
            )
            is True
        ),
        "legal_set_verified_cells": count_cells(
            lambda row: _mapping(row["runtime_evidence"]).get("legal_set_verified")
            is True
        ),
        "strategy_promotion_allowed_cells": count_cells(
            lambda row: _mapping(row["runtime_evidence"]).get(
                "strategy_promotion_allowed"
            )
            is True
        ),
        "inner_training_ready": inner_readiness_summary.get("training_ready"),
        "inner_full_rl_policy_ready": inner_readiness_summary.get(
            "full_rl_policy_ready"
        ),
        "inner_complete_stage_count": inner_readiness_summary.get(
            "complete_stage_count"
        ),
        "available_union_additional_episode_count": _mapping(
            available_episode_union.get("additional_beyond_current_v6")
        ).get("episode_count"),
    }
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "contract": {
            "matrix": "produce-004/produce-005 x six declared archetypes",
            "declared_archetypes": [row[0] for row in ARCHETYPES],
            "declared_cell_count": len(MODES) * len(ARCHETYPES),
            "legacy_key_semantics": "ten_cell/ten_cell_union are retained schema-v1 names for all declared cells, now twelve",
            "counting": "canonical dedupe rows only; data and runtime evidence are separate",
            "official_replay": "leaderboard replay episode v2 rows carrying leaderboard-v6-corpus compatibility only",
            "native_verified_overlay": "reported separately; never added to current corpus counts",
            "outer_behavior": "decision rows with exact outer candidate scope",
            "row_level_full_rl": "canonical full_rl_policy_ready rows; dataset readiness is a separate gate",
        },
        "source": {
            "labels_path": str(labels_path),
            "labels_bytes": labels_path.stat().st_size,
            "labels_sha256_declared": declared_sha,
            "labels_sha256_verified": actual_sha == declared_sha
            if actual_sha is not None
            else None,
            "manifest_path": str(manifest_path),
            "manifest_sha256": _sha256_file(manifest_path),
            "readiness_path": str(readiness_path)
            if readiness_path is not None and Path(readiness_path).is_file()
            else None,
            "readiness_schema": readiness_schema,
            "readiness_sha256": readiness_sha,
            "inner_readiness_path": str(inner_readiness_path)
            if inner_readiness_path is not None
            and Path(inner_readiness_path).is_file()
            else None,
            "inner_readiness_sha256": inner_readiness_sha,
            "leaderboard_root": str(leaderboard_root)
            if leaderboard_root is not None and Path(leaderboard_root).is_dir()
            else None,
        },
        "input_rows": input_rows,
        "excluded_rows": dict(sorted(exclusions.items())),
        "summary": summary,
        "native_verified_overlay": {
            key: int(overlay_totals.get(key, 0))
            for key in (
                "total",
                "current_exact_join",
                "historical_unmatched",
                "action_mismatch",
                "identity_missing",
            )
        },
        "current_v6_corpus": current_v6_corpus,
        "inner_training_readiness": inner_readiness_summary,
        "available_episode_union": available_episode_union,
        "matrix": matrix,
    }
    report["report_sha256"] = hashlib.sha256(_canonical_bytes(report)).hexdigest()
    return report


def write_training_coverage_matrix(
    output_path: Path = DEFAULT_OUTPUT,
    **kwargs: Any,
) -> dict[str, object]:
    report = build_training_coverage_matrix(**kwargs)
    _atomic_write(
        Path(output_path),
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the canonical N.I.A. two-mode/five-archetype coverage matrix"
    )
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--readiness", type=Path, default=DEFAULT_READINESS)
    parser.add_argument(
        "--inner-readiness", type=Path, default=DEFAULT_INNER_READINESS
    )
    parser.add_argument(
        "--leaderboard-root", type=Path, default=DEFAULT_LEADERBOARD_ROOT
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify-input-hash", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = write_training_coverage_matrix(
        args.output,
        labels_path=args.labels,
        manifest_path=args.manifest,
        readiness_path=args.readiness,
        inner_readiness_path=args.inner_readiness,
        leaderboard_root=args.leaderboard_root,
        verify_input_hash=args.verify_input_hash,
    )
    print(
        json.dumps(
            {
                "schema": report["schema"],
                "summary": report["summary"],
                "excluded_rows": report["excluded_rows"],
                "output": str(args.output),
                "report_sha256": report["report_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


__all__ = [
    "ARCHETYPES",
    "DEFAULT_LABELS",
    "DEFAULT_INNER_READINESS",
    "DEFAULT_LEADERBOARD_ROOT",
    "DEFAULT_MANIFEST",
    "DEFAULT_OUTPUT",
    "DEFAULT_READINESS",
    "MODES",
    "REPORT_SCHEMA",
    "build_training_coverage_matrix",
    "main",
    "write_training_coverage_matrix",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
