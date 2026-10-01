"""Shadow-only Outer BC scoring for the formal cultivation loop."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .behavior_cloning import (
    OUTER_REPORT_SCHEMA,
    build_outer_context_tokens,
    load_model_artifact,
    score_pointer_candidates,
)
from .canonical_training_labels import ARCHETYPE_BY_EFFECT


SHADOW_SCHEMA: Final = "gkms.outer-bc-shadow.v1"
LIVE_AUDIT_SCHEMA: Final = "gkms.outer-bc-live-shadow-audit.v1"
FULL_CULTIVATION_SHADOW_SCHEMA: Final = "gkms.full-cultivation-bc-shadow.v1"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_PATH: Final = (
    PROJECT_ROOT / "var" / "models" / "behavior_cloning_v1" / "outer_bc_model.npz"
)
DEFAULT_REPORT_PATH: Final = (
    PROJECT_ROOT / "var" / "models" / "behavior_cloning_v1" / "outer_bc_report.json"
)
DEFAULT_POLICY_BUNDLE_PATH: Final = (
    PROJECT_ROOT
    / "var"
    / "models"
    / "policy_bundles"
    / "behavior_cloning_v0"
    / "manifest.json"
)


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _value(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _snapshot_state(snapshot: object) -> dict[str, object]:
    mapping = {
        "stamina": "stamina",
        "max_stamina": "max_stamina",
        # Frozen Outer BC v1 learned the canonical-label key
        # ``produce_points``.  Keep serving on that same token; changing only
        # the live spelling to singular hashes it as an unseen feature.
        "produce_points": "produce_points",
        "vocal": "vocal",
        "dance": "dance",
        "visual": "visual",
        "week": "latest_week_marker",
        "last_completed_week": "last_completed_week",
        "vote_count": "vote_count",
        "drink_count": "drink_count",
    }
    result: dict[str, object] = {}
    for target, source in mapping.items():
        value = _value(snapshot, source)
        if isinstance(value, (int, float, str, bool)) and not (
            isinstance(value, str) and not value
        ):
            result[target] = value
    return result


@dataclass(frozen=True, slots=True)
class OuterBCShadowEvaluator:
    model_path: Path
    report_path: Path
    model_sha256: str
    report_sha256: str
    produce_id: str
    idol_card_id: str
    character_id: str | None
    plan_type: str | None
    exam_effect_type: str | None
    archetype: str | None

    @classmethod
    def load(
        cls,
        *,
        produce_id: str,
        idol_card_id: str,
        character_id: str | None,
        plan_type: str | None,
        exam_effect_type: str | None,
        model_path: Path = DEFAULT_MODEL_PATH,
        report_path: Path = DEFAULT_REPORT_PATH,
        bundle_path: Path | None = DEFAULT_POLICY_BUNDLE_PATH,
    ) -> "OuterBCShadowEvaluator":
        model_path = Path(model_path)
        report_path = Path(report_path)
        if (
            bundle_path is not None
            and model_path == DEFAULT_MODEL_PATH
            and report_path == DEFAULT_REPORT_PATH
            and Path(bundle_path).is_file()
        ):
            from .policy_bundle import PolicyBundle

            bundle = PolicyBundle.load(
                None
                if Path(bundle_path) == DEFAULT_POLICY_BUNDLE_PATH
                else Path(bundle_path)
            )
            model_path = bundle.model_path("outer_policy")
            report_path = bundle.report_path("outer_policy")
        if not model_path.is_file() or not report_path.is_file():
            raise FileNotFoundError("Outer BC model/report is missing")
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        if not isinstance(report, Mapping) or report.get("schema") != OUTER_REPORT_SCHEMA:
            raise ValueError("Outer BC report schema mismatch")
        if _value(_value(report, "acceptance"), "passed") is not True:
            raise ValueError("Outer BC report did not pass acceptance")
        expected_model_sha = _value(_value(report, "artifacts"), "model_sha256")
        actual_model_sha = _sha256_file(model_path)
        if expected_model_sha != actual_model_sha:
            raise ValueError("Outer BC model hash mismatch")
        _parameters, metadata, _extras = load_model_artifact(model_path)
        if metadata.get("kind") != "outer-candidate-pointer":
            raise ValueError("Outer BC model kind mismatch")
        archetype = (
            ARCHETYPE_BY_EFFECT.get(exam_effect_type)
            if isinstance(exam_effect_type, str)
            else None
        )
        return cls(
            model_path=model_path,
            report_path=report_path,
            model_sha256=actual_model_sha,
            report_sha256=_sha256_file(report_path),
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            character_id=character_id,
            plan_type=plan_type,
            exam_effect_type=exam_effect_type,
            archetype=archetype,
        )

    def evaluate(
        self,
        *,
        advice: object,
        snapshot: object,
        candidate_ids: Sequence[str],
        formal_action: str,
    ) -> dict[str, object]:
        candidates = tuple(str(value) for value in candidate_ids)
        if (
            not candidates
            or len(set(candidates)) != len(candidates)
            or formal_action not in candidates
        ):
            raise ValueError("Outer BC shadow requires unique candidates and formal member")
        stage = _value(advice, "stage")
        week = _value(advice, "week")
        mode = {
            "produce-004": "nia_pro",
            "produce-005": "nia_master",
        }.get(self.produce_id, "unknown")
        context_tokens = build_outer_context_tokens(
            mode=mode,
            produce_id=self.produce_id,
            plan_type=self.plan_type,
            exam_effect_type=self.exam_effect_type,
            archetype=self.archetype,
            stage=stage,
            idol_card_id=self.idol_card_id,
            character_id=self.character_id,
            step=week,
            source_schema="gkms.nia-training-observation.v1",
            candidate_keys=candidates,
            state_before=_snapshot_state(snapshot),
        )
        probabilities = score_pointer_candidates(
            self.model_path,
            context_tokens=context_tokens,
            candidate_semantics=candidates,
        )
        ranked_positions = sorted(
            range(len(candidates)),
            key=lambda index: (-probabilities[index], candidates[index]),
        )
        top = ranked_positions[0]
        margin = (
            probabilities[top] - probabilities[ranked_positions[1]]
            if len(ranked_positions) > 1
            else probabilities[top]
        )
        formal_index = candidates.index(formal_action)
        return {
            "schema": SHADOW_SCHEMA,
            "status": "scored",
            "applied": False,
            "default_enabled": False,
            "shadow_only": True,
            "model_sha256": self.model_sha256,
            "report_sha256": self.report_sha256,
            "produce_id": self.produce_id,
            "flow": "|".join(
                str(value)
                for value in (
                    self.produce_id,
                    self.plan_type,
                    self.exam_effect_type,
                )
            ),
            "week": week,
            "stage": stage,
            "formal_action": formal_action,
            "formal_action_probability": probabilities[formal_index],
            "model_top_action": candidates[top],
            "model_top_probability": probabilities[top],
            "model_margin": margin,
            "agrees_with_formal": candidates[top] == formal_action,
            "candidate_probabilities": [
                {"candidate_id": candidate, "probability": probability}
                for candidate, probability in zip(candidates, probabilities, strict=True)
            ],
            "candidate_order_owner": "runtime-eligible-actions",
            "input_authority": "formal advisor and LocalSave snapshot",
        }


ShadowProvider = Callable[[object, object, Sequence[str], str], Mapping[str, object]]


def attach_outer_bc_shadow_evidence(
    decision_evidence: Mapping[str, Any],
    *,
    provider: ShadowProvider | None,
    advice: object,
    snapshot: object,
    candidate_ids: Sequence[str],
    formal_action: str,
) -> dict[str, Any]:
    result = dict(decision_evidence)
    if provider is None:
        return result
    try:
        shadow = provider(advice, snapshot, candidate_ids, formal_action)
        if not isinstance(shadow, Mapping):
            raise TypeError("Outer BC shadow provider returned no mapping")
        result["outer_bc_shadow"] = dict(shadow)
    except Exception as error:
        result["outer_bc_shadow"] = {
            "schema": SHADOW_SCHEMA,
            "status": "blocked",
            "applied": False,
            "default_enabled": False,
            "shadow_only": True,
            "reason": f"{type(error).__name__}: {error}",
            "formal_action": formal_action,
            "candidate_ids": list(candidate_ids),
        }
    return result


def build_full_cultivation_shadow_report(
    report: Mapping[str, Any],
    *,
    run_id: str,
    exact_ledger_path: Path | None = None,
    exact_completion: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Join one completed run's Outer and Exact BC evidence without scoring.

    The source report and append-only Exact ledger remain authoritative.  Rows
    from every other run are ignored, and this projection never returns an
    action or changes the completed cultivation result.
    """

    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("full cultivation shadow report requires run_id")
    run_id = run_id.strip()
    result = report.get("result")
    steps = result.get("steps") if isinstance(result, Mapping) else None
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
        steps = ()

    outer_expected = 0
    outer_records: list[dict[str, object]] = []
    exact_expected = 0
    for fallback_index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        outcome = step.get("outcome")
        if not isinstance(outcome, Mapping):
            continue
        transitions = outcome.get("exact_transitions")
        if isinstance(transitions, Sequence) and not isinstance(
            transitions, (str, bytes)
        ):
            exact_expected += sum(isinstance(row, Mapping) for row in transitions)
        evidence = outcome.get("decision_evidence")
        if not isinstance(evidence, Mapping):
            continue
        shadow = evidence.get("outer_bc_shadow")
        if evidence.get("kind") == "outer-action" or isinstance(shadow, Mapping):
            outer_expected += 1
        if not isinstance(shadow, Mapping):
            continue
        step_index = step.get("index")
        if not isinstance(step_index, int) or isinstance(step_index, bool):
            step_index = fallback_index
        outer_records.append({"step_index": step_index, **dict(shadow)})

    outer_status_counts: Counter[str] = Counter(
        str(row.get("status") or "missing") for row in outer_records
    )
    outer_agreement_counts: Counter[str] = Counter()
    outer_applied = []
    outer_blockers: list[str] = []
    for row in outer_records:
        agrees = row.get("agrees_with_formal")
        if agrees is True:
            outer_agreement_counts["agree"] += 1
        elif agrees is False:
            outer_agreement_counts["disagree"] += 1
        else:
            outer_agreement_counts["not_scored"] += 1
        if row.get("applied") is True:
            outer_applied.append(row.get("step_index"))
        if row.get("status") == "blocked":
            outer_blockers.append(
                f"outer:{row.get('step_index')}:{row.get('reason') or 'blocked'}"
            )

    if exact_ledger_path is None:
        from .exact_exam_bc_sidecar import DEFAULT_LEDGER_PATH

        exact_ledger_path = DEFAULT_LEDGER_PATH
    exact_rows_by_id: dict[str, dict[str, Any]] = {}
    ledger_read_error: str | None = None
    path = Path(exact_ledger_path)
    from .exact_exam_bc_sidecar import EVIDENCE_SCHEMA

    try:
        stream = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        stream = None
    except OSError as error:
        stream = None
        ledger_read_error = f"{type(error).__name__}: {error}"
    if stream is not None:
        with stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    not isinstance(row, Mapping)
                    or row.get("schema") != EVIDENCE_SCHEMA
                ):
                    continue
                source = row.get("source")
                if not isinstance(source, Mapping) or source.get("run_id") != run_id:
                    continue
                record_id = row.get("record_id")
                if not isinstance(record_id, str) or not record_id:
                    continue
                exact_rows_by_id.setdefault(record_id, dict(row))
    exact_rows = tuple(exact_rows_by_id.values())
    exact_status_counts: Counter[str] = Counter(
        str(row.get("status") or "missing") for row in exact_rows
    )
    exact_agreement_counts: Counter[str] = Counter()
    exact_applied: list[str] = []
    exact_blockers: list[str] = []
    for row in exact_rows:
        model = row.get("model")
        agrees = model.get("agrees_with_formal") if isinstance(model, Mapping) else None
        if agrees is True:
            exact_agreement_counts["agree"] += 1
        elif agrees is False:
            exact_agreement_counts["disagree"] += 1
        else:
            exact_agreement_counts["not_scored"] += 1
        if row.get("applied") is True:
            exact_applied.append(str(row.get("record_id")))
        if row.get("status") == "blocked":
            exact_blockers.append(
                f"exact:{row.get('record_id')}:{row.get('reason') or 'blocked'}"
            )

    completion = dict(exact_completion or {})
    exact_source = completion.get("diagnostic_source")
    native_expected = completion.get("expected_transition_count")
    if (
        exact_source == "native-runtime-stage"
        and isinstance(native_expected, int)
        and not isinstance(native_expected, bool)
        and native_expected >= 0
    ):
        # Native stage batches cover every official card, drink, and END_TURN
        # action.  Maa's outcome list is only the legacy card-settled subset
        # and must not remain the denominator when the native spine owns this
        # run's Exact shadow.
        exact_expected = native_expected
    fence_complete = completion.get("complete") is not False
    worker_errors = completion.get("worker_error_count")
    if not isinstance(worker_errors, int) or isinstance(worker_errors, bool):
        worker_errors = 0
    worker_clean = worker_errors == 0
    outer_coverage_complete = len(outer_records) == outer_expected
    exact_coverage_complete = len(exact_rows) == exact_expected
    outer_scoring_complete = outer_status_counts.get("scored", 0) == outer_expected
    exact_scoring_complete = exact_status_counts.get("scored", 0) == exact_expected
    blockers = [*outer_blockers, *exact_blockers]
    if not outer_coverage_complete:
        blockers.append(
            f"outer-evidence-incomplete:{len(outer_records)}/{outer_expected}"
        )
    if not exact_coverage_complete:
        blockers.append(f"exact-evidence-incomplete:{len(exact_rows)}/{exact_expected}")
    if not fence_complete:
        blockers.append("exact-sidecar-fence-incomplete")
    if worker_errors:
        blockers.append(f"exact-sidecar-worker-errors:{worker_errors}")
    if ledger_read_error is not None:
        blockers.append(f"exact-ledger-read-error:{ledger_read_error}")
    if outer_applied:
        blockers.append("outer-shadow-applied")
    if exact_applied:
        blockers.append("exact-shadow-applied")

    bundle_ids = sorted(
        {
            str(row["bundle_id"])
            for row in exact_rows
            if isinstance(row.get("bundle_id"), str) and row.get("bundle_id")
        }
    )
    return {
        "schema": FULL_CULTIVATION_SHADOW_SCHEMA,
        "run_id": run_id,
        "diagnostic_only": True,
        "applied": False,
        "formal_result_unchanged": True,
        "bundle_ids": bundle_ids,
        "outer": {
            "expected_decision_count": outer_expected,
            "record_count": len(outer_records),
            "status_counts": dict(sorted(outer_status_counts.items())),
            "agreement_counts": dict(sorted(outer_agreement_counts.items())),
            "blocker_count": len(outer_blockers),
            "applied_true_steps": outer_applied,
        },
        "exact": {
            "source": exact_source or "maa-exact-transition",
            "expected_transition_count": exact_expected,
            "record_count": len(exact_rows),
            "status_counts": dict(sorted(exact_status_counts.items())),
            "agreement_counts": dict(sorted(exact_agreement_counts.items())),
            "blocker_count": len(exact_blockers),
            "applied_true_record_ids": exact_applied,
            "ledger_path": str(path.resolve()),
            "completion_fence": completion,
        },
        "completeness": {
            "outer_complete": outer_coverage_complete
            and outer_scoring_complete
            and not outer_applied,
            "exact_complete": exact_coverage_complete
            and exact_scoring_complete
            and fence_complete
            and worker_clean
            and not exact_applied,
            "complete": outer_coverage_complete
            and outer_scoring_complete
            and exact_coverage_complete
            and exact_scoring_complete
            and fence_complete
            and worker_clean
            and not outer_applied
            and not exact_applied,
        },
        "blockers": blockers,
    }


