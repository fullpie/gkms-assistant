"""Bind one completed Maa exam to the frozen native recorder span.

The native DLL is append-only for the lifetime of the game process.  A Maa
exam therefore owns a byte interval, not the whole per-PID JSONL file.  This
module records that interval without touching the game, validates the one
native stage through the existing exact-stage contract, and returns the
already promoted transition rows used by the Exact BC shadow.

No candidate is reconstructed from Maa, LocalSave, or the simulator here.
If the interval is partial, contains more than one stage, or fails any native
continuity/legal-action gate, the capture is rejected as a whole.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Final

from .runtime_exact_stage_dataset import (
    RECORDER_SCHEMA,
    split_runtime_exact_stages,
    validate_runtime_exact_stage,
)
from .training_artifact_io import canonical_json_bytes


SCHEMA: Final = "gkms.native-runtime-stage-capture.v1"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_TELEMETRY_DIR: Final = PROJECT_ROOT / "var" / "telemetry"
MAIN_ACTION_SURFACE_EVIDENCE: Final = (
    "frozen-v1:"
    "d94548addfb878a7453505442f99736e10cdb9c1b3d3bff03e9dada6cbf5a6cb:"
    "1bffd8d8195dcf61c1628525974b8c0aa3d414b94c00e3316a7c2cf10de8b7ec"
)


def default_native_runtime_recorder_path(pid: int) -> Path:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("native runtime recorder pid must be positive")
    return (
        DEFAULT_TELEMETRY_DIR
        / f"runtime_exam_recorder_legal_verified_probe_{pid}.jsonl"
    )


@dataclass(frozen=True, slots=True)
class NativeRuntimeStageCapture:
    pid: int
    path: Path
    start_offset: int
    end_offset: int | None = None
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ValueError("native runtime stage capture schema mismatch")
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError("native runtime stage capture pid must be positive")
        path = Path(self.path).resolve(strict=False)
        object.__setattr__(self, "path", path)
        if (
            isinstance(self.start_offset, bool)
            or not isinstance(self.start_offset, int)
            or self.start_offset < 0
        ):
            raise ValueError("native runtime stage start_offset must be non-negative")
        if self.end_offset is not None and (
            isinstance(self.end_offset, bool)
            or not isinstance(self.end_offset, int)
            or self.end_offset < self.start_offset
        ):
            raise ValueError("native runtime stage end_offset is invalid")

    @property
    def complete(self) -> bool:
        return self.end_offset is not None and self.end_offset > self.start_offset

    def finish(self) -> "NativeRuntimeStageCapture":
        end = self.path.stat().st_size if self.path.is_file() else self.start_offset
        return type(self)(
            pid=self.pid,
            path=self.path,
            start_offset=self.start_offset,
            end_offset=end,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "pid": self.pid,
            "path": str(self.path),
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "complete": self.complete,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "NativeRuntimeStageCapture":
        if not isinstance(value, Mapping):
            raise ValueError("native runtime stage capture must be an object")
        return cls(
            schema=value.get("schema", ""),  # type: ignore[arg-type]
            pid=value.get("pid"),  # type: ignore[arg-type]
            path=Path(value.get("path", "")),  # type: ignore[arg-type]
            start_offset=value.get("start_offset"),  # type: ignore[arg-type]
            end_offset=value.get("end_offset"),  # type: ignore[arg-type]
        )


def begin_native_runtime_stage_capture(
    pid: int,
    *,
    path: str | Path | None = None,
) -> NativeRuntimeStageCapture:
    source = (
        default_native_runtime_recorder_path(pid)
        if path is None
        else Path(path).resolve(strict=False)
    )
    start = source.stat().st_size if source.is_file() else 0
    return NativeRuntimeStageCapture(
        pid=pid,
        path=source,
        start_offset=start,
    )


def _capture_scan(
    capture: NativeRuntimeStageCapture,
) -> tuple[list[dict[str, Any]], dict[str, object], str]:
    if not capture.complete or capture.end_offset is None:
        raise ValueError("native runtime stage capture is incomplete")
    if not capture.path.is_file():
        raise FileNotFoundError(capture.path)
    size = capture.path.stat().st_size
    if size < capture.end_offset:
        raise ValueError("native runtime stage capture was truncated")

    transitions: list[dict[str, Any]] = []
    legal_preflights: list[dict[str, object]] = []
    recorder_preflights: list[dict[str, object]] = []
    parse_errors: list[str] = []
    selected_hasher = hashlib.sha256()
    byte_offset = 0
    rows_seen = 0
    with capture.path.open("rb") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line_start = byte_offset
            byte_offset += len(raw_line)
            if line_start >= capture.end_offset:
                break
            if byte_offset > capture.end_offset:
                if line_start >= capture.start_offset:
                    parse_errors.append(f"line-{line_number}:crosses-end-offset")
                break
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line.decode("utf-8-sig"))
            except (json.JSONDecodeError, UnicodeError, ValueError) as error:
                if line_start >= capture.start_offset:
                    parse_errors.append(f"line-{line_number}:{type(error).__name__}")
                continue
            if not isinstance(row, Mapping) or row.get("schema") != RECORDER_SCHEMA:
                continue
            body = row.get("body")
            if not isinstance(body, Mapping):
                continue
            record = body.get("record")
            if record == "legal_verified_probe_preflight":
                legal_preflights.append(
                    {
                        "line_number": line_number,
                        "pid": row.get("pid"),
                        "sequence": row.get("sequence"),
                        "valid": (
                            body.get("valid") is True
                            and body.get("target")
                            == "gkms_runtime_exam_recorder_legal_verified_probe"
                            and body.get("macro") == "GKMS_RUNTIME_LEGAL_VERIFIED_PROBE"
                            and body.get("transition_promotion_gate")
                            == "python-stage-89-field-continuity-v1"
                        ),
                    }
                )
                continue
            if record == "preflight":
                recorder_preflights.append(
                    {
                        "line_number": line_number,
                        "pid": row.get("pid"),
                        "sequence": row.get("sequence"),
                        "valid": (
                            body.get("valid") is True
                            and body.get("atomic_hooks") is True
                        ),
                    }
                )
                continue
            if record != "transition" or line_start < capture.start_offset:
                continue
            rows_seen += 1
            selected_hasher.update(raw_line)
            transitions.append(
                {
                    "line_number": line_number,
                    "row": dict(row),
                    "body": dict(body),
                }
            )
    scan: dict[str, object] = {
        "rows_seen": rows_seen,
        "parse_errors": parse_errors,
        "legal_verified_preflights": legal_preflights,
        "recorder_preflights": recorder_preflights,
    }
    return transitions, scan, selected_hasher.hexdigest()


def build_native_runtime_stage_rows(
    capture_value: NativeRuntimeStageCapture | Mapping[str, object],
    *,
    source_run_id: str,
) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
    """Validate and promote exactly one completed native stage in memory."""

    if not isinstance(source_run_id, str) or not source_run_id.strip():
        raise ValueError("native runtime stage source_run_id must be non-empty")
    capture = (
        capture_value
        if isinstance(capture_value, NativeRuntimeStageCapture)
        else NativeRuntimeStageCapture.from_mapping(capture_value)
    )
    transitions, scan, source_sha256 = _capture_scan(capture)
    if scan.get("parse_errors"):
        raise ValueError(
            f"native runtime capture parse errors: {scan['parse_errors']}"
        )
    stages, incomplete_suffix = split_runtime_exact_stages(transitions)
    if incomplete_suffix or len(stages) != 1:
        raise ValueError(
            "native runtime capture must contain exactly one terminal stage"
        )
    capture_source_id = (
        f"runtime-recorder:{capture.pid}:{capture.start_offset}:"
        f"{capture.end_offset}:{source_sha256}"
    )
    stage = stages[0]
    rows, stage_report = validate_runtime_exact_stage(
        stage,
        stage_index=0,
        source_sha256=source_sha256,
        trajectory_id=f"live:{source_run_id}",
        capture_source_id=capture_source_id,
        expected_mode="live",
        main_action_surface_evidence=MAIN_ACTION_SURFACE_EVIDENCE,
        scan=scan,
    )
    if stage_report.get("passed") is not True or len(rows) != len(stage):
        blockers = stage_report.get("blockers")
        raise ValueError(f"native runtime stage validation failed: {blockers}")

    result: list[dict[str, object]] = []
    for row, source_item in zip(rows, stage, strict=True):
        value = deepcopy(row)
        metadata = value.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("promoted native runtime row metadata is invalid")
        before = value.get("state_before")
        after = value.get("state_after")
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            raise ValueError("promoted native runtime row state is invalid")
        before_digest = hashlib.sha256(canonical_json_bytes(before)).hexdigest()
        after_digest = hashlib.sha256(canonical_json_bytes(after)).hexdigest()
        source_body = source_item["body"]
        metadata.update(
            {
                "candidate_authority": "native-runtime-verified-stage-v1",
                "evidence_before_digest": before_digest,
                "evidence_after_digest": after_digest,
                "source_before_sha256": before_digest,
                "source_after_sha256": after_digest,
                "step_context_id": f"exam-step:{before.get('stepType')}",
                "session_transition_id": (
                    f"native-sequence:{source_body.get('runtime_sequence')}"
                ),
                "native_runtime_capture_schema": SCHEMA,
                "native_runtime_envelope_sequence": source_item["row"].get(
                    "sequence"
                ),
            }
        )
        result.append(value)

    report = {
        "schema": SCHEMA,
        "status": "accepted",
        "source_run_id": source_run_id.strip(),
        "capture": capture.to_dict(),
        "capture_source_id": capture_source_id,
        "source_sha256": source_sha256,
        "transition_count": len(result),
        "stage": stage_report,
        "scan": scan,
        "simulator_used": False,
        "formal_control_unchanged": True,
    }
    return tuple(result), report


__all__ = [
    "DEFAULT_TELEMETRY_DIR",
    "MAIN_ACTION_SURFACE_EVIDENCE",
    "NativeRuntimeStageCapture",
    "SCHEMA",
    "begin_native_runtime_stage_capture",
    "build_native_runtime_stage_rows",
    "default_native_runtime_recorder_path",
]
