"""Incrementally validate native outer-produce observer receipts.

This consumer intentionally stays below policy/training code.  It validates
the append-only v3 receipt stream, pairs the two native hook boundaries, and
emits bounded *candidate* family/phase evidence.  It never promotes fixture
rows (or even a plausibly named runtime file) to an exact/runtime smoke pass.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from .runtime_replay_watcher import IncrementalShadowReader


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TELEMETRY_DIR = PROJECT_ROOT / "var" / "telemetry"
RECORDER_SCHEMA = "gkms.runtime-outer-observer.v3"
SCHEMA_VERSION = 3
BARRIER_RESULT_SCHEMA = "gkms.runtime-outer-observer.flush-barrier-result.v1"
REPORT_SCHEMA = "gkms.runtime-outer-observer-watch.v2"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_POLL_SECONDS = 0.25
DEFAULT_CHUNK_BYTES = 64 * 1024
_MAX_ERRORS = 64
_MAX_TRANSACTIONS = 32
_MAX_LOGS = 64
_MAX_IDENTITIES = 128

FAMILIES = (
    "lesson",
    "event",
    "present",
    "shop",
    "refresh",
    "business",
    "customize",
    "interval",
    "audition",
)
_FAMILY_TOKENS = {
    "lesson": ("lesson", "training", "レッスン"),
    "event": ("event", "story", "adv", "イベント"),
    "present": ("present", "gift", "fanpresent", "プレゼント"),
    "shop": ("shop", "shopping", "consult", "ショップ", "相談"),
    "refresh": ("refresh", "rest", "outing", "drink", "おでかけ", "休む"),
    "business": ("business", "work", "営業"),
    "customize": ("custom", "upgrade", "delete", "change", "強化", "削除"),
    "interval": ("interval", "間隔"),
    "audition": ("audition", "exam", "live", "オーディション", "試験"),
}
_PHASE_TOKENS = ("start", "select", "end")
_RUNTIME_NAME = re.compile(r"^runtime_outer_observer_(\d+)\.jsonl$")


class RuntimeOuterObserverWatcherError(ValueError):
    """Raised for invalid watcher arguments, not invalid observed rows."""


def default_outer_observer_path(pid: int) -> Path:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RuntimeOuterObserverWatcherError("pid must be a positive integer")
    return DEFAULT_TELEMETRY_DIR / f"runtime_outer_observer_{pid}.jsonl"


def _is_int(value: object, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _identity_reward(value: object, index: int) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    if (
        not _is_int(value.get("resource_type"))
        or not isinstance(value.get("resource_id"), str)
        or not _is_int(value.get("resource_level"))
        or not _is_int(value.get("quantity"))
    ):
        return None
    return {
        "index": index,
        "resource_type": value.get("resource_type"),
        "resource_id": value.get("resource_id"),
        "resource_level": value.get("resource_level"),
        "quantity": value.get("quantity"),
    }


def _identity_effect(value: object, index: int) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    nested = value.get("provided_rewards")
    origin = value.get("origin")
    scalar_names = (
        "max_stamina",
        "stamina",
        "produce_point",
        "vote_count",
        "star",
        "vocal",
        "dance",
        "visual",
        "high_score_gold",
    )
    scalar_fields = tuple(
        f"{side}_{name}"
        for name in scalar_names
        for side in ("before", "after")
    )
    if (
        not _is_int(value.get("effect_type"))
        or not isinstance(value.get("produce_effect_id"), str)
        or not isinstance(value.get("effect_value"), int)
        or isinstance(value.get("effect_value"), bool)
        or not isinstance(value.get("effect_target_id"), str)
        or not isinstance(origin, Mapping)
        or type(origin.get("present")) is not bool
        or any(
            not isinstance(value.get(field), int)
            or isinstance(value.get(field), bool)
            for field in scalar_fields
        )
        or type(value.get("ineffective")) is not bool
        or value.get("provided_rewards_complete") is not True
        or not isinstance(nested, list)
    ):
        return None
    rewards = [
        identity
        for position, item in enumerate(nested)
        if (identity := _identity_reward(item, position)) is not None
    ]
    if len(rewards) != len(nested):
        return None
    if origin.get("present") is True and (
        not _is_int(origin.get("origin_type"))
        or not isinstance(origin.get("origin_owner_id"), str)
        or not isinstance(origin.get("origin_id"), str)
        or not _is_int(origin.get("origin_level"))
    ):
        return None
    state_before = {
        name: value[f"before_{name}"] for name in scalar_names
    }
    state_after = {
        name: value[f"after_{name}"] for name in scalar_names
    }
    return {
        "index": index,
        "effect_type": value.get("effect_type"),
        "produce_effect_id": value.get("produce_effect_id"),
        "origin": dict(origin),
        "effect_value": value.get("effect_value"),
        "effect_target_id": value.get("effect_target_id"),
        "state_before": state_before,
        "state_after": state_after,
        "ineffective": value.get("ineffective"),
        "provided_rewards_complete": value.get("provided_rewards_complete"),
        "provided_rewards": rewards,
    }


def _strings(value: object, *, limit: int = 512) -> list[str]:
    result: list[str] = []

    def visit(item: object) -> None:
        if len(result) >= limit:
            return
        if isinstance(item, str) and item:
            result.append(item)
        elif isinstance(item, Mapping):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return result


class RuntimeOuterObserverAnchorCursor:
    """Expose one fail-closed byte/order anchor for an outer observer stream.

    The PID is bound by the recorder filename. Native v3 rows intentionally
    carry no PID, so this cursor never guesses provenance from row contents.
    One :class:`IncrementalShadowReader` is retained for the lifetime of the
    cursor and each ``snapshot`` reads only newly appended bytes.

    A file replacement or truncation starts a new stream generation. The
    generation is part of ``source_ref`` so begin/terminal anchors from two
    physical streams cannot be joined merely because their path is equal.
    Structural validation is reported as ``stream_complete`` only. Until the
    native writer exposes an external flush/watermark, ``complete`` remains
    false and this anchor must not be promoted to exact transition truth.
    """

    def __init__(
        self,
        expected_pid: int,
        *,
        telemetry_dir: str | Path = DEFAULT_TELEMETRY_DIR,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> None:
        if (
            isinstance(expected_pid, bool)
            or not isinstance(expected_pid, int)
            or expected_pid <= 0
        ):
            raise RuntimeOuterObserverWatcherError(
                "expected_pid must be a positive integer"
            )
        self.expected_pid = expected_pid
        self.path = (
            Path(telemetry_dir).expanduser()
            / f"runtime_outer_observer_{expected_pid}.jsonl"
        )
        self._resolved_path = self.path.resolve()
        self._reader = IncrementalShadowReader(
            self.path,
            chunk_bytes=chunk_bytes,
        )
        self._observed_reset_count = self._reader.reset_count
        self._file_missing = True
        self._reset_generation_state()

    def _reset_generation_state(self) -> None:
        self._last_order: int | None = None
        self._observer_ready = False
        self._rows_seen = 0
        self._generation_file_identity: tuple[int, int] | None = None
        self._prefix_sha256: str | None = None
        self._parse_errors: list[dict[str, object]] = []
        self._schema_errors: list[dict[str, object]] = []
        self._order_errors: list[dict[str, object]] = []
        self._ready_errors: list[dict[str, object]] = []
        self._source_errors: list[dict[str, object]] = []
        self._parse_error_count = 0
        self._schema_error_count = 0
        self._order_error_count = 0
        self._ready_error_count = 0
        self._source_error_count = 0

    @staticmethod
    def _append_error(
        values: list[dict[str, object]],
        value: Mapping[str, object],
    ) -> None:
        if len(values) < _MAX_ERRORS:
            values.append(dict(value))

    def _consume_parse_errors(
        self,
        errors: Sequence[Mapping[str, object]],
    ) -> None:
        for error in errors:
            self._parse_error_count += 1
            self._append_error(self._parse_errors, error)

    def _schema_error(
        self,
        code: str,
        *,
        order: object,
        actual: object,
    ) -> None:
        self._schema_error_count += 1
        self._append_error(
            self._schema_errors,
            {"code": code, "order": order, "actual": actual},
        )

    def _order_error(
        self,
        code: str,
        *,
        order: object,
        expected: int | None = None,
    ) -> None:
        self._order_error_count += 1
        error: dict[str, object] = {"code": code, "order": order}
        if expected is not None:
            error["expected"] = expected
        self._append_error(self._order_errors, error)

    def _ready_error(self, code: str, *, order: object) -> None:
        self._ready_error_count += 1
        self._append_error(
            self._ready_errors,
            {"code": code, "order": order},
        )

    def _source_error(self, code: str, *, detail: object = None) -> None:
        self._source_error_count += 1
        error: dict[str, object] = {"code": code}
        if detail is not None:
            error["detail"] = detail
        self._append_error(self._source_errors, error)

    def _hash_prefix(self, byte_offset: int) -> str | None:
        try:
            stat_before = self.path.stat()
            identity_before = IncrementalShadowReader._file_identity(stat_before)
            if int(stat_before.st_size) < byte_offset:
                self._source_error(
                    "prefix-shorter-than-byte-offset",
                    detail={
                        "byte_offset": byte_offset,
                        "file_size": int(stat_before.st_size),
                    },
                )
                return None
            if (
                self._generation_file_identity is not None
                and identity_before is not None
                and identity_before != self._generation_file_identity
            ):
                self._source_error(
                    "file-identity-changed-without-reader-reset",
                    detail={
                        "expected": list(self._generation_file_identity),
                        "actual": list(identity_before),
                    },
                )
                return None

            digest = hashlib.sha256()
            remaining = byte_offset
            with self.path.open("rb") as handle:
                while remaining:
                    chunk = handle.read(min(DEFAULT_CHUNK_BYTES, remaining))
                    if not chunk:
                        self._source_error(
                            "prefix-read-ended-early",
                            detail={
                                "byte_offset": byte_offset,
                                "remaining": remaining,
                            },
                        )
                        return None
                    digest.update(chunk)
                    remaining -= len(chunk)
            stat_after = self.path.stat()
            identity_after = IncrementalShadowReader._file_identity(stat_after)
            if (
                identity_before is not None
                and identity_after is not None
                and identity_before != identity_after
            ):
                self._source_error("file-identity-changed-during-prefix-read")
                return None
            if int(stat_after.st_size) < byte_offset:
                self._source_error(
                    "file-truncated-during-prefix-read",
                    detail={
                        "byte_offset": byte_offset,
                        "file_size": int(stat_after.st_size),
                    },
                )
                return None
        except OSError as error:
            self._source_error("prefix-read-failed", detail=_safe_error(error))
            return None

        self._generation_file_identity = identity_after
        return digest.hexdigest()

    def _consume(self, rows: Sequence[Mapping[str, object]]) -> None:
        for row in rows:
            self._rows_seen += 1
            order_value = row.get("order")
            order_valid = (
                isinstance(order_value, int)
                and not isinstance(order_value, bool)
                and order_value >= 0
            )
            if not order_valid:
                self._order_error("invalid-order", order=order_value)
                continue

            order = int(order_value)
            expected_order = (
                0 if self._last_order is None else self._last_order + 1
            )
            if order != expected_order:
                code = (
                    "order-does-not-start-at-zero"
                    if self._last_order is None
                    else "order-not-contiguous"
                )
                self._order_error(code, order=order, expected=expected_order)
            self._last_order = order

            schema_valid = row.get("schema") == RECORDER_SCHEMA
            version = row.get("schema_version")
            version_valid = type(version) is int and version == SCHEMA_VERSION
            if not schema_valid:
                self._schema_error(
                    "schema-mismatch",
                    order=order,
                    actual=row.get("schema"),
                )
            if not version_valid:
                self._schema_error(
                    "schema-version-mismatch",
                    order=order,
                    actual=version,
                )

            event = row.get("event")
            phase = row.get("phase")
            ready_row = event == "observer.ready" and phase == "ready"
            if order == 0 and not ready_row:
                self._ready_error("order-zero-not-ready", order=order)
            if event == "observer.ready":
                if phase != "ready":
                    self._ready_error("ready-phase-invalid", order=order)
                elif order != 0:
                    self._ready_error("ready-not-order-zero", order=order)
                elif self._observer_ready:
                    self._ready_error("duplicate-ready", order=order)
                elif schema_valid and version_valid:
                    self._observer_ready = True

    def _validate_barrier_result(
        self,
        barrier_result: object,
    ) -> tuple[Mapping[str, object] | None, list[dict[str, object]]]:
        errors: list[dict[str, object]] = []

        def mismatch(field: str, expected: object, actual: object) -> None:
            errors.append(
                {
                    "code": "barrier-field-invalid",
                    "field": field,
                    "expected": expected,
                    "actual": actual,
                }
            )

        if not isinstance(barrier_result, Mapping):
            mismatch("result", "mapping", type(barrier_result).__name__)
            return None, errors
        required = {
            "schema": BARRIER_RESULT_SCHEMA,
            "target_pid": self.expected_pid,
            "status": "ok",
            "native_status": "ok",
            "native_status_code": 0,
            "writer_error": "none",
            "writer_error_code": 0,
            "barrier_settled": True,
            "exact_prefix_available": True,
            "contract_errors": [],
        }
        for field, expected in required.items():
            actual = barrier_result.get(field)
            if field in {"target_pid", "native_status_code", "writer_error_code"}:
                valid = type(actual) is int and actual == expected
            elif field in {"barrier_settled", "exact_prefix_available"}:
                valid = type(actual) is bool and actual is expected
            else:
                valid = actual == expected
            if not valid:
                mismatch(field, expected, actual)

        raw_request = barrier_result.get("request")
        if not isinstance(raw_request, Mapping):
            mismatch("request", "mapping", type(raw_request).__name__)
            return None, errors

        aliases = {
            "expected_writer_generation": (
                "expected_writer_generation",
                "expected_generation",
            ),
            "minimum_next_enqueue_sequence": (
                "minimum_next_enqueue_sequence",
                "minimum_next",
            ),
            "status": ("status",),
            "writer_error": ("writer_error",),
            "writer_generation": ("writer_generation", "generation"),
            "target_next_enqueue_sequence": (
                "target_next_enqueue_sequence",
                "target_next",
            ),
            "flushed_next_enqueue_sequence": (
                "flushed_next_enqueue_sequence",
                "flushed_next",
            ),
            "watermark_next_physical_order": (
                "watermark_next_physical_order",
                "watermark_next_physical",
            ),
            "dropped_rows_at_target": (
                "dropped_rows_at_target",
                "dropped_at_target",
            ),
            "dropped_rows_now": ("dropped_rows_now", "dropped_now"),
        }
        request: dict[str, object] = {}
        for canonical, names in aliases.items():
            present = [raw_request[name] for name in names if name in raw_request]
            if not present:
                mismatch(f"request.{canonical}", "present", None)
                continue
            if any(value != present[0] for value in present[1:]):
                mismatch(
                    f"request.{canonical}",
                    "aliases-equal",
                    present,
                )
            request[canonical] = present[0]

        integer_fields = {
            "expected_writer_generation": 0,
            "minimum_next_enqueue_sequence": 0,
            "status": 0,
            "writer_error": 0,
            "writer_generation": 1,
            "target_next_enqueue_sequence": 1,
            "flushed_next_enqueue_sequence": 0,
            "watermark_next_physical_order": 0,
            "dropped_rows_at_target": 0,
            "dropped_rows_now": 0,
        }
        for field, minimum in integer_fields.items():
            if not _is_int(request.get(field), minimum=minimum):
                mismatch(field, f"integer>={minimum}", request.get(field))
        if errors:
            return request, errors

        writer_generation = int(request["writer_generation"])
        expected_generation = int(request["expected_writer_generation"])
        target = int(request["target_next_enqueue_sequence"])
        minimum_next = int(request["minimum_next_enqueue_sequence"])
        flushed = int(request["flushed_next_enqueue_sequence"])
        watermark = int(request["watermark_next_physical_order"])
        if request["status"] != 0:
            mismatch("request.status", 0, request["status"])
        if request["writer_error"] != 0:
            mismatch("request.writer_error", 0, request["writer_error"])
        if expected_generation not in {0, writer_generation}:
            mismatch(
                "request.expected_writer_generation",
                f"0-or-{writer_generation}",
                expected_generation,
            )
        if minimum_next > target:
            mismatch(
                "request.minimum_next_enqueue_sequence",
                f"<=target({target})",
                minimum_next,
            )
        if flushed < target:
            mismatch(
                "request.flushed_next_enqueue_sequence",
                f">=target({target})",
                flushed,
            )
        if watermark != target:
            mismatch(
                "request.watermark_next_physical_order",
                target,
                watermark,
            )
        if request["dropped_rows_at_target"] != 0:
            mismatch(
                "request.dropped_rows_at_target",
                0,
                request["dropped_rows_at_target"],
            )
        return request, errors

    def _scan_barrier_prefix(self, target: int) -> dict[str, object]:
        errors: list[dict[str, object]] = []
        digest = hashlib.sha256()
        byte_offset = 0
        rows_seen = 0
        observer_ready = False
        identity_before: tuple[int, int] | None = None
        identity_after: tuple[int, int] | None = None
        file_size = 0

        def prefix_error(
            code: str,
            *,
            order: int | None = None,
            detail: object = None,
        ) -> None:
            error: dict[str, object] = {"code": code}
            if order is not None:
                error["order"] = order
            if detail is not None:
                error["detail"] = detail
            errors.append(error)

        try:
            stat_before = self.path.stat()
            file_size = int(stat_before.st_size)
            identity_before = IncrementalShadowReader._file_identity(stat_before)
            with self.path.open("rb") as handle:
                for expected_order in range(target):
                    raw_line = handle.readline()
                    if not raw_line:
                        prefix_error(
                            "target-prefix-short",
                            order=expected_order,
                            detail={"target": target, "rows_seen": rows_seen},
                        )
                        break
                    digest.update(raw_line)
                    byte_offset += len(raw_line)
                    if not raw_line.endswith(b"\n"):
                        prefix_error(
                            "target-prefix-partial-line",
                            order=expected_order,
                        )
                        break
                    rows_seen += 1
                    payload = raw_line[:-1]
                    if payload.endswith(b"\r"):
                        payload = payload[:-1]
                    try:
                        encoding = "utf-8-sig" if expected_order == 0 else "utf-8"
                        row = json.loads(
                            payload.decode(encoding),
                            parse_constant=_reject_json_constant,
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                        prefix_error(
                            "target-prefix-json-invalid",
                            order=expected_order,
                            detail=_safe_error(error),
                        )
                        continue
                    if not isinstance(row, Mapping):
                        prefix_error(
                            "target-prefix-row-not-object",
                            order=expected_order,
                        )
                        continue
                    if row.get("schema") != RECORDER_SCHEMA:
                        prefix_error(
                            "target-prefix-schema-mismatch",
                            order=expected_order,
                            detail=row.get("schema"),
                        )
                    version = row.get("schema_version")
                    if type(version) is not int or version != SCHEMA_VERSION:
                        prefix_error(
                            "target-prefix-schema-version-mismatch",
                            order=expected_order,
                            detail=version,
                        )
                    order = row.get("order")
                    if type(order) is not int or order != expected_order:
                        prefix_error(
                            "target-prefix-order-mismatch",
                            order=expected_order,
                            detail={"actual": order},
                        )
                    if expected_order == 0 and not (
                        row.get("event") == "observer.ready"
                        and row.get("phase") == "ready"
                    ):
                        prefix_error("target-prefix-ready-missing", order=0)
                    elif expected_order == 0:
                        observer_ready = True
                    elif expected_order > 0 and row.get("event") == "observer.ready":
                        prefix_error(
                            "target-prefix-ready-duplicated",
                            order=expected_order,
                        )
            stat_after = self.path.stat()
            identity_after = IncrementalShadowReader._file_identity(stat_after)
            if (
                identity_before is not None
                and identity_after is not None
                and identity_before != identity_after
            ):
                prefix_error("target-prefix-file-replaced-during-scan")
            if int(stat_after.st_size) < byte_offset:
                prefix_error(
                    "target-prefix-file-truncated-during-scan",
                    detail={
                        "byte_offset": byte_offset,
                        "file_size": int(stat_after.st_size),
                    },
                )
            file_size = int(stat_after.st_size)
        except (FileNotFoundError, OSError) as error:
            prefix_error("target-prefix-read-failed", detail=_safe_error(error))

        complete = rows_seen == target and not errors
        file_identity = identity_after if identity_after is not None else identity_before
        return {
            "stream_complete": complete,
            "observer_ready": observer_ready,
            "byte_offset": byte_offset,
            "prefix_sha256": digest.hexdigest() if complete else None,
            "file_identity": (
                None
                if file_identity is None
                else {"device": file_identity[0], "inode": file_identity[1]}
            ),
            "rows_seen": rows_seen,
            "file_size": file_size,
            "trailing_bytes_ignored": max(0, file_size - byte_offset),
            "errors": errors,
        }

    def snapshot(self) -> dict[str, object]:
        """Poll new bytes once and return a JSON-compatible anchor mapping."""

        missing_polls_before = self._reader.missing_file_polls
        rows, parse_errors = self._reader.poll()
        self._file_missing = (
            self._reader.missing_file_polls > missing_polls_before
        )
        if self._reader.reset_count != self._observed_reset_count:
            self._reset_generation_state()
            self._observed_reset_count = self._reader.reset_count
        self._consume_parse_errors(parse_errors)
        self._consume(rows)

        error_count = (
            self._parse_error_count
            + self._schema_error_count
            + self._order_error_count
            + self._ready_error_count
            + self._source_error_count
        )
        structure_ready_for_hash = (
            not self._file_missing
            and self._reader.pending_bytes == 0
            and self._observer_ready
            and self._last_order is not None
            and error_count == 0
        )
        self._prefix_sha256 = (
            self._hash_prefix(self._reader.offset)
            if structure_ready_for_hash
            else None
        )
        error_count = (
            self._parse_error_count
            + self._schema_error_count
            + self._order_error_count
            + self._ready_error_count
            + self._source_error_count
        )
        stream_complete = (
            structure_ready_for_hash
            and self._prefix_sha256 is not None
            and error_count == 0
        )
        generation = self._reader.reset_count
        resolved_path = str(self._resolved_path)
        file_identity = self._generation_file_identity
        return {
            "source_ref": f"{resolved_path}#reset_count={generation}",
            "path": resolved_path,
            "expected_pid": self.expected_pid,
            "generation": generation,
            "byte_offset": self._reader.offset,
            "prefix_sha256": self._prefix_sha256,
            "file_identity": (
                None
                if file_identity is None
                else {"device": file_identity[0], "inode": file_identity[1]}
            ),
            "last_order": self._last_order,
            "next_order": (
                0 if self._last_order is None else self._last_order + 1
            ),
            "observer_ready": self._observer_ready,
            "structurally_complete": stream_complete,
            "stream_complete": stream_complete,
            "producer_barrier_complete": False,
            "complete": False,
            "blockers": [
                {
                    "code": "producer-barrier-unavailable",
                    "detail": (
                        "The native writer exposes no external flush or "
                        "watermark boundary."
                    ),
                }
            ],
            "file_missing": self._file_missing,
            "pending_bytes": self._reader.pending_bytes,
            "reset_count": self._reader.reset_count,
            "rows_seen": self._rows_seen,
            "bytes_read_total": self._reader.bytes_read_total,
            "poll_count": self._reader.poll_count,
            "error_count": error_count,
            "parse_error_count": self._parse_error_count,
            "schema_error_count": self._schema_error_count,
            "order_error_count": self._order_error_count,
            "ready_error_count": self._ready_error_count,
            "source_error_count": self._source_error_count,
            "parse_errors": [dict(value) for value in self._parse_errors],
            "schema_errors": [dict(value) for value in self._schema_errors],
            "order_errors": [dict(value) for value in self._order_errors],
            "ready_errors": [dict(value) for value in self._ready_errors],
            "source_errors": [dict(value) for value in self._source_errors],
        }

    def snapshot_barrier(
        self,
        barrier_result: Mapping[str, object],
    ) -> dict[str, object]:
        """Validate one externally settled native prefix barrier.

        This method performs no controller call, wait, retry, or polling. The
        supplied barrier result must already be terminal. Only the first
        ``target_next_enqueue_sequence`` complete JSONL rows are scanned;
        later rows cannot change the exactness of that durable prefix.
        """

        request, barrier_errors = self._validate_barrier_result(barrier_result)
        target_value = (
            request.get("target_next_enqueue_sequence")
            if isinstance(request, Mapping)
            else None
        )
        target = int(target_value) if _is_int(target_value, minimum=1) else None
        writer_generation_value = (
            request.get("writer_generation")
            if isinstance(request, Mapping)
            else None
        )
        writer_generation = (
            int(writer_generation_value)
            if _is_int(writer_generation_value, minimum=1)
            else None
        )
        if target is None:
            prefix = {
                "stream_complete": False,
                "observer_ready": False,
                "byte_offset": 0,
                "prefix_sha256": None,
                "file_identity": None,
                "rows_seen": 0,
                "file_size": 0,
                "trailing_bytes_ignored": 0,
                "errors": [{"code": "barrier-target-unavailable"}],
            }
        else:
            prefix = self._scan_barrier_prefix(target)

        producer_barrier_complete = not barrier_errors
        stream_complete = prefix["stream_complete"] is True
        complete = producer_barrier_complete and stream_complete
        blockers: list[dict[str, object]] = []
        if barrier_errors:
            blockers.append(
                {
                    "code": "producer-barrier-invalid",
                    "detail": [dict(value) for value in barrier_errors],
                }
            )
        prefix_errors = prefix["errors"]
        if isinstance(prefix_errors, list) and prefix_errors:
            blockers.append(
                {
                    "code": "observer-prefix-invalid",
                    "detail": [
                        dict(value)
                        for value in prefix_errors
                        if isinstance(value, Mapping)
                    ],
                }
            )

        resolved_path = str(self._resolved_path)
        source_generation = (
            str(writer_generation)
            if writer_generation is not None
            else "unverified"
        )
        return {
            "source_ref": (
                f"{resolved_path}#writer_generation={source_generation}"
            ),
            "path": resolved_path,
            "expected_pid": self.expected_pid,
            "writer_generation": writer_generation,
            "target_next_enqueue_sequence": target,
            "byte_offset": prefix["byte_offset"],
            "prefix_sha256": prefix["prefix_sha256"],
            "file_identity": prefix["file_identity"],
            "last_order": target - 1 if complete and target is not None else None,
            "next_order": target,
            "observer_ready": prefix["observer_ready"],
            "structurally_complete": stream_complete,
            "stream_complete": stream_complete,
            "producer_barrier_complete": producer_barrier_complete,
            "complete": complete,
            "rows_seen": prefix["rows_seen"],
            "trailing_bytes_ignored": prefix["trailing_bytes_ignored"],
            "barrier_errors": [dict(value) for value in barrier_errors],
            "prefix_errors": [
                dict(value)
                for value in prefix_errors
                if isinstance(value, Mapping)
            ],
            "blockers": blockers,
        }


class RuntimeOuterObserverWatcher:
    """Select and validate the first complete root SaveApiResultLog call."""

    def __init__(
        self,
        *,
        expected_pid: int | None = None,
        after_order: int = -1,
    ) -> None:
        if expected_pid is not None and (
            isinstance(expected_pid, bool)
            or not isinstance(expected_pid, int)
            or expected_pid <= 0
        ):
            raise RuntimeOuterObserverWatcherError(
                "expected_pid must be a positive integer or None"
            )
        if isinstance(after_order, bool) or not isinstance(after_order, int) or after_order < -1:
            raise RuntimeOuterObserverWatcherError(
                "after_order must be -1 or a non-negative integer"
            )
        self.expected_pid = expected_pid
        self.after_order = after_order
        self.stream_reset_count = 0
        self.reset_stream()

    def reset_stream(self) -> None:
        self.rows_seen = 0
        self.rows_after_complete = 0
        self.rows_ignored_pid = 0
        self.ready_count = 0
        self.errors: list[dict[str, object]] = []
        self.error_count = 0
        self.parse_errors: list[dict[str, object]] = []
        self.order_values: list[int] = []
        self._last_order: int | None = None
        self.root_transaction_id: int | None = None
        self.root_closed = False
        self.transactions: dict[int, dict[str, object]] = {}
        self.unowned_add_logs: list[dict[str, object]] = []
        self._selected_ids: set[int] = set()
        self._pending_logs: dict[tuple[int, int], list[dict[str, object]]] = {}
        self._all_evidence: list[object] = []
        self.stream_reset_count += 1

    def _error(self, code: str, *, order: object = None, detail: object = None) -> None:
        self.error_count += 1
        if len(self.errors) < _MAX_ERRORS:
            row: dict[str, object] = {"code": code}
            if order is not None:
                row["order"] = order
            if detail is not None:
                row["detail"] = detail
            self.errors.append(row)

    def add_parse_errors(self, errors: Sequence[Mapping[str, object]]) -> None:
        for error in errors:
            if len(self.parse_errors) < _MAX_ERRORS:
                self.parse_errors.append(dict(error))
            self._error("jsonl-parse-error", detail=dict(error))

    def _validate_order(self, value: object) -> None:
        if not _is_int(value):
            self._error("invalid-order", order=value)
            return
        order = int(value)
        self.order_values.append(order)
        if self._last_order is None:
            if order != 0:
                self._error("order-does-not-start-at-zero", order=order)
        elif order != self._last_order + 1:
            self._error(
                "order-not-contiguous",
                order=order,
                detail={"expected": self._last_order + 1},
            )
        self._last_order = order

    def _base_valid(self, row: Mapping[str, object]) -> bool:
        self._validate_order(row.get("order"))
        if row.get("schema") != RECORDER_SCHEMA:
            self._error("schema-mismatch", order=row.get("order"))
            return False
        if row.get("schema_version") != SCHEMA_VERSION:
            self._error("schema-version-mismatch", order=row.get("order"))
            return False
        for name in ("transaction_id", "parent_transaction_id", "hook_thread_id"):
            if not _is_int(row.get(name)):
                self._error(f"invalid-{name.replace('_', '-')}", order=row.get("order"))
                return False
        if not isinstance(row.get("event"), str) or row.get("phase") not in {
            "ready",
            "before",
            "after",
        }:
            self._error("invalid-event-envelope", order=row.get("order"))
            return False
        return True

    def _selected_transaction(self, tx: int, parent: int) -> bool:
        if self.root_transaction_id is None:
            return False
        return tx in self._selected_ids or parent in self._selected_ids

    def _new_transaction(self, row: Mapping[str, object]) -> None:
        tx = int(row["transaction_id"])
        parent = int(row["parent_transaction_id"])
        if self.root_transaction_id is None:
            if parent != 0:
                return
            self.root_transaction_id = tx
            self._selected_ids.add(tx)
        elif not self._selected_transaction(tx, parent):
            return
        if tx in self.transactions:
            self._error("duplicate-save-before", order=row.get("order"), detail={"transaction_id": tx})
            return
        self._selected_ids.add(tx)
        effects_raw = row.get("effects")
        rewards_raw = row.get("rewards")
        if not isinstance(effects_raw, list):
            self._error("effects-not-list", order=row.get("order"))
            effects_raw = []
        if not isinstance(rewards_raw, list):
            self._error("rewards-not-list", order=row.get("order"))
            rewards_raw = []
        effects = [
            identity
            for index, item in enumerate(effects_raw[:_MAX_IDENTITIES])
            if (identity := _identity_effect(item, index)) is not None
        ]
        rewards = [
            identity
            for index, item in enumerate(rewards_raw[:_MAX_IDENTITIES])
            if (identity := _identity_reward(item, index)) is not None
        ]
        if len(effects) != len(effects_raw):
            self._error("effect-identity-incomplete", order=row.get("order"))
        if len(rewards) != len(rewards_raw):
            self._error("reward-identity-incomplete", order=row.get("order"))
        complete_fields = {
            name: row.get(name)
            for name in (
                "clear_missions_complete",
                "effects_complete",
                "rewards_complete",
                "before_data_complete",
            )
        }
        for name, value in complete_fields.items():
            if type(value) is not bool or value is not True:
                self._error(
                    "capture-incomplete",
                    order=row.get("order"),
                    detail={"transaction_id": tx, "field": name, "value": value},
                )
        for effect in effects:
            if effect["provided_rewards_complete"] is not True:
                self._error(
                    "nested-rewards-incomplete",
                    order=row.get("order"),
                    detail={"transaction_id": tx, "effect_index": effect["index"]},
                )
        before_data = row.get("before_data")
        if not isinstance(before_data, Mapping):
            self._error("before-data-not-object", order=row.get("order"))
        transaction = {
            "transaction_id": tx,
            "parent_transaction_id": parent,
            "hook_thread_id": row["hook_thread_id"],
            "before_order": row.get("order"),
            "after_order": None,
            "paired": False,
            "complete_flags": complete_fields,
            "effect_count": len(effects_raw),
            "reward_count": len(rewards_raw),
            "effects": effects,
            "rewards": rewards,
            "add_logs": [],
        }
        self.transactions[tx] = transaction
        self._all_evidence.extend((effects, rewards))

    def _close_transaction(self, row: Mapping[str, object]) -> None:
        tx = int(row["transaction_id"])
        transaction = self.transactions.get(tx)
        if transaction is None:
            if self.root_transaction_id is not None and not self.root_closed:
                self._error("save-after-without-selected-before", order=row.get("order"), detail={"transaction_id": tx})
            return
        if transaction["paired"] is True:
            self._error("duplicate-save-after", order=row.get("order"), detail={"transaction_id": tx})
            return
        if row.get("parent_transaction_id") != transaction["parent_transaction_id"]:
            self._error("save-parent-mismatch", order=row.get("order"), detail={"transaction_id": tx})
        if row.get("hook_thread_id") != transaction["hook_thread_id"]:
            self._error("save-thread-mismatch", order=row.get("order"), detail={"transaction_id": tx})
        transaction["paired"] = True
        transaction["after_order"] = row.get("order")
        if tx == self.root_transaction_id:
            self.root_closed = True
            if self.ready_count != 1:
                self._error(
                    "ready-count-invalid",
                    order=row.get("order"),
                    detail={"observed": self.ready_count},
                )
            for selected_tx, selected in self.transactions.items():
                if selected["paired"] is not True:
                    self._error("nested-save-unclosed", order=row.get("order"), detail={"transaction_id": selected_tx})
            for (thread, selected_tx), pending in self._pending_logs.items():
                if pending:
                    self._error(
                        "add-log-unclosed",
                        order=row.get("order"),
                        detail={"transaction_id": selected_tx, "hook_thread_id": thread, "count": len(pending)},
                    )

    def _parse_log_record(
        self,
        row: Mapping[str, object],
        *,
        tx: int,
    ) -> dict[str, object]:
        log = row.get("log")
        if row.get("log_complete") is not True:
            self._error("log-capture-incomplete", order=row.get("order"), detail={"transaction_id": tx})
        if not isinstance(log, Mapping):
            self._error("log-not-object", order=row.get("order"), detail={"transaction_id": tx})
            log = {}
        if log.get("present") is not True:
            self._error("log-not-present", order=row.get("order"), detail={"transaction_id": tx})
        details = log.get("details")
        if log.get("details_complete") is not True or not isinstance(details, list):
            self._error("log-details-incomplete", order=row.get("order"), detail={"transaction_id": tx})
            details = []
        for detail_index, detail in enumerate(details):
            if not isinstance(detail, Mapping) or detail.get("lines_complete") is not True or not isinstance(detail.get("lines"), list):
                self._error(
                    "log-lines-incomplete",
                    order=row.get("order"),
                    detail={"transaction_id": tx, "detail_index": detail_index},
                )
        record = {
            "transaction_id": tx,
            "parent_transaction_id": row.get("parent_transaction_id"),
            "hook_thread_id": row.get("hook_thread_id"),
            "before_order": row.get("order"),
            "after_order": None,
            "paired": False,
            "log_count_before": row.get("log_count_before"),
            "log_count_after": None,
            "log_complete": row.get("log_complete"),
            "identity": {
                name: log.get(name)
                for name in (
                    "cell_type",
                    "current_turn",
                    "trigger_id",
                    "trigger_subscription_number",
                    "step_type",
                    "parameter_type",
                    "produce_type",
                    "split_type",
                    "adv_index",
                )
            },
            "detail_count": len(details),
        }
        self._all_evidence.append(log)
        return record

    def _log_before(self, row: Mapping[str, object]) -> None:
        tx = int(row["transaction_id"])
        transaction = self.transactions.get(tx)
        if tx in self._selected_ids:
            if transaction is None:
                self._error("add-log-owner-before-missing", order=row.get("order"), detail={"transaction_id": tx})
                return
            if row.get("parent_transaction_id") != transaction["parent_transaction_id"]:
                self._error("add-log-parent-mismatch", order=row.get("order"), detail={"transaction_id": tx})
        elif tx != 0:
            if self.root_transaction_id is not None and not self.root_closed:
                self._error("add-log-not-owned-by-selected-save", order=row.get("order"), detail={"transaction_id": tx})
            return

        record = self._parse_log_record(row, tx=tx)
        add_logs = (
            transaction["add_logs"]
            if transaction is not None
            else self.unowned_add_logs
        )
        if isinstance(add_logs, list):
            if len(add_logs) < _MAX_LOGS:
                add_logs.append(record)
            else:
                self._error("add-log-report-limit-exceeded", order=row.get("order"))
        key = (int(row["hook_thread_id"]), tx)
        self._pending_logs.setdefault(key, []).append(record)

    def _log_after(self, row: Mapping[str, object]) -> None:
        tx = int(row["transaction_id"])
        if tx not in self._selected_ids and tx != 0:
            return
        key = (int(row["hook_thread_id"]), tx)
        pending = self._pending_logs.get(key)
        if not pending:
            self._error("add-log-after-without-before", order=row.get("order"), detail={"transaction_id": tx})
            return
        record = pending.pop()
        record["paired"] = True
        record["after_order"] = row.get("order")
        record["log_count_after"] = row.get("log_count_after")
        before = record.get("log_count_before")
        after = row.get("log_count_after")
        if row.get("log_count_before") != before:
            self._error("log-count-before-mismatch", order=row.get("order"), detail={"transaction_id": tx})
        if not _is_int(before) or not _is_int(after) or int(after) != int(before) + 1:
            self._error(
                "log-count-transition-invalid",
                order=row.get("order"),
                detail={"transaction_id": tx, "before": before, "after": after},
            )

    def consume(self, rows: Sequence[Mapping[str, object]]) -> None:
        for row in rows:
            self.rows_seen += 1
            if self.expected_pid is not None and row.get("pid") not in {None, self.expected_pid}:
                self.rows_ignored_pid += 1
                continue
            if self.root_closed:
                self.rows_after_complete += 1
                continue
            if not self._base_valid(row):
                continue
            event = row["event"]
            phase = row["phase"]
            if event == "observer.ready":
                if phase != "ready":
                    self._error("ready-phase-invalid", order=row.get("order"))
                if self.ready_count != 0:
                    self._error("duplicate-ready", order=row.get("order"))
                self.ready_count += 1
            elif int(row["order"]) <= self.after_order:
                continue
            elif event == "ProduceApiUtility.SaveApiResultLog" and phase == "before":
                self._new_transaction(row)
            elif event == "ProduceApiUtility.SaveApiResultLog" and phase == "after":
                self._close_transaction(row)
            elif event == "ProducePlayLogSaveData.AddLog" and phase == "before":
                self._log_before(row)
            elif event == "ProducePlayLogSaveData.AddLog" and phase == "after":
                self._log_after(row)
            else:
                self._error("unknown-event-phase", order=row.get("order"), detail={"event": event, "phase": phase})

    def _family_evidence(self) -> dict[str, object]:
        values = _strings(self._all_evidence)
        folded = [(value, value.casefold()) for value in values]
        families: dict[str, object] = {}
        for family in FAMILIES:
            matches: list[dict[str, str]] = []
            for value, lowered in folded:
                for token in _FAMILY_TOKENS[family]:
                    if token.casefold() in lowered:
                        matches.append({"token": token, "value": value[:256]})
                        break
                if len(matches) >= 16:
                    break
            families[family] = {
                "status": "candidate" if matches else "unobserved",
                "exact": False,
                "matches": matches,
            }
        phase_matches = sorted(
            {
                token
                for _value, lowered in folded
                for token in _PHASE_TOKENS
                if token in lowered
            }
        )
        return {
            "classification": "consumer-side-candidate-only",
            "exact": False,
            "families": families,
            "phase": {
                "status": "candidate" if phase_matches else "unresolved",
                "exact": False,
                "matched_tokens": phase_matches,
                "note": "before/after is hook timing, not the official endpoint phase",
            },
        }

    def report(self, *, path: str | Path, timed_out: bool = False, reader: IncrementalShadowReader | None = None) -> dict[str, object]:
        tx_rows = list(self.transactions.values())
        paired = sum(1 for transaction in tx_rows if transaction["paired"] is True)
        logs = [
            *self.unowned_add_logs,
            *(log for transaction in tx_rows for log in transaction["add_logs"]),
        ]
        log_paired = sum(1 for log in logs if log["paired"] is True)
        passed = self.root_closed and self.error_count == 0
        source_name = Path(path).name
        runtime_name_match = _RUNTIME_NAME.fullmatch(source_name)
        blockers: list[dict[str, str]] = [
            {
                "code": "event-request-identity-missing",
                "detail": "Event suggestion index/id is not captured by the two shared receipt hooks.",
            },
            {
                "code": "business-request-identity-missing",
                "detail": "Business Start/Select request identity is not captured by the two shared receipt hooks.",
            },
            {
                "code": "endpoint-phase-identity-unproven",
                "detail": "Receipt before/after phases do not prove official Start/Select/End identity.",
            },
        ]
        if self.unowned_add_logs:
            blockers.append(
                {
                    "code": "add-log-save-association-unproven",
                    "detail": (
                        "Observed AddLog pairs with transaction_id=0 outside "
                        "SaveApiResultLog; physical order is exact but their "
                        "owning endpoint/Save transaction is not yet proven."
                    ),
                }
            )
        result: dict[str, object] = {
            "schema": REPORT_SCHEMA,
            "source": str(Path(path).resolve()),
            "status": "complete" if passed else ("invalid" if self.error_count else ("timeout" if timed_out else "waiting")),
            "complete": self.root_closed,
            "passed": passed,
            "root_transaction_id": self.root_transaction_id,
            "validation": {
                "schema_expected": RECORDER_SCHEMA,
                "schema_version_expected": SCHEMA_VERSION,
                "order_contiguous": not any(error["code"].startswith("order-") or error["code"] == "invalid-order" for error in self.errors),
                "save_pairs": {"observed": len(tx_rows), "paired": paired},
                "add_log_pairs": {"observed": len(logs), "paired": log_paired},
                "error_count": self.error_count,
                "errors": self.errors,
                "errors_truncated": self.error_count > len(self.errors),
            },
            "transactions": tx_rows[:_MAX_TRANSACTIONS],
            "transactions_truncated": len(tx_rows) > _MAX_TRANSACTIONS,
            "unowned_add_logs": self.unowned_add_logs[:_MAX_LOGS],
            "unowned_add_logs_truncated": len(self.unowned_add_logs) > _MAX_LOGS,
            "family_phase_evidence": self._family_evidence(),
            "blockers": blockers,
            "source_assurance": {
                "runtime_filename_candidate": runtime_name_match is not None,
                "filename_pid": int(runtime_name_match.group(1)) if runtime_name_match else None,
                "runtime_smoke_pass": False,
                "offline_fixture_promoted": False,
                "note": "This watcher validates receipt structure only; provenance requires an isolated real-runtime smoke.",
            },
            "promotion": {"allowed": False, "exact": False},
            "scan": {
                "after_order": self.after_order,
                "rows_seen": self.rows_seen,
                "rows_ignored_pid": self.rows_ignored_pid,
                "rows_after_complete": self.rows_after_complete,
                "ready_count": self.ready_count,
                "first_order": self.order_values[0] if self.order_values else None,
                "last_order": self.order_values[-1] if self.order_values else None,
                "parse_errors": self.parse_errors,
            },
        }
        if reader is not None:
            result["reader"] = {
                "offset": reader.offset,
                "pending_bytes": reader.pending_bytes,
                "bytes_read_total": reader.bytes_read_total,
                "poll_count": reader.poll_count,
                "missing_file_polls": reader.missing_file_polls,
                "reset_count": reader.reset_count,
                "read_sizes": reader.read_sizes[-64:],
            }
        return result


def watch_runtime_outer_observer(
    input_path: str | Path | None = None,
    *,
    pid: int | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    after_order: int = -1,
) -> dict[str, object]:
    if (input_path is None) == (pid is None):
        raise RuntimeOuterObserverWatcherError("provide exactly one of input_path or pid")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds < 0:
        raise RuntimeOuterObserverWatcherError("timeout_seconds must be non-negative")
    if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or poll_seconds <= 0:
        raise RuntimeOuterObserverWatcherError("poll_seconds must be positive")
    path = default_outer_observer_path(pid) if pid is not None else Path(input_path).expanduser()  # type: ignore[arg-type]
    reader = IncrementalShadowReader(path, chunk_bytes=chunk_bytes)
    watcher = RuntimeOuterObserverWatcher(
        expected_pid=pid,
        after_order=after_order,
    )
    observed_reset_count = reader.reset_count
    deadline = time.monotonic() + float(timeout_seconds)
    timed_out = False
    while True:
        rows, errors = reader.poll()
        if reader.reset_count != observed_reset_count:
            watcher.reset_stream()
            observed_reset_count = reader.reset_count
        watcher.add_parse_errors(errors)
        watcher.consume(rows)
        if watcher.root_closed:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(min(float(poll_seconds), max(0.0, deadline - time.monotonic())))
    return watcher.report(path=path, timed_out=timed_out, reader=reader)


def write_runtime_outer_observer_report(report: Mapping[str, object], output_path: str | Path) -> Path:
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Incrementally validate runtime outer-observer v3 JSONL receipts.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--pid", type=int)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--poll", "--poll-interval", dest="poll", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    parser.add_argument(
        "--after-order",
        type=int,
        default=-1,
        help="ignore completed events through this physical order",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = watch_runtime_outer_observer(
            args.input,
            pid=args.pid,
            timeout_seconds=args.timeout,
            poll_seconds=args.poll,
            chunk_bytes=args.chunk_bytes,
            after_order=args.after_order,
        )
    except RuntimeOuterObserverWatcherError as error:
        parser.error(str(error))
    if args.output is not None:
        write_runtime_outer_observer_report(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report.get("passed") is True else 2


__all__ = [
    "BARRIER_RESULT_SCHEMA",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "FAMILIES",
    "RECORDER_SCHEMA",
    "REPORT_SCHEMA",
    "RuntimeOuterObserverAnchorCursor",
    "RuntimeOuterObserverWatcher",
    "RuntimeOuterObserverWatcherError",
    "default_outer_observer_path",
    "main",
    "watch_runtime_outer_observer",
    "write_runtime_outer_observer_report",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
