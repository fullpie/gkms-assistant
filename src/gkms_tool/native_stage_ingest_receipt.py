"""Materialize run-scoped native stages from completed-run evidence.

The output is a candidate artifact only.  It never appends to a frozen
dataset, changes the active policy, or starts a training process.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import uuid
from typing import Any, Final

from .completed_run_evidence import REPORT_SCHEMA as COMPLETED_REPORT_SCHEMA
from .native_runtime_stage_capture import build_native_runtime_stage_rows
from .training_artifact_io import canonical_json_bytes


REPORT_SCHEMA: Final = "gkms.completed-run-native-stage-candidate.v1"
RECEIPT_SCHEMA: Final = "gkms.completed-run-native-stage-ingest-receipt.v1"
OUTPUT_DIRECTORY_NAME: Final = "native_stage_candidate"
TRANSITIONS_NAME: Final = "inner_transitions.jsonl"
REPORT_NAME: Final = "report.json"
RECEIPT_NAME: Final = "receipt.json"

StageBuilder = Callable[..., tuple[Sequence[Mapping[str, Any]], Mapping[str, Any]]]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return _sha256_bytes(payload)


def _strict_completed_report(path: Path) -> tuple[dict[str, Any], bytes, str]:
    path = path.resolve(strict=True)
    source = path.read_bytes()
    payload = json.loads(source.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("completed-run evidence root must be an object")
    if payload.get("schema") != COMPLETED_REPORT_SCHEMA:
        raise ValueError("completed-run evidence schema mismatch")
    result = payload.get("result")
    if not isinstance(result, Mapping) or result.get("status") != "completed":
        raise ValueError("completed-run evidence has no completed result")
    run = payload.get("run")
    if not isinstance(run, Mapping):
        raise ValueError("completed-run evidence has no run identity")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("completed-run evidence run_id is invalid")
    return payload, source, run_id.strip()


def _capture_steps(report: Mapping[str, Any]) -> tuple[tuple[int, Mapping[str, Any]], ...]:
    result = report["result"]
    assert isinstance(result, Mapping)
    steps = result.get("steps")
    if not isinstance(steps, list):
        raise ValueError("completed-run evidence result.steps must be a list")
    from .native_cultivation_evidence import extract_native_stage_captures
    # A verified segment aggregation keeps original partial steps for audit,
    # while this explicit list supplies their conservative coalesced ranges.
    if "native_runtime_stage_captures" in result:
        values = result["native_runtime_stage_captures"]
        if not isinstance(values, list) or any(not isinstance(value, Mapping) for value in values):
            raise ValueError("native runtime stage captures must be an object array")
        return tuple(enumerate(values))
    captures: list[tuple[int, Mapping[str, Any]]] = []
    seen = set()
    for entry in extract_native_stage_captures({key: val for key, val in result.items() if key != "steps"}):
        digest = _sha256_bytes(canonical_json_bytes(entry["capture"]))
        seen.add(digest)
        captures.append((0, entry["capture"]))
    for fallback_index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            raise ValueError("completed-run evidence contains a malformed step")
        outcome = step.get("outcome")
        if not isinstance(outcome, Mapping):
            continue
        step_index = step.get("index")
        for entry in extract_native_stage_captures(outcome):
            capture = entry["capture"]
            digest = _sha256_bytes(canonical_json_bytes(capture))
            if digest in seen:
                continue
            seen.add(digest)
            captures.append((step_index if type(step_index) is int else fallback_index, capture))
    return tuple(captures)


def ingest_completed_run_native_stages(
    completed_report_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    stage_builder: StageBuilder = build_native_runtime_stage_rows,
) -> Mapping[str, Any]:
    """Validate every captured native stage and atomically write a receipt.

    Accepted rows retain the existing native-stage validator's state/action/
    legal-set contracts.  If any captured stage fails, its error is recorded
    and ``dataset_ready`` remains false; valid rows are still materialized as
    run-scoped diagnostic candidates and are never promoted automatically.
    """

    source_path = Path(completed_report_path)
    report, source_bytes, run_id = _strict_completed_report(source_path)
    if output_dir is None:
        output = source_path.resolve().parent / OUTPUT_DIRECTORY_NAME
    else:
        output = Path(output_dir).resolve()
    if not callable(stage_builder):
        raise TypeError("stage_builder must be callable")

    accepted_rows: list[dict[str, Any]] = []
    stage_reports: list[dict[str, Any]] = []
    captures = _capture_steps(report)
    from .native_cultivation_evidence import extract_native_stage_captures
    aggregation = report.get("native_stage_aggregation", report["result"].get("native_stage_aggregation"))
    evidence_blockers = []
    if aggregation is not None:
        if not isinstance(aggregation, Mapping) or aggregation.get("run_id") != run_id:
            raise ValueError("native stage aggregation run identity differs")
        evidence_blockers.extend(str(value) for value in aggregation.get("blockers", ()))
        if aggregation.get("run_completed") is not True:
            evidence_blockers.append("native-aggregation-has-no-completed-run")
    if "native_runtime_stage_captures" not in report["result"]:
        for index, step in enumerate(report["result"]["steps"]):
            if step.get("page") == "exam" and not extract_native_stage_captures(step.get("outcome", {})):
                evidence_blockers.append(f"native-exam-step-has-no-capture:{index}")
    for ordinal, (step_index, capture) in enumerate(captures):
        try:
            rows, raw_report = stage_builder(capture, source_run_id=run_id)
            if not isinstance(raw_report, Mapping):
                raise TypeError("native stage builder returned no report")
            if raw_report.get("status") != "accepted":
                raise ValueError("native stage builder did not accept the complete stage")
            if raw_report.get("source_run_id", run_id) != run_id:
                raise ValueError("native stage builder report belongs to another run")
            normalized_rows: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, Mapping):
                    raise TypeError("native stage builder returned a malformed row")
                normalized_rows.append(dict(row))
            if not normalized_rows:
                raise ValueError("native stage builder returned no transitions")
            accepted_rows.extend(normalized_rows)
            stage_reports.append(
                {
                    "ordinal": ordinal,
                    "step_index": step_index,
                    "status": "accepted",
                    "transition_count": len(normalized_rows),
                    "report": dict(raw_report),
                }
            )
        except Exception as error:
            stage_reports.append(
                {
                    "ordinal": ordinal,
                    "step_index": step_index,
                    "status": "rejected",
                    "transition_count": 0,
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    transitions_path = output / TRANSITIONS_NAME
    report_path = output / REPORT_NAME
    receipt_path = output / RECEIPT_NAME
    transitions_bytes = b"".join(
        canonical_json_bytes(row) + b"\n" for row in accepted_rows
    )
    transitions_sha256 = _atomic_write(transitions_path, transitions_bytes)
    accepted_stage_count = sum(
        value["status"] == "accepted" for value in stage_reports
    )
    rejected_stage_count = len(stage_reports) - accepted_stage_count
    dataset_ready = bool(captures) and rejected_stage_count == 0 and bool(accepted_rows) and not evidence_blockers
    candidate_report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "run_id": run_id,
        "dataset_ready": dataset_ready,
        "blockers": evidence_blockers,
        "input": {
            "completed_run_evidence_path": str(source_path.resolve()),
            "completed_run_evidence_sha256": _sha256_bytes(source_bytes),
        },
        "counts": {
            "capture_count": len(captures),
            "accepted_stage_count": accepted_stage_count,
            "rejected_stage_count": rejected_stage_count,
            "accepted_transition_count": len(accepted_rows),
        },
        "stages": stage_reports,
        "output": {
            "inner_transitions_path": str(transitions_path.resolve()),
            "inner_transitions_sha256": transitions_sha256,
        },
        "contract": {
            "source": "completed-run-evidence-native-runtime-stage-captures",
            "simulator_used": False,
            "frozen_dataset_modified": False,
            "active_policy_modified": False,
            "training_triggered": False,
        },
    }
    report_bytes = _json_bytes(candidate_report)
    report_sha256 = _atomic_write(report_path, report_bytes)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "run_id": run_id,
        "dataset_ready": dataset_ready,
        "blockers": evidence_blockers,
        "completed_run_evidence_sha256": _sha256_bytes(source_bytes),
        "candidate_report_path": str(report_path.resolve()),
        "candidate_report_sha256": report_sha256,
        "inner_transitions_path": str(transitions_path.resolve()),
        "inner_transitions_sha256": transitions_sha256,
        "capture_count": len(captures),
        "accepted_stage_count": accepted_stage_count,
        "rejected_stage_count": rejected_stage_count,
        "accepted_transition_count": len(accepted_rows),
        "frozen_dataset_modified": False,
        "active_policy_modified": False,
        "training_triggered": False,
    }
    _atomic_write(receipt_path, _json_bytes(receipt))
    return receipt


__all__ = [
    "OUTPUT_DIRECTORY_NAME",
    "RECEIPT_NAME",
    "RECEIPT_SCHEMA",
    "REPORT_NAME",
    "REPORT_SCHEMA",
    "TRANSITIONS_NAME",
    "ingest_completed_run_native_stages",
]