def audit_live_shadow_report(report_path: Path) -> dict[str, object]:
    """Audit a completed live report without replaying or controlling the game."""

    report_path = Path(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    if not isinstance(report, Mapping):
        raise ValueError("Live report root must be a mapping")
    result = report.get("result")
    steps = result.get("steps") if isinstance(result, Mapping) else None
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
        raise ValueError("Live report result.steps must be a sequence")

    status_counts: Counter[str] = Counter()
    agreement_counts: Counter[str] = Counter()
    model_hashes: set[str] = set()
    model_report_hashes: set[str] = set()
    formal_binding_mismatches: list[dict[str, object]] = []
    applied_true_steps: list[int] = []
    schema_mismatch_steps: list[int] = []
    decisions: list[dict[str, object]] = []

    for fallback_index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        outcome = step.get("outcome")
        evidence = (
            outcome.get("decision_evidence")
            if isinstance(outcome, Mapping)
            else None
        )
        shadow = (
            evidence.get("outer_bc_shadow")
            if isinstance(evidence, Mapping)
            else None
        )
        if not isinstance(shadow, Mapping):
            continue
        step_index = step.get("index")
        if not isinstance(step_index, int) or isinstance(step_index, bool):
            step_index = fallback_index
        status = str(shadow.get("status") or "missing")
        status_counts[status] += 1
        if shadow.get("schema") != SHADOW_SCHEMA:
            schema_mismatch_steps.append(step_index)
        if shadow.get("applied") is True:
            applied_true_steps.append(step_index)
        agrees = shadow.get("agrees_with_formal")
        if agrees is True:
            agreement_counts["agree"] += 1
        elif agrees is False:
            agreement_counts["disagree"] += 1
        else:
            agreement_counts["not_scored"] += 1
        model_sha = shadow.get("model_sha256")
        if isinstance(model_sha, str) and model_sha:
            model_hashes.add(model_sha)
        model_report_sha = shadow.get("report_sha256")
        if isinstance(model_report_sha, str) and model_report_sha:
            model_report_hashes.add(model_report_sha)

        formal_action = shadow.get("formal_action")
        target = step.get("target")
        chosen = evidence.get("chosen") if isinstance(evidence, Mapping) else None
        if target != formal_action or chosen != formal_action:
            formal_binding_mismatches.append(
                {
                    "step_index": step_index,
                    "target": target,
                    "chosen": chosen,
                    "formal_action": formal_action,
                }
            )
        decisions.append(
            {
                "step_index": step_index,
                "week": shadow.get("week"),
                "stage": shadow.get("stage"),
                "formal_action": formal_action,
                "model_top_action": shadow.get("model_top_action"),
                "model_top_probability": shadow.get("model_top_probability"),
                "agrees_with_formal": agrees,
                "status": status,
                "applied": shadow.get("applied"),
            }
        )

    shadow_count = len(decisions)
    accepted = all(
        (
            report.get("status") == "completed",
            report.get("authentic_full_run_completed") is True,
            isinstance(report.get("completion_acceptance"), Mapping)
            and report["completion_acceptance"].get("accepted") is True,
            shadow_count > 0,
            status_counts.get("scored", 0) == shadow_count,
            not formal_binding_mismatches,
            not applied_true_steps,
            not schema_mismatch_steps,
            len(model_hashes) == 1,
            len(model_report_hashes) == 1,
        )
    )
    scored_count = status_counts.get("scored", 0)
    agree_count = agreement_counts.get("agree", 0)
    audit = {
        "schema": LIVE_AUDIT_SCHEMA,
        "input": {
            "path": str(report_path.resolve()),
            "sha256": _sha256_file(report_path),
        },
        "run": {
            "status": report.get("status"),
            "authentic_full_run_completed": report.get(
                "authentic_full_run_completed"
            ),
            "completion_accepted": (
                report["completion_acceptance"].get("accepted")
                if isinstance(report.get("completion_acceptance"), Mapping)
                else False
            ),
            "step_count": len(steps),
        },
        "shadow": {
            "decision_count": shadow_count,
            "status_counts": dict(sorted(status_counts.items())),
            "agreement_counts": dict(sorted(agreement_counts.items())),
            "agreement_rate": (
                agree_count / scored_count if scored_count else None
            ),
            "model_sha256": sorted(model_hashes),
            "model_report_sha256": sorted(model_report_hashes),
            "formal_binding_mismatches": formal_binding_mismatches,
            "applied_true_steps": applied_true_steps,
            "schema_mismatch_steps": schema_mismatch_steps,
            "decisions": decisions,
        },
        "acceptance": {
            "passed": accepted,
            "contract": (
                "completed authentic run; at least one fully scored shadow "
                "decision; one model/report identity; formal target preserved; "
                "shadow never applied"
            ),
        },
    }
    joint = report.get("full_cultivation_shadow")
    if isinstance(joint, Mapping):
        # Additive projection only: the legacy Outer counters and acceptance
        # contract above intentionally retain their exact old semantics.
        audit["full_cultivation_shadow"] = dict(joint)
    return audit


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit Outer BC live shadow evidence")
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    audit = audit_live_shadow_report(args.report)
    rendered = json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if audit["acceptance"]["passed"] is True else 2


__all__ = [
    "DEFAULT_MODEL_PATH",
    "DEFAULT_POLICY_BUNDLE_PATH",
    "DEFAULT_REPORT_PATH",
    "FULL_CULTIVATION_SHADOW_SCHEMA",
    "LIVE_AUDIT_SCHEMA",
    "OuterBCShadowEvaluator",
    "SHADOW_SCHEMA",
    "attach_outer_bc_shadow_evidence",
    "audit_live_shadow_report",
    "build_full_cultivation_shadow_report",
]


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main())
