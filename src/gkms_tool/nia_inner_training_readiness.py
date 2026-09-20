"""Read-only readiness gate for exact N.I.A. inner-policy training.

One-step transitions are useful individually, but gaps must never be treated
as a continuous trajectory.  This module splits the exact dataset at every
step/digest/state discontinuity and reports whether enough complete native
exam segments exist to start a production full-RL training run.  Exact
per-kind candidate surfaces (for example Maa card-only or drink-only rows)
remain visible as dynamics evidence, but are never promoted as a unified
policy stage unless the producer explicitly marks the candidate set unified.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path

from .nia_inner_transition_dataset import (
    DEFAULT_OUTPUT,
    NiaInnerTransition,
    load_nia_inner_transition_dataset,
)
from .nia_legal_decision_dataset import (
    NiaLegalDecision,
    load_nia_legal_decision_dataset,
)


READINESS_SCHEMA = "gkms.nia-inner-training-readiness.v1"
LEGAL_READINESS_SCHEMA = "gkms.nia-legal-decision-readiness.v1"
DEFAULT_READINESS_OUTPUT = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "nia_training"
    / "full_rl_readiness.json"
)

MAIN_EFFECT_NAMES = {
    2: "ProduceExamEffectType_ExamParameterBuff",
    10: "ProduceExamEffectType_ExamLessonBuff",
    31: "ProduceExamEffectType_ExamReview",
    42: "ProduceExamEffectType_ExamCardPlayAggressive",
    45: "ProduceExamEffectType_ExamConcentration",
}


def _stage_key(value: NiaInnerTransition) -> tuple[str, str, str]:
    metadata = value.metadata
    return (
        value.source_id,
        str(metadata.get("step_context_id", "unknown-step-context")),
        str(metadata.get("session_transition_id", "unknown-session-transition")),
    )


def _flow(value: NiaInnerTransition) -> str:
    runtime = value.state_before.get("root_runtime")
    opaque = runtime.get("opaque_fields") if isinstance(runtime, Mapping) else None
    if not isinstance(opaque, Mapping):
        return "unknown|unknown|unknown"
    produce = opaque.get("produceId", "unknown")
    plan = opaque.get("planType", "unknown")
    effect = opaque.get("mainEffectType", "unknown")
    if isinstance(plan, int) and not isinstance(plan, bool):
        plan = {
            2: "ProducePlanType_Plan1",
            3: "ProducePlanType_Plan2",
            4: "ProducePlanType_Plan3",
        }.get(plan, plan)
    if isinstance(effect, int) and not isinstance(effect, bool):
        effect = MAIN_EFFECT_NAMES.get(effect, effect)
    return f"{produce}|{plan}|{effect}"


def _metadata_digest(value: NiaInnerTransition, name: str) -> str | None:
    raw = value.metadata.get(name)
    return raw if isinstance(raw, str) and raw else None


def _candidate_set_kind(value: NiaInnerTransition) -> str:
    raw = value.metadata.get("candidate_set_kind")
    return raw if isinstance(raw, str) and raw else "legacy"


def _policy_ready(value: NiaInnerTransition) -> bool:
    """Use the row's explicit unified-candidate gate for policy training."""

    return value.full_rl_policy_ready


def _continuous(left: NiaInnerTransition, right: NiaInnerTransition) -> bool:
    if left.terminal or _stage_key(left) != _stage_key(right):
        return False
    if right.step != left.step + 1:
        return False
    if dict(left.state_after) != dict(right.state_before):
        return False
    after_digest = _metadata_digest(left, "evidence_after_digest")
    before_digest = _metadata_digest(right, "evidence_before_digest")
    return not (
        after_digest is not None
        and before_digest is not None
        and after_digest != before_digest
    )


def _stage_start(value: NiaInnerTransition) -> bool:
    turn = value.state_before.get("current_turn")
    count = value.state_before.get("exam_card_play_count")
    return turn == 1 and count == 0


