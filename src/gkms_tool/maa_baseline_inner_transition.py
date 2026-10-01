"""Promote exact Maa boundaries into the exact-dynamics transition dataset.

The Maa action sidecar supplies complete detector-labelled legal candidates,
the selected card GUID, and typed settled ExamSave states before/after.  This
module only accepts those explicit rows from an authentically completed run;
ordinary behavior traces and incomplete runs remain excluded.  Because the
completion action observes card, drink, and end-turn phases separately, the
resulting rows carry per-kind candidate provenance and remain outside unified
full-RL policy readiness until a cross-kind legal surface is proven.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

from .nia_inner_training_readiness import (
    DEFAULT_READINESS_OUTPUT,
    audit_nia_inner_training_readiness,
)
from .nia_inner_transition_dataset import (
    CANDIDATE_SET_KIND_CARD_ONLY,
    CANDIDATE_SET_KIND_DRINK_ONLY,
    CANDIDATE_SET_KIND_END_TURN_ONLY,
    CANDIDATE_SET_KIND_UNKNOWN,
    CANDIDATE_SET_KIND_UNIFIED,
    DEFAULT_OUTPUT,
    NiaInnerTransition,
    NiaInnerTransitionCollection,
    load_nia_inner_transition_dataset,
    try_build_nia_inner_transition,
    write_nia_inner_transition_export,
)


SOURCE_MAA_BASELINE_EXACT = "maa_baseline_exact"


def _candidate_set_provenance(
    row: Mapping[str, object],
) -> tuple[str, bool]:
    """Classify legacy Maa rows without claiming a unified action set.

    Older reports predate ``candidate_set_kind``.  Their exact sidecar rows
    were card-only, while the new sidecar can also emit drink-only and
    end-turn-only rows.  Mixed/unknown shapes stay explicitly unready rather
    than being guessed into a unified legal set.
    """

    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        raw_kind = metadata.get("candidate_set_kind")
        if raw_kind in {
            CANDIDATE_SET_KIND_CARD_ONLY,
            CANDIDATE_SET_KIND_DRINK_ONLY,
            CANDIDATE_SET_KIND_END_TURN_ONLY,
            CANDIDATE_SET_KIND_UNKNOWN,
            CANDIDATE_SET_KIND_UNIFIED,
        }:
            return raw_kind, metadata.get(
                "full_rl_policy_ready", raw_kind == CANDIDATE_SET_KIND_UNIFIED
            ) is True
    candidates = row.get("legal_candidates")
    kinds: set[str] = set()
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                kind = candidate.get("kind")
                if kind in {"play", "card", "use-hand", "use_hand"}:
                    kinds.add(CANDIDATE_SET_KIND_CARD_ONLY)
                elif kind in {"drink", "use-drink", "use_drink"}:
                    kinds.add(CANDIDATE_SET_KIND_DRINK_ONLY)
                elif kind in {"end_turn", "turn-end", "turn_end"}:
                    kinds.add(CANDIDATE_SET_KIND_END_TURN_ONLY)
                else:
                    kinds.add(CANDIDATE_SET_KIND_UNKNOWN)
            else:
                kinds.add(CANDIDATE_SET_KIND_UNKNOWN)
    if len(kinds) == 1:
        return next(iter(kinds)), False
    return CANDIDATE_SET_KIND_UNKNOWN, False


def _baseline_outcomes(report: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    result = report.get("result")
    steps = result.get("steps") if isinstance(result, Mapping) else None
    if not isinstance(steps, list):
        return ()
    values: list[Mapping[str, object]] = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        outcome = step.get("outcome")
        if not isinstance(outcome, Mapping):
            continue
        inner = outcome.get("inner_imitation")
        if isinstance(inner, Mapping):
            decision = inner.get("decision")
            baseline = (
                decision.get("baseline_result")
                if isinstance(decision, Mapping)
                else None
            )
            if isinstance(baseline, Mapping):
                values.append(
                    {
                        **dict(baseline),
                        "exam_save_identity": outcome.get("exam_save_identity"),
                    }
                )
                continue
        policy = outcome.get("execution_policy")
        if (
            isinstance(policy, Mapping)
            and policy.get("mode") == "maa-completion-baseline"
        ):
            values.append(outcome)
    return tuple(values)


def extract_maa_baseline_inner_transitions(
    report: Mapping[str, object],
) -> tuple[NiaInnerTransition, ...]:
    acceptance = report.get("completion_acceptance")
    if (
        report.get("status") != "completed"
        or report.get("authentic_full_run_completed") is not True
        or not isinstance(acceptance, Mapping)
        or acceptance.get("accepted") is not True
    ):
        return ()
    strategy = report.get("strategy_journal")
    source_id = strategy.get("run_id") if isinstance(strategy, Mapping) else None
    if not isinstance(source_id, str) or not source_id:
        return ()
    request = report.get("request")
    produce_id = request.get("produce_id") if isinstance(request, Mapping) else None
    idol_card_id = request.get("idol_card_id") if isinstance(request, Mapping) else None

    rows: list[NiaInnerTransition] = []
    sequence = 0
    for outcome in _baseline_outcomes(report):
        identity = outcome.get("exam_save_identity")
        transitions = outcome.get("exact_transitions")
        if not isinstance(identity, Mapping) or not isinstance(transitions, list):
            continue
        step_context_id = identity.get("step_context_id")
        session_transition_id = identity.get("session_transition_id")
        if (
            not isinstance(step_context_id, str)
            or not step_context_id
            or not isinstance(session_transition_id, str)
            or not session_transition_id
        ):
            sequence += len(transitions)
            continue
        for raw in transitions:
            step = sequence
            sequence += 1
            if not isinstance(raw, Mapping):
                continue
            metadata = raw.get("metadata")
            metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
            candidate_set_kind, policy_ready = _candidate_set_provenance(raw)
            metadata.update(
                {
                    "step_context_id": step_context_id,
                    "session_transition_id": session_transition_id,
                    "produce_id": produce_id,
                    "idol_card_id": idol_card_id,
                    "candidate_set_kind": candidate_set_kind,
                    "full_rl_policy_ready": policy_ready,
                    "transition_scope": "exact-dynamics",
                }
            )
            transition, _rejection = try_build_nia_inner_transition(
                {
                    **dict(raw),
                    "source": SOURCE_MAA_BASELINE_EXACT,
                    "source_id": source_id,
                    "step": step,
                    "metadata": metadata,
                },
                source=SOURCE_MAA_BASELINE_EXACT,
                source_id=source_id,
                step=step,
            )
            if transition is not None:
                rows.append(transition)
    return tuple(rows)


def append_maa_baseline_inner_transitions(
    report_path: str | Path,
    *,
    output: str | Path = DEFAULT_OUTPUT,
    readiness_output: str | Path = DEFAULT_READINESS_OUTPUT,
) -> dict[str, object]:
    source = Path(report_path).resolve()
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("completed report must be an object")
    added = extract_maa_baseline_inner_transitions(raw)
    target = Path(output).resolve()
    existing: tuple[NiaInnerTransition, ...] = ()
    if target.is_file() and target.stat().st_size > 0:
        existing = load_nia_inner_transition_dataset(target)

    by_key: dict[tuple[str, str, int], NiaInnerTransition] = {
        (value.source, value.source_id, value.step): value for value in existing
    }
    appended_count = 0
    for value in added:
        key = (value.source, value.source_id, value.step)
        prior = by_key.get(key)
        if prior is not None:
            if prior != value:
                raise ValueError("same Maa transition boundary changed across append")
            continue
        by_key[key] = value
        appended_count += 1
    values = tuple(
        sorted(by_key.values(), key=lambda row: (row.source, row.source_id, row.step))
    )
    if not values:
        return {
            "transition_count": 0,
            "appended_transition_count": 0,
            "training_ready": False,
        }
    manifest = write_nia_inner_transition_export(
        target,
        NiaInnerTransitionCollection(values),
        allow_empty=False,
    )
    readiness = audit_nia_inner_training_readiness(values)
    readiness_target = Path(readiness_output).resolve()
    readiness_target.parent.mkdir(parents=True, exist_ok=True)
    readiness_target.write_text(
        json.dumps(readiness, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        **manifest,
        "appended_transition_count": appended_count,
        "training_ready": readiness["training_ready"],
        "readiness_output": str(readiness_target),
    }


__all__ = [
    "SOURCE_MAA_BASELINE_EXACT",
    "append_maa_baseline_inner_transitions",
    "extract_maa_baseline_inner_transitions",
]
