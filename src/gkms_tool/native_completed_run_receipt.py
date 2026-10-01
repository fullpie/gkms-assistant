"""Read and verify native-owned completion evidence; never finalize it again."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .run_identity import DEFAULT_RUN_ROOT, load_run
from .training_artifact_io import canonical_json_bytes


def _require(value, reason):
    if not value:
        raise ValueError("Native completed-run receipt: " + reason)


def reuse_native_completed_run_evidence(result, request, run_id, *, run_root=DEFAULT_RUN_ROOT):
    """Verify exact report ownership and reuse all original qualification flags."""
    receipt = getattr(result, "native_completed_run_evidence", None)
    _require(result.completed and isinstance(receipt, Mapping)
             and receipt.get("schema") == "gkms.completed-run-evidence-receipt.v1"
             and receipt.get("run_id") == run_id, "missing or malformed native receipt; no legacy fallback")
    run = load_run(run_id, root=Path(run_root))
    _require(run.produce_id == request.produce_id and run.idol_card_id == request.idol_card_id
             and request.expected_run_id in (None, run_id), "receipt run/mode/idol differs from the GUI request")
    path = Path(receipt.get("report_path", "")).resolve()
    expected_path = (Path(run_root) / run_id / "completed_run_evidence.json").resolve()
    _require(path == expected_path, "report is outside this native run")
    payload = path.read_bytes()
    _require(hashlib.sha256(payload).hexdigest() == receipt.get("report_sha256"),
             "native report changed after the runner returned its receipt")
    report = json.loads(payload)
    _require(isinstance(report, Mapping) and report.get("schema") == "gkms.completed-run-evidence.v1"
             and canonical_json_bytes(report.get("run")) == canonical_json_bytes(run.to_dict()),
             "native report identity differs")
    source_request = report.get("request")
    _require(isinstance(source_request, Mapping) and source_request.get("produce_id") == run.produce_id
             and source_request.get("idol_card_id") == run.idol_card_id
             and source_request.get("expected_run_id") == run_id
             and source_request.get("exam_execution_mode") == "exact",
             "report belongs to a different completion backend or request")
    formal = result.to_dict()
    stored = report.get("result")
    _require(isinstance(stored, Mapping)
             and set(stored) - set(formal) <= {"native_runtime_stage_captures", "native_stage_aggregation"}
             and all(key in stored and canonical_json_bytes(stored[key]) == canonical_json_bytes(value)
                     for key, value in formal.items()), "report formal result/steps/Home differs from the returned native result")
    _require(receipt.get("result_step_count") == len(result.steps)
             and receipt.get("evidence_complete") is False
             and receipt.get("dataset_ingest_performed") is False
             and receipt.get("training_triggered") is False
             and report.get("formal_result_unchanged") is True
             and report.get("dataset_ingest_performed") is False and report.get("training_triggered") is False,
             "native workflow receipt cannot claim data or training admission")
    _require(report.get("strategy_journal") == {"status": "not-used-native", "terminal_written": False}
             and report.get("exact_completion") == {"status": "not-used-native", "complete": False},
             "native completion source was replaced by legacy diagnostics")
    shadow = report.get("full_cultivation_shadow")
    _require(isinstance(shadow, Mapping) and shadow.get("diagnostic_only") is True
             and shadow.get("applied") is False and shadow.get("formal_result_unchanged") is True
             and shadow.get("run_id") == run_id
             and shadow.get("completeness") == {"outer_complete": False, "exact_complete": False, "complete": False}
             and shadow.get("blockers") == ["native-stage-validation-pending"]
             and all(receipt.get(key) is False for key in
                     ("strategy_journal_terminal", "exact_completion_fence_complete", "full_cultivation_shadow_complete")),
             "native shadow/source qualification flags differ")
    return {**deepcopy(dict(receipt)), "native_receipt_reused": True,
        "verification": {"schema": "gkms.native-completed-receipt-reuse.v1",
            "report_sha256": receipt["report_sha256"], "formal_result_sha256": hashlib.sha256(canonical_json_bytes(formal)).hexdigest(),
            "run_identity_verified": True, "report_rewritten": False, "legacy_finalizer_invoked": False,
            "dataset_ingest_attempted": False, "training_triggered": False}}


__all__ = ["reuse_native_completed_run_evidence"]