def _segment_payload(values: Sequence[NiaInnerTransition]) -> dict[str, object]:
    rows = tuple(values)
    first = rows[0]
    last = rows[-1]
    exact_dynamics_stage = _stage_start(first) and last.terminal
    candidate_set_kinds = tuple(sorted({_candidate_set_kind(value) for value in rows}))
    complete = exact_dynamics_stage and all(_policy_ready(value) for value in rows)
    identity = {
        "source_id": first.source_id,
        "stage_key": list(_stage_key(first)[1:]),
        "first_step": first.step,
        "last_step": last.step,
    }
    segment_id = "inner-segment:" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "segment_id": segment_id,
        **identity,
        "flow": _flow(first),
        "transition_count": len(rows),
        "starts_at_stage_start": _stage_start(first),
        "terminal": last.terminal,
        "exact_dynamics_stage": exact_dynamics_stage,
        "candidate_set_kinds": list(candidate_set_kinds),
        "full_rl_policy_ready": complete,
        "complete_stage": complete,
    }


def audit_nia_inner_training_readiness(
    transitions: Sequence[NiaInnerTransition],
    *,
    min_transition_count: int = 100,
    min_complete_stage_count: int = 10,
    min_independent_source_count: int = 5,
) -> dict[str, object]:
    """Split exact rows into continuous segments and apply training minima."""

    for label, value in (
        ("min_transition_count", min_transition_count),
        ("min_complete_stage_count", min_complete_stage_count),
        ("min_independent_source_count", min_independent_source_count),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{label} must be a positive integer")
    values = tuple(transitions)
    if any(not isinstance(value, NiaInnerTransition) for value in values):
        raise TypeError("transitions must contain NiaInnerTransition values")

    grouped: defaultdict[tuple[str, str, str], list[NiaInnerTransition]] = defaultdict(list)
    for value in values:
        grouped[_stage_key(value)].append(value)

    segments: list[dict[str, object]] = []
    for key in sorted(grouped):
        ordered = sorted(grouped[key], key=lambda value: value.step)
        current: list[NiaInnerTransition] = []
        for value in ordered:
            if current and not _continuous(current[-1], value):
                segments.append(_segment_payload(current))
                current = []
            current.append(value)
        if current:
            segments.append(_segment_payload(current))

    complete = [value for value in segments if value["complete_stage"] is True]
    exact_dynamics = [
        value for value in segments if value["exact_dynamics_stage"] is True
    ]
    complete_sources = {str(value["source_id"]) for value in complete}
    policy_transition_count = sum(_policy_ready(value) for value in values)
    candidate_set_kind_counts: Counter[str] = Counter(
        _candidate_set_kind(value) for value in values
    )
    full_rl_policy_ready = bool(values) and policy_transition_count == len(values)
    blockers: list[str] = []
    if len(values) < min_transition_count:
        blockers.append(
            f"exact-transition-count:{len(values)}<{min_transition_count}"
        )
    if len(complete) < min_complete_stage_count:
        blockers.append(
            f"complete-stage-count:{len(complete)}<{min_complete_stage_count}"
        )
    if len(complete_sources) < min_independent_source_count:
        blockers.append(
            "complete-stage-independent-source-count:"
            f"{len(complete_sources)}<{min_independent_source_count}"
        )
    if not full_rl_policy_ready:
        blockers.append("full-rl-policy-ready:false")

    flow_counts: defaultdict[str, int] = defaultdict(int)
    for value in complete:
        flow_counts[str(value["flow"])] += 1
    return {
        "schema": READINESS_SCHEMA,
        "training_ready": not blockers,
        "transition_count": len(values),
        "full_rl_policy_ready": full_rl_policy_ready,
        "policy_transition_count": policy_transition_count,
        "source_count": len({value.source_id for value in values}),
        "stage_count": len(grouped),
        "continuous_segment_count": len(segments),
        "exact_dynamics_stage_count": len(exact_dynamics),
        "complete_stage_count": len(complete),
        "complete_stage_independent_source_count": len(complete_sources),
        "complete_stage_flow_counts": dict(sorted(flow_counts.items())),
        "candidate_set_kind_counts": dict(sorted(candidate_set_kind_counts.items())),
        "thresholds": {
            "min_transition_count": min_transition_count,
            "min_complete_stage_count": min_complete_stage_count,
            "min_independent_source_count": min_independent_source_count,
        },
        "blockers": blockers,
        "segments": segments,
    }


def audit_nia_inner_training_file(
    path: str | Path = DEFAULT_OUTPUT,
    **kwargs: int,
) -> dict[str, object]:
    return audit_nia_inner_training_readiness(
        load_nia_inner_transition_dataset(Path(path)),
        **kwargs,
    )


def audit_nia_legal_decision_readiness(
    decisions: Sequence[NiaLegalDecision],
    *,
    min_decision_count: int = 50,
    min_independent_source_count: int = 5,
    required_chosen_action_kinds: Sequence[str] = ("play", "drink", "end_turn"),
) -> dict[str, object]:
    """Report whether native-legal bandit rows cover a unified action policy."""

    if isinstance(min_decision_count, bool) or not isinstance(min_decision_count, int) or min_decision_count < 1:
        raise ValueError("min_decision_count must be a positive integer")
    if (
        isinstance(min_independent_source_count, bool)
        or not isinstance(min_independent_source_count, int)
        or min_independent_source_count < 1
    ):
        raise ValueError("min_independent_source_count must be a positive integer")
    required = tuple(required_chosen_action_kinds)
    if not required or any(not isinstance(value, str) or not value for value in required):
        raise ValueError("required_chosen_action_kinds must contain non-empty text")
    if len(required) != len(set(required)):
        raise ValueError("required_chosen_action_kinds must be unique")

    values = tuple(decisions)
    if any(not isinstance(value, NiaLegalDecision) for value in values):
        raise TypeError("decisions must contain NiaLegalDecision values")
    sources = {value.source_id for value in values}
    chosen_kinds = Counter(str(value.action.get("kind", "unknown")) for value in values)
    candidate_kinds: Counter[str] = Counter()
    scopes: defaultdict[str, dict[str, object]] = defaultdict(
        lambda: {"decision_count": 0, "sources": set(), "chosen_action_kinds": Counter()}
    )
    for value in values:
        for candidate in value.legal_candidates:
            candidate_kinds[str(candidate.get("kind", "unknown"))] += 1
        metadata = value.metadata
        scope = "|".join(
            str(metadata.get(name, "unknown"))
            for name in ("produce_id", "plan_type", "exam_effect_type")
        )
        row = scopes[scope]
        row["decision_count"] = int(row["decision_count"]) + 1
        source_values = row["sources"]
        assert isinstance(source_values, set)
        source_values.add(value.source_id)
        kind_values = row["chosen_action_kinds"]
        assert isinstance(kind_values, Counter)
        kind_values[str(value.action.get("kind", "unknown"))] += 1

    missing_kinds = [value for value in required if chosen_kinds[value] == 0]
    blockers: list[str] = []
    if len(values) < min_decision_count:
        blockers.append(f"legal-decision-count:{len(values)}<{min_decision_count}")
    if len(sources) < min_independent_source_count:
        blockers.append(
            "legal-decision-independent-source-count:"
            f"{len(sources)}<{min_independent_source_count}"
        )
    if missing_kinds:
        blockers.append("chosen-action-kinds-missing:" + ",".join(missing_kinds))

    scope_payload: dict[str, object] = {}
    for scope, raw in sorted(scopes.items()):
        source_values = raw["sources"]
        kind_values = raw["chosen_action_kinds"]
        assert isinstance(source_values, set)
        assert isinstance(kind_values, Counter)
        scope_payload[scope] = {
            "decision_count": raw["decision_count"],
            "source_count": len(source_values),
            "chosen_action_kind_counts": dict(sorted(kind_values.items())),
        }
    return {
        "schema": LEGAL_READINESS_SCHEMA,
        "contextual_policy_training_ready": not blockers,
        "descriptive_only": bool(blockers),
        "decision_count": len(values),
        "independent_source_count": len(sources),
        "scope_count": len(scopes),
        "chosen_action_kind_counts": dict(sorted(chosen_kinds.items())),
        "candidate_action_kind_counts": dict(sorted(candidate_kinds.items())),
        "thresholds": {
            "min_decision_count": min_decision_count,
            "min_independent_source_count": min_independent_source_count,
            "required_chosen_action_kinds": list(required),
        },
        "blockers": blockers,
        "scopes": scope_payload,
    }


def audit_nia_legal_decision_file(
    path: str | Path,
    **kwargs: object,
) -> dict[str, object]:
    return audit_nia_legal_decision_readiness(
        load_nia_legal_decision_dataset(path),
        **kwargs,
    )


__all__ = [
    "DEFAULT_READINESS_OUTPUT",
    "LEGAL_READINESS_SCHEMA",
    "READINESS_SCHEMA",
    "audit_nia_inner_training_file",
    "audit_nia_inner_training_readiness",
    "audit_nia_legal_decision_file",
    "audit_nia_legal_decision_readiness",
]
