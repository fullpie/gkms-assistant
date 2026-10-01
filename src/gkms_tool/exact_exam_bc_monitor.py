"""Read-only incremental projection of Exact Exam BC diagnostic evidence.

The ledger is append-only and owned by the completed-action sidecar.  This
module keeps a byte cursor, consumes only newly appended complete JSON lines,
and exposes a row only when the caller supplies matching fresh live context.
It never loads a policy bundle or evaluates an action.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any, Final

from .exact_exam_bc_sidecar import DEFAULT_LEDGER_PATH, EVIDENCE_SCHEMA


DEFAULT_FRESHNESS_SECONDS: Final = 180.0
_FUTURE_CLOCK_TOLERANCE_SECONDS: Final = 5.0


def _recorded_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


@dataclass(frozen=True, slots=True)
class ExactExamBCMonitorContext:
    run_id: str
    flow: str
    stage: str
    state_timestamp: float

    def __post_init__(self) -> None:
        for name in ("run_id", "flow", "stage"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty text")
        if isinstance(self.state_timestamp, bool) or not isinstance(
            self.state_timestamp, (int, float)
        ):
            raise TypeError("state_timestamp must be numeric")


class ExactExamBCDiagnosticLedgerMonitor:
    """Incrementally tail diagnostic JSONL without modifying its contents."""

    def __init__(
        self,
        path: str | Path = DEFAULT_LEDGER_PATH,
        *,
        freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS,
    ) -> None:
        if freshness_seconds <= 0:
            raise ValueError("freshness_seconds must be positive")
        self.path = Path(path)
        self.freshness_seconds = float(freshness_seconds)
        self._offset = 0
        self._pending = b""
        self._file_identity: tuple[int, int] | None = None
        self._latest: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
        self._record_ids: set[str] = set()

    @property
    def offset(self) -> int:
        """Current byte cursor, exposed for deterministic monitor tests."""

        return self._offset

    def _reset(self) -> None:
        self._offset = 0
        self._pending = b""
        self._latest.clear()
        self._record_ids.clear()

    def _accept(self, raw: object) -> None:
        if not isinstance(raw, Mapping):
            return
        if raw.get("schema") != EVIDENCE_SCHEMA or raw.get("status") != "scored":
            return
        if raw.get("diagnostic_only") is not True or raw.get("applied") is not False:
            return
        source = raw.get("source")
        scope = raw.get("scope")
        formal = raw.get("formal")
        model = raw.get("model")
        if not all(
            isinstance(value, Mapping)
            for value in (source, scope, formal, model)
        ):
            return
        run_id = source.get("run_id")
        record_id = raw.get("record_id")
        flow = scope.get("flow")
        stage = scope.get("stage")
        recorded = _recorded_timestamp(raw.get("recorded_at"))
        if not all(
            isinstance(value, str) and value
            for value in (record_id, run_id, flow, stage)
        ):
            # Legacy rows without an explicit formal-run binding are never
            # guessed onto the current GUI run.
            return
        if recorded is None:
            return
        if record_id in self._record_ids:
            return
        formal_action = formal.get("action_id")
        model_action = model.get("top_action_id")
        agreement = model.get("agrees_with_formal")
        if not (
            isinstance(formal_action, str)
            and formal_action
            and isinstance(model_action, str)
            and model_action
            and type(agreement) is bool
        ):
            return
        key = (run_id, flow, stage)
        current = self._latest.get(key)
        if current is None or recorded >= current[0]:
            self._latest[key] = (recorded, dict(raw))
        self._record_ids.add(record_id)

    def _read_appended(self) -> None:
        try:
            stat = self.path.stat()
        except (FileNotFoundError, OSError):
            return
        identity = (int(stat.st_dev), int(stat.st_ino))
        if self._file_identity is not None and (
            identity != self._file_identity or stat.st_size < self._offset
        ):
            self._reset()
        self._file_identity = identity
        try:
            with self.path.open("rb") as stream:
                stream.seek(self._offset)
                appended = stream.read()
        except OSError:
            return
        self._offset += len(appended)
        if not appended:
            return
        chunks = (self._pending + appended).split(b"\n")
        self._pending = chunks.pop()
        for line in chunks:
            if not line.strip():
                continue
            try:
                self._accept(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # One malformed diagnostic line cannot affect either the GUI
                # or later valid append-only rows.
                continue

    def poll(
        self,
        context: ExactExamBCMonitorContext | None,
        *,
        now: float | None = None,
    ) -> Mapping[str, Any] | None:
        self._read_appended()
        if context is None:
            return None
        current_time = time.time() if now is None else float(now)
        if not (
            -_FUTURE_CLOCK_TOLERANCE_SECONDS
            <= current_time - context.state_timestamp
            <= self.freshness_seconds
        ):
            return None
        value = self._latest.get((context.run_id, context.flow, context.stage))
        if value is None:
            return None
        recorded, record = value
        if not (
            -_FUTURE_CLOCK_TOLERANCE_SECONDS
            <= current_time - recorded
            <= self.freshness_seconds
        ):
            return None
        if abs(recorded - context.state_timestamp) > self.freshness_seconds:
            return None
        return record


__all__ = [
    "DEFAULT_FRESHNESS_SECONDS",
    "ExactExamBCDiagnosticLedgerMonitor",
    "ExactExamBCMonitorContext",
]
