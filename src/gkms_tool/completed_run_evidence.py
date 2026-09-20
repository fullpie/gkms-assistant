"""Durable, replay-safe evidence finalization for one completed live run.

This module owns no game input and no learned-policy activation.  It receives
the already-completed formal autopilot result, preserves its full step ledger,
then reuses the existing strategy-journal, Exact BC completion-fence, and
full-cultivation shadow APIs.  Dataset ingestion and training are explicitly
outside this boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
import os
from pathlib import Path
import uuid
from typing import Any, Final

from .exact_exam_bc_sidecar import flush_exact_exam_bc_diagnostics
from .initial_regular_autopilot import InitialRegularAutopilotResult
from .nia_live_unattended import (
    NiaLiveUnattendedRequest,
    finalize_nia_strategy_telemetry_from_result,
)
from .outer_bc_shadow import (
    FULL_CULTIVATION_SHADOW_SCHEMA,
    build_full_cultivation_shadow_report,
)
from .produce_outer_local_save import ProduceOuterLocalSaveSnapshot
from .run_identity import DEFAULT_RUN_ROOT, RunIdentity, paths_for


REPORT_SCHEMA: Final = "gkms.completed-run-evidence.v1"
RECEIPT_SCHEMA: Final = "gkms.completed-run-evidence-receipt.v1"
REPORT_NAME: Final = "completed_run_evidence.json"
RECEIPT_NAME: Final = "completed_run_evidence_receipt.json"

StrategyFinalizer = Callable[..., Mapping[str, Any]]
ExactCompletionFlusher = Callable[..., Mapping[str, Any]]
ShadowBuilder = Callable[..., Mapping[str, Any]]


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(value)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return hashlib.sha256(payload).hexdigest()


def _error_mapping(kind: str, error: Exception) -> dict[str, object]:
    return {
        "status": "error",
        "complete": False,
        "kind": kind,
        "error": f"{type(error).__name__}: {error}",
    }


def finalize_completed_run_evidence(
    result: InitialRegularAutopilotResult,
    *,
    run: RunIdentity,
    request: NiaLiveUnattendedRequest,
    final_snapshot: ProduceOuterLocalSaveSnapshot | None = None,
    run_root: str | Path = DEFAULT_RUN_ROOT,
    exact_timeout_seconds: float = 30.0,
    strategy_finalizer: StrategyFinalizer = (
        finalize_nia_strategy_telemetry_from_result
    ),
    exact_completion_flusher: ExactCompletionFlusher = (
        flush_exact_exam_bc_diagnostics
    ),
    shadow_builder: ShadowBuilder = build_full_cultivation_shadow_report,
) -> Mapping[str, Any]:
    """Persist one completed formal result and return its durable receipt.

    Re-running with the same completed result is safe: JSON artifacts are
    atomically replaced, the strategy finalizer observes the existing terminal
    row, and the Exact sidecar fence only waits for already-submitted batches.
    No frozen dataset, model, active-policy pointer, or training process is
    touched.
    """

    if not isinstance(result, InitialRegularAutopilotResult):
        raise TypeError("result must be InitialRegularAutopilotResult")
    if not result.completed:
        raise ValueError("completed-run evidence requires a completed result")
    if not isinstance(run, RunIdentity):
        raise TypeError("run must be RunIdentity")
    run.validate()
    if not isinstance(request, NiaLiveUnattendedRequest):
        raise TypeError("request must be NiaLiveUnattendedRequest")
    if request.produce_id != run.produce_id:
        raise ValueError("request/run produce_id mismatch")
    if request.idol_card_id != run.idol_card_id:
        raise ValueError("request/run idol_card_id mismatch")
    if request.expected_run_id not in (None, run.run_id):
        raise ValueError("request/run run_id mismatch")
    if isinstance(exact_timeout_seconds, bool) or not isinstance(
        exact_timeout_seconds, (int, float)
    ) or exact_timeout_seconds < 0:
        raise ValueError("exact_timeout_seconds must be non-negative")
    for dependency in (
        strategy_finalizer,
        exact_completion_flusher,
        shadow_builder,
    ):
        if not callable(dependency):
            raise TypeError("finalizer dependencies must be callable")

    run_paths = paths_for(run, root=Path(run_root))
    report_path = run_paths.directory / REPORT_NAME
    receipt_path = run_paths.directory / RECEIPT_NAME
    result_payload = result.to_dict()
    native_aggregation = None
    segment_directory = run_paths.directory / "native_segments"
    if segment_directory.is_dir() and any(segment_directory.glob("*.json")):
        from .native_cultivation_evidence import MERGED_SCHEMA, merge_native_cultivation_segments
        try:
            native_aggregation = merge_native_cultivation_segments(run.run_id, run_root=Path(run_root))
        except Exception as error:
            # Preserve the completed gameplay result, but never fall back to
            # only the last (possibly partial) stage after a failed merge.
            native_aggregation = {"schema": MERGED_SCHEMA, "run_id": run.run_id,
                "run_completed": result.completed, "native_runtime_stage_captures": [],
                "blockers": [f"native-segment-merge-failed:{type(error).__name__}:{error}"],
                "dataset_ready": False}
        result_payload["native_runtime_stage_captures"] = native_aggregation["native_runtime_stage_captures"]
        result_payload["native_stage_aggregation"] = native_aggregation

    try:
        strategy = dict(
            strategy_finalizer(
                request,
                run_id=run.run_id,
                result=result,
                final_snapshot=final_snapshot,
            )
        )
    except Exception as error:
        strategy = _error_mapping("strategy-journal-finalize", error)

    try:
        exact_completion = dict(
            exact_completion_flusher(
                run.run_id,
                timeout_seconds=float(exact_timeout_seconds),
            )
        )
    except Exception as error:
        exact_completion = _error_mapping("exact-sidecar-completion-fence", error)

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "run": run.to_dict(),
        "request": request.to_dict(),
        "result": result_payload,
        "strategy_journal": strategy,
        "exact_completion": exact_completion,
        "formal_result_unchanged": True,
        "dataset_ingest_performed": False,
        "training_triggered": False,
    }
    try:
        full_shadow = dict(
            shadow_builder(
                report,
                run_id=run.run_id,
                exact_completion=exact_completion,
            )
        )
    except Exception as error:
        full_shadow = {
            "schema": FULL_CULTIVATION_SHADOW_SCHEMA,
            "run_id": run.run_id,
            "diagnostic_only": True,
            "applied": False,
            "formal_result_unchanged": True,
            "completeness": {
                "outer_complete": False,
                "exact_complete": False,
                "complete": False,
            },
            "blockers": [
                "aggregation-error:"
                f"{type(error).__name__}: {error}"
            ],
        }
    report["full_cultivation_shadow"] = full_shadow
    report_sha256 = _atomic_json(report_path, report)

    shadow_completeness = full_shadow.get("completeness")
    shadow_complete = bool(
        isinstance(shadow_completeness, Mapping)
        and shadow_completeness.get("complete") is True
    )
    strategy_complete = strategy.get("terminal_written") is True
    exact_complete = exact_completion.get("complete") is True
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "run_id": run.run_id,
        "report_path": str(report_path.resolve()),
        "report_sha256": report_sha256,
        "result_step_count": len(result.steps),
        "strategy_journal_terminal": strategy_complete,
        "exact_completion_fence_complete": exact_complete,
        "full_cultivation_shadow_complete": shadow_complete,
        "evidence_complete": (
            strategy_complete and exact_complete and shadow_complete and native_aggregation is None
        ),
        **({"native_stage_validation_pending": True,
            "native_stage_aggregation_blockers": list(native_aggregation.get("blockers", ())) }
           if native_aggregation is not None else {}),
        "dataset_ingest_performed": False,
        "training_triggered": False,
    }
    _atomic_json(receipt_path, receipt)
    return receipt


__all__ = [
    "REPORT_NAME",
    "REPORT_SCHEMA",
    "RECEIPT_NAME",
    "RECEIPT_SCHEMA",
    "finalize_completed_run_evidence",
]
