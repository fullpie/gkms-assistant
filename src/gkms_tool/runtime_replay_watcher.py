"""Incrementally watch the runtime recorder for one Replay Exam stage.

The native recorder appends very large JSONL rows (the serialized
``ExamSaveData`` is intentionally retained as shadow evidence).  Re-running
the offline audit on every poll would repeatedly parse the complete file and
would make a live recommended-replay check needlessly expensive.  This module
keeps a byte offset and reads only bytes appended since the previous poll.

The watcher deliberately has a narrower contract than the recorder audit:
it waits for the first transition whose *native* ``isReplay`` state is true,
then summarizes that stage until its terminal transition.  It never fills in
missing states, legal actions, or exactness from the simulator, ExamSave, or
another source.  All reports are observational and ``promotion.allowed`` is
always false.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import argparse
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TELEMETRY_DIR = PROJECT_ROOT / "var" / "telemetry"
RECORDER_SCHEMA = "gkms.runtime-exam-recorder.shadow.v1"
SCHEMA = "gkms.runtime-replay-watcher.v1"
WATCHER_VERSION = "incremental-v1"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.25
DEFAULT_CHUNK_BYTES = 64 * 1024
_MAX_REPORTED_ERRORS = 64
_MAX_REPORTED_MISMATCHES = 64


class RuntimeReplayWatcherError(ValueError):
    """Raised for invalid watcher arguments, not for an incomplete file."""


def default_shadow_path(pid: int) -> Path:
    """Return the recorder's per-process append-only shadow JSONL path."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RuntimeReplayWatcherError("pid must be a positive integer")
    return DEFAULT_TELEMETRY_DIR / f"runtime_exam_recorder_shadow_{pid}.jsonl"


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value}")


def _canonical_json(value: object) -> bytes:
    """Serialize a state deterministically for exact adjacent comparison."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def _is_bool(value: object) -> bool:
    return type(value) is bool


def _is_int(value: object, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _state_mode(state: object) -> bool | None:
    if not isinstance(state, Mapping) or "isReplay" not in state:
        return None
    value = state.get("isReplay")
    return value if _is_bool(value) else None


def _body_mode(body: Mapping[str, object]) -> bool | None:
    """Return native replay mode when either captured state exposes it.

    ``None`` means the state is absent/malformed, not that it is Live.  This
    distinction prevents a malformed row from being silently accepted as a
    Replay stage boundary.
    """

    values = [
        value
        for value in (
            _state_mode(body.get("state_before")),
            _state_mode(body.get("state_after")),
        )
        if value is not None
    ]
    if not values:
        return None
    if all(value is values[0] for value in values):
        return values[0]
    return None


def _mode_name(value: bool | None) -> str:
    if value is True:
        return "replay"
    if value is False:
        return "live"
    return "unknown"


def _safe_error(error: BaseException) -> str:
    return f"{type(error).__name__}:{error}"


class IncrementalShadowReader:
    """Read complete JSONL rows from an append-only file without rereading.

    The reader's offset advances over bytes read, including an incomplete last
    line.  That incomplete line is held in ``_carry`` and completed on the
    next poll.  ``bytes_read_total`` is exposed in the report/tests so the
    incremental behavior remains observable.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> None:
        self.path = Path(path).expanduser()
        if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes <= 0:
            raise RuntimeReplayWatcherError("chunk_bytes must be a positive integer")
        self.chunk_bytes = chunk_bytes
        self.offset = 0
        self.line_number = 0
        self._carry = b""
        self._file_key: tuple[int, int] | None = None
        self.bytes_read_total = 0
        self.poll_count = 0
        self.missing_file_polls = 0
        self.reset_count = 0
        self.read_sizes: list[int] = []

    @property
    def pending_bytes(self) -> int:
        return len(self._carry)

    def _reset_for_replacement(self) -> None:
        self.offset = 0
        self.line_number = 0
        self._carry = b""
        self._file_key = None
        self.reset_count += 1

    @staticmethod
    def _file_identity(stat_result: Any) -> tuple[int, int] | None:
        # st_ino is stable for normal append writes on Windows and POSIX.  A
        # filesystem that does not expose it returns None; size shrink still
        # detects the common truncate/replace case.
        device = int(getattr(stat_result, "st_dev", 0) or 0)
        inode = int(getattr(stat_result, "st_ino", 0) or 0)
        if device == 0 and inode == 0:
            return None
        return device, inode

    def _parse_line(self, raw_line: bytes, line_number: int) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        if not raw_line.strip():
            return None, {"line": line_number, "error": "blank-line"}
        try:
            # Accept a UTF-8 BOM only at the beginning of the first physical
            # line; all later bytes must remain strict UTF-8.
            encoding = "utf-8-sig" if line_number == 1 else "utf-8"
            value = json.loads(
                raw_line.decode(encoding),
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            return None, {"line": line_number, "error": f"json:{_safe_error(error)}"}
        if not isinstance(value, dict):
            return None, {"line": line_number, "error": "row-not-object"}
        return value, None

    def poll(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Return newly completed rows and parse errors since the last poll."""

        self.poll_count += 1
        try:
            stat_result = self.path.stat()
        except FileNotFoundError:
            self.missing_file_polls += 1
            return [], []
        except OSError as error:
            return [], [{"line": 0, "error": f"file:{_safe_error(error)}"}]

        file_key = self._file_identity(stat_result)
        if (
            (self._file_key is not None and file_key is not None and file_key != self._file_key)
            or int(stat_result.st_size) < self.offset
        ):
            self._reset_for_replacement()
            file_key = self._file_identity(stat_result)
        if self._file_key is None:
            self._file_key = file_key

        rows: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                while True:
                    chunk = handle.read(self.chunk_bytes)
                    if not chunk:
                        break
                    self.read_sizes.append(len(chunk))
                    self.bytes_read_total += len(chunk)
                    self.offset += len(chunk)
                    self._carry += chunk
                    parts = self._carry.split(b"\n")
                    self._carry = parts.pop()
                    for part in parts:
                        self.line_number += 1
                        if part.endswith(b"\r"):
                            part = part[:-1]
                        row, error = self._parse_line(part, self.line_number)
                        if row is not None:
                            rows.append(row)
                        if error is not None:
                            errors.append(error)
        except OSError as error:
            errors.append({"line": 0, "error": f"file:{_safe_error(error)}"})
        return rows, errors


class RuntimeReplayStageWatcher:
    """Consume recorder rows and summarize the first requested mode stage."""

    _IDENTITY_FIELDS = (
        "produceId",
        "idolCardId",
        "settingId",
        "characterId",
        "examType",
        "stepType",
        "phase",
        "limitTurn",
    )

    def __init__(
        self,
        *,
        expected_mode: str = "replay",
        expected_pid: int | None = None,
        expected_action_count: int | None = None,
    ) -> None:
        if expected_mode not in {"live", "replay"}:
            raise RuntimeReplayWatcherError("expected_mode must be 'live' or 'replay'")
        if expected_pid is not None and (
            isinstance(expected_pid, bool) or not isinstance(expected_pid, int) or expected_pid <= 0
        ):
            raise RuntimeReplayWatcherError("expected_pid must be a positive integer or None")
        if expected_action_count is not None and (
            isinstance(expected_action_count, bool)
            or not isinstance(expected_action_count, int)
            or expected_action_count <= 0
        ):
            raise RuntimeReplayWatcherError(
                "expected_action_count must be a positive integer or None"
            )
        self.expected_mode = expected_mode
        self.expected_mode_bool = expected_mode == "replay"
        self.expected_pid = expected_pid
        self.expected_action_count = expected_action_count
        self.rows_seen = 0
        self.rows_ignored_pid = 0
        self.rows_ignored_schema = 0
        self.rows_after_terminal = 0
        self.schema_invalid_rows: list[int] = []
        self.parse_errors: list[dict[str, object]] = []
        self.observed_pids: set[int] = set()
        self.rows_ignored_runtime_sequence = 0
        self.abandoned_runtime_sequences: list[dict[str, object]] = []
        self._reset_selected_stage()

    def _reset_selected_stage(self) -> None:
        """Reset only the selected stage, retaining file-scan diagnostics."""

        self.stage_started = False
        self.stage_complete = False
        self.stage_start_sequence: int | None = None
        self.stage_runtime_sequence: object | None = None
        self.stage_identity: dict[str, object] = {}
        self.stage_transitions = 0
        # Keep the selected transition envelopes available to small,
        # read-only validators.  The watcher still does not retain rows from
        # an earlier Live stage or a later Replay stage, and callers that do
        # not need the raw envelopes continue to use the compact report.
        self._stage_transition_rows: list[dict[str, object]] = []
        # Some probe builds emit the hand/passive records as small JSONL rows
        # adjacent to the transition rather than nesting them under
        # ``legal_action_candidates``.  Keep only those bounded records and
        # join them by the native action_order when the transition is seen.
        self._pending_probe_rows: dict[object, list[dict[str, object]]] = {}
        self.action_orders: list[int] = []
        self.invalid_action_order_positions: list[int] = []
        self.action_types: list[str | None] = []
        self.action_indexes: list[int | None] = []
        self.turn1_before_positions: list[int] = []
        self.turn1_after_positions: list[int] = []
        self.before_captured_count = 0
        self.after_captured_count = 0
        self.before_complete_true_count = 0
        self.after_complete_true_count = 0
        self.terminal_positions: list[int] = []
        self.terminal_marker_count = 0
        self.terminal_marker_seen = False
        self.terminal_marker_sequences: list[int] = []
        self.mode_unknown_transition_count = 0
        self.mode_mismatch_transition_count = 0
        self._previous_after: bytes | None = None
        self._previous_after_digest: str | None = None
        self._previous_after_size: int | None = None
        self.adjacent_pairs = 0
        self.canonical_equal_pairs = 0
        self.continuity_mismatches: list[dict[str, object]] = []

    @staticmethod
    def _action_type(action: object) -> str | None:
        if not isinstance(action, Mapping):
            return None
        value = action.get("action_type")
        if isinstance(value, str):
            return value
        # Keep the native action labels intact if the recorder adds a new
        # label.  The watcher summarizes rather than normalizes actions.
        for name in ("playType", "play_type", "type"):
            candidate = action.get(name)
            if isinstance(candidate, str):
                return candidate
        return None

    @staticmethod
    def _action_index(action: object) -> int | None:
        if not isinstance(action, Mapping):
            return None
        for name in ("play_index", "playIndex", "index"):
            value = action.get(name)
            if _is_int(value):
                return int(value)
        return None

    @staticmethod
    def _state_digest(value: object) -> tuple[bytes | None, str | None, int | None]:
        if not isinstance(value, Mapping):
            return None, None, None
        try:
            serialized = _canonical_json(value)
        except (TypeError, ValueError, UnicodeError):
            return None, None, None
        return serialized, hashlib.sha256(serialized).hexdigest(), len(serialized)

    @staticmethod
    def _runtime_sequence(body: Mapping[str, object]) -> object | None:
        value = body.get("runtime_sequence")
        if isinstance(value, str) and value:
            return value
        if _is_int(value, minimum=1):
            return int(value)
        return None

    def _restart_for_runtime_sequence(
        self,
        row: Mapping[str, object],
        body: Mapping[str, object],
    ) -> None:
        """Drop an unterminated recommendation sequence at a new Turn1 root."""

        self.abandoned_runtime_sequences.append(
            {
                "runtime_sequence": self.stage_runtime_sequence,
                "transition_count": self.stage_transitions,
                "action_orders": list(self.action_orders),
                "terminal_seen": self.stage_complete,
                "reason": "superseded-by-new-turn1-runtime-sequence",
            }
        )
        self._reset_selected_stage()
        self._start_transition(row, body)

    def _reject_completed_count_mismatch(self) -> None:
        """Continue to the next runtime sequence when the bound count differs."""

        if (
            not self.stage_complete
            or self.expected_action_count is None
            or self.stage_transitions == self.expected_action_count
        ):
            return
        self.abandoned_runtime_sequences.append(
            {
                "runtime_sequence": self.stage_runtime_sequence,
                "transition_count": self.stage_transitions,
                "action_orders": list(self.action_orders),
                "terminal_seen": True,
                "reason": "terminal-action-count-mismatch",
                "expected_action_count": self.expected_action_count,
            }
        )
        self._reset_selected_stage()

    def _mode_matches(self, body: Mapping[str, object]) -> bool:
        mode = _body_mode(body)
        if mode is None:
            self.mode_unknown_transition_count += 1
            return False
        if mode != self.expected_mode_bool:
            self.mode_mismatch_transition_count += 1
            return False
        return True

    def _start_transition(self, row: Mapping[str, object], body: Mapping[str, object]) -> None:
        # Mode mismatches encountered while searching are expected (for
        # example, a Live stage earlier in the same PID).  Stage diagnostics
        # begin only after the first requested-mode transition is selected.
        self.mode_unknown_transition_count = 0
        self.mode_mismatch_transition_count = 0
        self.stage_started = True
        self.stage_runtime_sequence = self._runtime_sequence(body)
        sequence = row.get("sequence")
        self.stage_start_sequence = int(sequence) if _is_int(sequence, minimum=1) else None
        before = body.get("state_before")
        if isinstance(before, Mapping):
            self.stage_identity = {
                name: before[name]
                for name in self._IDENTITY_FIELDS
                if name in before
            }
        self._consume_transition(row, body)

    def _consume_transition(self, row: Mapping[str, object], body: Mapping[str, object]) -> None:
        if self.stage_complete:
            self.rows_after_terminal += 1
            return
        position = self.stage_transitions
        self.stage_transitions += 1
        # The recorder's state snapshots can be many megabytes.  Candidate
        # validation needs only the transition envelope, legal-action policy,
        # candidate object, terminal bit, and Turn1 marker; retaining the
        # complete snapshots here would defeat the watcher's bounded-memory
        # purpose.
        before = body.get("state_before")
        compact_body: dict[str, object] = {
            "record": body.get("record"),
            "action_order": body.get("action_order"),
            "action": body.get("action"),
            "legal_actions": body.get("legal_actions"),
            "legal_action_candidates": body.get("legal_action_candidates"),
            "terminal": body.get("terminal"),
            "state_before": (
                {"currentTurn": before.get("currentTurn")}
                if isinstance(before, Mapping) and "currentTurn" in before
                else None
            ),
        }
        if "boundary" in body:
            compact_body["boundary"] = body.get("boundary")
        # The next legal-action probe may attach validator/passive evidence
        # beside (rather than inside) ``legal_action_candidates``.  Preserve
        # only the bounded probe payload keys so the watcher remains compact;
        # full state snapshots are still reduced to the Turn1 marker above.
        for probe_key in (
            "runtime_probe",
            "runtime_legal_action_probe",
            "runtime_evidence",
            "legal_action_probe",
            "passive_probe",
            "hand_validator_probe",
            "drink_probe",
            "end_turn_probe",
            "endturn_probe",
            "family_probes",
            "probe",
        ):
            if probe_key in body:
                compact_body[probe_key] = body.get(probe_key)
        action_order = body.get("action_order")
        pending = self._pending_probe_rows.pop(action_order, None)
        if pending:
            compact_body["runtime_probe_records"] = pending
        self._stage_transition_rows.append(
            {
                "schema": row.get("schema"),
                "pid": row.get("pid"),
                "shadow": row.get("shadow"),
                "exact": row.get("exact"),
                "legal_actions_complete": row.get("legal_actions_complete"),
                "body": compact_body,
            }
        )
        order = body.get("action_order")
        if _is_int(order, minimum=0):
            self.action_orders.append(int(order))
        else:
            self.invalid_action_order_positions.append(position)
        action = body.get("action")
        self.action_types.append(self._action_type(action))
        self.action_indexes.append(self._action_index(action))

        before = body.get("state_before")
        after = body.get("state_after")
        before_map = before if isinstance(before, Mapping) else None
        after_map = after if isinstance(after, Mapping) else None
        before_captured = body.get("state_before_captured") is True and before_map is not None
        after_captured = body.get("state_after_captured") is True and after_map is not None
        if before_captured:
            self.before_captured_count += 1
        if after_captured:
            self.after_captured_count += 1
        if body.get("state_before_complete") is True:
            self.before_complete_true_count += 1
        if body.get("state_after_complete") is True:
            self.after_complete_true_count += 1
        if before_map is not None and before_map.get("currentTurn") == 1:
            self.turn1_before_positions.append(position)
        if after_map is not None and after_map.get("currentTurn") == 1:
            self.turn1_after_positions.append(position)

        before_bytes, before_digest, before_size = self._state_digest(before)
        if position > 0:
            self.adjacent_pairs += 1
            equal = (
                before_bytes is not None
                and self._previous_after is not None
                and before_bytes == self._previous_after
            )
            if equal:
                self.canonical_equal_pairs += 1
            elif len(self.continuity_mismatches) < _MAX_REPORTED_MISMATCHES:
                self.continuity_mismatches.append(
                    {
                        "pair": position,
                        "left_action_order": self.action_orders[position - 1]
                        if len(self.action_orders) >= position
                        else None,
                        "right_action_order": order if _is_int(order, minimum=0) else None,
                        "left_sha256": self._previous_after_digest,
                        "right_sha256": before_digest,
                        "left_bytes": self._previous_after_size,
                        "right_bytes": before_size,
                        "reason": "canonical-state-after-before-mismatch-or-missing",
                    }
                )
        after_bytes, after_digest, after_size = self._state_digest(after)
        self._previous_after = after_bytes
        self._previous_after_digest = after_digest
        self._previous_after_size = after_size
        if body.get("terminal") is True:
            self.terminal_positions.append(position)
            self.stage_complete = True

    @staticmethod
    def _is_probe_record(body: Mapping[str, object]) -> bool:
        record = body.get("record")
        if isinstance(record, str) and any(
            token in record.casefold()
            for token in ("probe", "validator", "passive", "purity")
        ):
            return True
        return any(
            key in body
            for key in (
                "runtime_probe",
                "runtime_evidence",
                "legal_action_probe",
                "passive_probe",
                "hand_validator_probe",
                "drink_probe",
                "end_turn_probe",
                "endturn_probe",
            )
        )

    def _consume_probe_record(self, body: Mapping[str, object]) -> None:
        """Retain one bounded auxiliary probe row for its transition."""

        if not self._is_probe_record(body):
            return
        action_order = body.get("action_order")
        if action_order is None:
            return
        record = dict(body)
        # Native state snapshots are never needed from an auxiliary probe row;
        # discard them if a future writer happens to repeat one here.
        for key in ("state_before", "state_after", "state", "snapshot"):
            record.pop(key, None)
        for compact in reversed(self._stage_transition_rows):
            compact_body = compact.get("body")
            if isinstance(compact_body, Mapping) and compact_body.get("action_order") == action_order:
                records = compact_body.setdefault("runtime_probe_records", [])
                if isinstance(records, list):
                    records.append(record)
                return
        self._pending_probe_rows.setdefault(action_order, []).append(record)

    def _consume_terminal_marker(self, row: Mapping[str, object], body: Mapping[str, object]) -> None:
        marker_runtime_sequence = body.get("sequence")
        if (
            self.stage_runtime_sequence is not None
            and marker_runtime_sequence is not None
            and marker_runtime_sequence != self.stage_runtime_sequence
        ):
            self.rows_ignored_runtime_sequence += 1
            return
        marker_state = body.get("state")
        marker_mode = _state_mode(marker_state)
        if marker_mode is not self.expected_mode_bool:
            return
        self.terminal_marker_count += 1
        if body.get("terminal") is True:
            self.terminal_marker_seen = True
            sequence = row.get("sequence")
            if _is_int(sequence, minimum=1):
                self.terminal_marker_sequences.append(int(sequence))

    def consume(self, rows: Sequence[Mapping[str, object]], errors: Sequence[Mapping[str, object]] = ()) -> None:
        """Consume only newly-read rows; no historical file scan occurs here."""

        self.parse_errors.extend(dict(error) for error in errors[:_MAX_REPORTED_ERRORS])
        for row in rows:
            self.rows_seen += 1
            pid = row.get("pid")
            if _is_int(pid, minimum=1):
                self.observed_pids.add(int(pid))
            if self.expected_pid is not None and pid != self.expected_pid:
                self.rows_ignored_pid += 1
                continue
            if row.get("schema") != RECORDER_SCHEMA:
                self.rows_ignored_schema += 1
                if len(self.schema_invalid_rows) < _MAX_REPORTED_ERRORS:
                    self.schema_invalid_rows.append(self.rows_seen)
                continue
            body = row.get("body")
            if not isinstance(body, Mapping):
                self.rows_ignored_schema += 1
                continue
            record = body.get("record")
            if not self.stage_started:
                if record == "transition" and self._mode_matches(body):
                    self._start_transition(row, body)
                    self._reject_completed_count_mismatch()
                elif record != "transition" and self._is_probe_record(body):
                    # Retain only an action-order keyed auxiliary row; it is
                    # joined once the corresponding Replay transition starts.
                    self._consume_probe_record(body)
                continue
            if self.stage_complete:
                # The terminal marker can be appended immediately before or
                # after the terminal transition.  Retain that one proof, but
                # never let a later stage in the same PID contaminate the
                # already completed selection.
                if record == "terminal_marker":
                    self._consume_terminal_marker(row, body)
                elif self._is_probe_record(body):
                    self._consume_probe_record(body)
                else:
                    self.rows_after_terminal += 1
                continue
            if record == "terminal_marker":
                self._consume_terminal_marker(row, body)
                continue
            if record != "transition" and self._is_probe_record(body):
                probe_runtime_sequence = body.get(
                    "runtime_sequence", body.get("sequence")
                )
                if (
                    self.stage_runtime_sequence is not None
                    and probe_runtime_sequence is not None
                    and probe_runtime_sequence != self.stage_runtime_sequence
                ):
                    self.rows_ignored_runtime_sequence += 1
                    continue
                self._consume_probe_record(body)
                continue
            if record != "transition":
                continue
            # Once a Replay stage has started, retain unknown-mode transition
            # rows as part of the stage only when their native state is
            # unavailable; a known Live row is never allowed to contaminate it.
            mode = _body_mode(body)
            if mode is not None and mode != self.expected_mode_bool:
                self.mode_mismatch_transition_count += 1
                continue
            if mode is None:
                self.mode_unknown_transition_count += 1
            runtime_sequence = self._runtime_sequence(body)
            if (
                self.stage_runtime_sequence is not None
                and runtime_sequence is not None
                and runtime_sequence != self.stage_runtime_sequence
            ):
                before = body.get("state_before")
                if (
                    isinstance(before, Mapping)
                    and before.get("currentTurn") == 1
                ):
                    self._restart_for_runtime_sequence(row, body)
                else:
                    self.rows_ignored_runtime_sequence += 1
                continue
            self._consume_transition(row, body)
            self._reject_completed_count_mismatch()

    @property
    def stage_transition_rows(self) -> tuple[Mapping[str, object], ...]:
        """Return the selected stage's transition envelopes.

        The tuple is a shallow immutable view of compact envelopes captured
        while consuming the append-only source.  Large runtime state snapshots
        are intentionally reduced to their Turn1 marker, so a focused offline
        validator can inspect a nested shadow field (for example,
        ``legal_action_candidates``) without retaining or rescanning the
        complete JSONL.
        No caller may use this property to mutate the watcher state.
        """

        return tuple(self._stage_transition_rows)

    def _action_order_report(self) -> dict[str, object]:
        values = list(self.action_orders)
        expected = list(range(values[0], values[0] + len(values))) if values else []
        gaps = [
            {
                "position": index + 1,
                "expected": right,
                "observed": left,
            }
            for index, (left, right) in enumerate(zip(values, values[1:]))
            if right != left + 1
        ]
        duplicates = sorted(value for value in set(values) if values.count(value) > 1)
        passed = bool(values) and not self.invalid_action_order_positions and values == expected and not duplicates
        return {
            "passed": passed,
            "values": values,
            "transition_count": self.stage_transitions,
            "invalid_positions": list(self.invalid_action_order_positions),
            "expected_contiguous_values": expected,
            "start": values[0] if values else None,
            "end": values[-1] if values else None,
            "gap_count": len(gaps),
            "gaps": gaps,
            "duplicate_values": duplicates,
            "policy": "native producer action_order is retained; no reindexing",
        }

    def report(
        self,
        *,
        path: str | Path,
        reader: IncrementalShadowReader | None = None,
        timed_out: bool = False,
        elapsed_seconds: float | None = None,
    ) -> dict[str, object]:
        transition_count = self.stage_transitions
        capture_passed = (
            self.stage_started
            and transition_count > 0
            and self.before_captured_count == transition_count
            and self.after_captured_count == transition_count
        )
        pair_count = max(0, transition_count - 1)
        continuity_passed = self.stage_started and (
            pair_count == 0 or self.canonical_equal_pairs == pair_count
        )
        terminal_once_last = (
            len(self.terminal_positions) == 1
            and self.terminal_positions[-1] == transition_count - 1
        )
        turn1_passed = bool(self.turn1_before_positions) and self.turn1_before_positions[0] == 0
        stage_passed = (
            self.stage_complete
            and self.stage_started
            and self._action_order_report()["passed"] is True
            and capture_passed
            and continuity_passed
            and turn1_passed
            and terminal_once_last
            and self.terminal_marker_seen
            and self.mode_unknown_transition_count == 0
            and self.mode_mismatch_transition_count == 0
        )
        scan: dict[str, object] = {
            "rows_seen": self.rows_seen,
            "rows_ignored_pid": self.rows_ignored_pid,
            "rows_ignored_schema": self.rows_ignored_schema,
            "rows_ignored_runtime_sequence": self.rows_ignored_runtime_sequence,
            "rows_after_terminal": self.rows_after_terminal,
            "observed_pids": sorted(self.observed_pids),
            "schema_invalid_rows": list(self.schema_invalid_rows),
            "parse_errors": list(self.parse_errors),
        }
        if reader is not None:
            read_sizes = list(reader.read_sizes)
            scan.update(
                {
                    "poll_count": reader.poll_count,
                    "bytes_read": reader.bytes_read_total,
                    "current_offset": reader.offset,
                    "pending_bytes": reader.pending_bytes,
                    "missing_file_polls": reader.missing_file_polls,
                    "file_reset_count": reader.reset_count,
                    "incremental": True,
                    "read_count": len(read_sizes),
                    "read_size_min": min(read_sizes) if read_sizes else None,
                    "read_size_max": max(read_sizes) if read_sizes else None,
                    "read_sizes_sample": (
                        read_sizes
                        if len(read_sizes) <= 8
                        else [*read_sizes[:4], *read_sizes[-4:]]
                    ),
                }
            )
        stage = {
            "started": self.stage_started,
            "complete": self.stage_complete,
            "expected_mode": self.expected_mode,
            "expected_action_count": self.expected_action_count,
            "source_mode": self.expected_mode if self.stage_started else "unknown",
            "start_sequence": self.stage_start_sequence,
            "runtime_sequence": self.stage_runtime_sequence,
            "abandoned_runtime_sequences": list(
                self.abandoned_runtime_sequences
            ),
            "identity": dict(self.stage_identity),
            "transition_count": transition_count,
            "action_orders": self._action_order_report(),
            "action_types": list(self.action_types),
            "action_indexes": list(self.action_indexes),
            "turn1": {
                "passed": turn1_passed,
                "state_before_count": len(self.turn1_before_positions),
                "state_after_count": len(self.turn1_after_positions),
                "state_before_positions": list(self.turn1_before_positions),
                "state_after_positions": list(self.turn1_after_positions),
                "first_state_before_turn": 1 if self.turn1_before_positions else None,
            },
            "state_capture": {
                "passed": capture_passed,
                "transition_count": transition_count,
                "state_before_captured_count": self.before_captured_count,
                "state_after_captured_count": self.after_captured_count,
                "state_before_complete_true_count": self.before_complete_true_count,
                "state_after_complete_true_count": self.after_complete_true_count,
                "completeness_is_unverified": (
                    self.before_complete_true_count == 0 and self.after_complete_true_count == 0
                ),
            },
            "adjacent_canonical_continuity": {
                "passed": continuity_passed,
                "adjacent_pairs": pair_count,
                "canonical_equal_pairs": self.canonical_equal_pairs,
                "mismatch_count": pair_count - self.canonical_equal_pairs,
                "mismatches": list(self.continuity_mismatches),
                "canonical_json": True,
                "state_policy": "native runtime state only; no simulator/ExamSave repair",
            },
            "terminal": {
                "passed": terminal_once_last and self.stage_complete,
                "transition_terminal_positions": list(self.terminal_positions),
                "terminal_transition_count": len(self.terminal_positions),
                "terminal_once": len(self.terminal_positions) == 1,
                "terminal_last": terminal_once_last,
                "terminal_marker_count": self.terminal_marker_count,
                "terminal_marker_seen": self.terminal_marker_seen,
                "terminal_marker_sequences": list(self.terminal_marker_sequences),
            },
            "mode_diagnostics": {
                "unknown_mode_transition_count": self.mode_unknown_transition_count,
                "known_mismatch_transition_count": self.mode_mismatch_transition_count,
            },
            "passed": stage_passed,
        }
        report: dict[str, object] = {
            "schema": SCHEMA,
            "watcher_version": WATCHER_VERSION,
            "path": str(Path(path).resolve()),
            "pid": self.expected_pid,
            "expected_mode": self.expected_mode,
            "status": "complete" if self.stage_complete else ("timeout" if timed_out else "waiting"),
            "started": self.stage_started,
            "complete": self.stage_complete,
            "timed_out": bool(timed_out and not self.stage_complete),
            "elapsed_seconds": elapsed_seconds,
            "stage": stage,
            # Keep the most useful counters at the top level for shell users;
            # ``stage`` remains the canonical nested evidence object.
            "transition_count": transition_count,
            "action_orders": list(self.action_orders),
            "turn1_state_before_count": len(self.turn1_before_positions),
            "state_before_captured_count": self.before_captured_count,
            "state_after_captured_count": self.after_captured_count,
            "canonical_equal_pairs": self.canonical_equal_pairs,
            "adjacent_pairs": pair_count,
            "scan": scan,
            "promotion": {
                "allowed": False,
                "status": "shadow-only",
                "exact": False,
                "legal_actions_complete": False,
                "reason": (
                    "runtime recorder evidence is candidate-only; watcher never promotes "
                    "exact transitions or synthesizes complete legal actions"
                ),
            },
        }
        return report


def watch_runtime_replay(
    input_path: str | Path | None = None,
    *,
    pid: int | None = None,
    expected_mode: str = "replay",
    expected_action_count: int | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Wait for the first requested stage and return an incremental report."""

    if input_path is not None and pid is not None:
        raise RuntimeReplayWatcherError("provide either input_path or pid, not both")
    if input_path is None and pid is None:
        raise RuntimeReplayWatcherError("one of input_path or pid is required")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(float(timeout_seconds)) or timeout_seconds < 0:
        raise RuntimeReplayWatcherError("timeout_seconds must be a finite number >= 0")
    if isinstance(poll_interval_seconds, bool) or not isinstance(poll_interval_seconds, (int, float)) or not math.isfinite(float(poll_interval_seconds)) or poll_interval_seconds <= 0:
        raise RuntimeReplayWatcherError("poll_interval_seconds must be a finite number > 0")
    path = default_shadow_path(pid) if pid is not None else Path(input_path)  # type: ignore[arg-type]
    reader = IncrementalShadowReader(path, chunk_bytes=chunk_bytes)
    watcher = RuntimeReplayStageWatcher(
        expected_mode=expected_mode,
        expected_pid=pid,
        expected_action_count=expected_action_count,
    )
    started_at = monotonic()
    deadline = started_at + float(timeout_seconds)
    while True:
        rows, errors = reader.poll()
        watcher.consume(rows, errors)
        now = monotonic()
        if watcher.stage_complete or now >= deadline:
            elapsed = max(0.0, now - started_at)
            return watcher.report(
                path=path,
                reader=reader,
                timed_out=not watcher.stage_complete,
                elapsed_seconds=elapsed,
            )
        sleep(min(float(poll_interval_seconds), max(0.0, deadline - now)))


def write_runtime_replay_watch_report(report: Mapping[str, object], output_path: str | Path) -> Path:
    """Write a watcher report without touching recorder input or game state."""

    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return output


def extract_first_runtime_stage(
    input_path: str | Path,
    output_path: str | Path,
    *,
    expected_mode: str = "replay",
    expected_pid: int | None = None,
    stage_index: int = 0,
    expected_action_count: int | None = None,
) -> dict[str, object]:
    """Copy one complete requested-mode stage without reindexing it."""

    if expected_mode not in {"live", "replay"}:
        raise RuntimeReplayWatcherError("expected_mode must be 'live' or 'replay'")
    if isinstance(stage_index, bool) or not isinstance(stage_index, int) or stage_index < 0:
        raise RuntimeReplayWatcherError("stage_index must be a non-negative integer")
    if expected_action_count is not None and (
        isinstance(expected_action_count, bool)
        or not isinstance(expected_action_count, int)
        or expected_action_count <= 0
    ):
        raise RuntimeReplayWatcherError(
            "expected_action_count must be a positive integer or None"
        )
    expected = expected_mode == "replay"
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    selected: list[dict[str, object]] = []
    runtime_groups: dict[object, list[dict[str, object]]] = {}
    runtime_group_first_sequence: dict[object, int] = {}
    legacy_current: list[dict[str, object]] = []
    complete_groups: list[tuple[int, object | None, list[dict[str, object]]]] = []
    parse_errors: list[dict[str, object]] = []
    reader = IncrementalShadowReader(source)
    rows, errors = reader.poll()
    parse_errors.extend(errors)
    for row in rows:
        if expected_pid is not None and row.get("pid") != expected_pid:
            continue
        if row.get("schema") != RECORDER_SCHEMA:
            continue
        body = row.get("body")
        if not isinstance(body, Mapping) or body.get("record") != "transition":
            continue
        mode = _body_mode(body)
        if mode != expected:
            continue
        runtime_sequence = RuntimeReplayStageWatcher._runtime_sequence(body)
        envelope_sequence = row.get("sequence")
        source_position = (
            int(envelope_sequence)
            if _is_int(envelope_sequence, minimum=1)
            else len(rows) + len(complete_groups)
        )
        if runtime_sequence is None:
            legacy_current.append(dict(row))
            if body.get("terminal") is True:
                complete_groups.append(
                    (source_position - len(legacy_current) + 1, None, legacy_current)
                )
                legacy_current = []
            continue
        group = runtime_groups.setdefault(runtime_sequence, [])
        runtime_group_first_sequence.setdefault(runtime_sequence, source_position)
        group.append(dict(row))

    for runtime_sequence, group in runtime_groups.items():
        terminal_positions = [
            index
            for index, row in enumerate(group)
            if isinstance(row.get("body"), Mapping)
            and row["body"].get("terminal") is True
        ]
        if terminal_positions == [len(group) - 1]:
            complete_groups.append(
                (
                    runtime_group_first_sequence[runtime_sequence],
                    runtime_sequence,
                    group,
                )
            )
    if parse_errors:
        raise RuntimeReplayWatcherError(
            f"runtime stage source has JSONL parse errors: {parse_errors[:3]!r}"
        )
    complete_groups.sort(key=lambda value: value[0])
    nonmatching_action_count_runtime_sequences: list[dict[str, object]] = []
    if expected_action_count is not None:
        nonmatching_action_count_runtime_sequences = [
            {
                "runtime_sequence": value[1],
                "transition_count": len(value[2]),
            }
            for value in complete_groups
            if len(value[2]) != expected_action_count
        ]
        complete_groups = [
            value
            for value in complete_groups
            if len(value[2]) == expected_action_count
        ]
        if len(complete_groups) > 1:
            raise RuntimeReplayWatcherError(
                "more than one runtime stage matches expected_action_count"
            )
    if stage_index < len(complete_groups):
        _position, selected_runtime_sequence, selected = complete_groups[stage_index]
    else:
        selected_runtime_sequence = None
    complete = bool(selected)
    if not complete:
        raise RuntimeReplayWatcherError("requested runtime stage is incomplete or absent")
    output = Path(output_path).resolve()
    if output == source:
        raise RuntimeReplayWatcherError("stage output must not overwrite recorder input")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            + "\n"
            for row in selected
        ),
        encoding="utf-8",
    )
    orders = [
        row["body"].get("action_order")
        for row in selected
        if isinstance(row.get("body"), Mapping)
    ]
    return {
        "source": str(source),
        "output": str(output),
        "expected_mode": expected_mode,
        "expected_action_count": expected_action_count,
        "stage_index": stage_index,
        "transition_count": len(selected),
        "first_action_order": orders[0] if orders else None,
        "last_action_order": orders[-1] if orders else None,
        "terminal": complete,
        "runtime_sequence": selected_runtime_sequence,
        "complete_runtime_sequence_count": len(complete_groups),
        "nonmatching_action_count_runtime_sequences": (
            nonmatching_action_count_runtime_sequences
        ),
        "incomplete_runtime_sequences": [
            value
            for value, group in runtime_groups.items()
            if not (
                group
                and isinstance(group[-1].get("body"), Mapping)
                and group[-1]["body"].get("terminal") is True
            )
        ],
        "reindexed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Incrementally watch runtime Exam shadow JSONL for one Replay stage."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="runtime recorder shadow JSONL")
    source.add_argument("--pid", type=int, help="game PID; uses the recorder's per-PID telemetry path")
    parser.add_argument("--expected-mode", choices=("replay", "live"), default="replay")
    parser.add_argument("--expected-action-count", type=int)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="seconds to wait")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    args = parser.parse_args(argv)
    try:
        report = watch_runtime_replay(
            args.input,
            pid=args.pid,
            expected_mode=args.expected_mode,
            expected_action_count=args.expected_action_count,
            timeout_seconds=args.timeout,
            poll_interval_seconds=args.poll_interval,
            chunk_bytes=args.chunk_bytes,
        )
    except RuntimeReplayWatcherError as error:
        parser.error(str(error))
        return 2
    if args.output is not None:
        write_runtime_replay_watch_report(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report.get("complete") is True else 2


def watch_runtime_replay_legal_candidates(*args: Any, **kwargs: Any) -> dict[str, object]:
    """Compatibility entry point for the focused legal-candidate probe.

    The implementation lives in its own module so the existing watcher stays
    small, while callers discovering the feature beside
    :func:`watch_runtime_replay` do not need to know the new module name.
    Importing lazily avoids a module cycle because the validator reuses this
    watcher's reader and stage selector.
    """

    from .runtime_replay_legal_candidate_probe import (
        watch_runtime_replay_legal_candidates as _watch,
    )

    return _watch(*args, **kwargs)


watch_runtime_replay_legal_candidate = watch_runtime_replay_legal_candidates


__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_TELEMETRY_DIR",
    "DEFAULT_TIMEOUT_SECONDS",
    "IncrementalShadowReader",
    "RECORDER_SCHEMA",
    "RuntimeReplayStageWatcher",
    "RuntimeReplayWatcherError",
    "SCHEMA",
    "WATCHER_VERSION",
    "default_shadow_path",
    "extract_first_runtime_stage",
    "main",
    "watch_runtime_replay_legal_candidate",
    "watch_runtime_replay_legal_candidates",
    "watch_runtime_replay",
    "write_runtime_replay_watch_report",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
