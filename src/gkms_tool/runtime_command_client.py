"""Client for the game's managed-thread command bridge.

The bridge owns game access. This client only publishes bounded JSON requests
and correlates their receipts. A timeout or cancellation never retries or
deletes an operation: the game may already have accepted it.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
import uuid

from .application_paths import native_state_root
DEFAULT_BRIDGE_ROOT = native_state_root() / "runtime_command_bridge"
REQUEST_SCHEMA = "gkms.runtime-command.v1"
RESULT_SCHEMA = "gkms.runtime-command-result.v1"
STATUS_SCHEMA = "gkms.runtime-command-status.v1"
READ_COMMANDS = frozenset({"status", "read_snapshot", "read_inventory", "read_model_context", "read_pc_contracts", "read_loadout", "read_outer_snapshot", "read_diagnostic",
    "official_replay.inspect", "official_replay.poll"})
WRITE_COMMANDS = frozenset({"exam.play", "exam.drink", "exam.end_turn", "loadout.apply", "outer.action",
    "official_replay.prepare", "official_replay.start", "official_replay.release"})
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RESULT_BYTES = 32 * 1024 * 1024


def _claim_lock():
    from .controller_client import _serialized_controller_request
    return _serialized_controller_request(2.0, mutex_name=r"Local\gkms_tool.runtime_command.claim.v1")


def input_backend() -> str:
    """Select the explicit execution backend; DLL never silently falls back."""
    backend = os.environ.get("GKMS_INPUT_BACKEND")
    if backend is None:
        settings_path = DEFAULT_BRIDGE_ROOT / "settings.json"
        backend = _read_object(settings_path).get("input_backend", "dll") if settings_path.is_file() else "dll"
    from .application_paths import public_installation
    if public_installation() and backend != "dll":
        raise RuntimeCommandProtocolError("unsupported-public-backend: public builds require dll")
    if backend not in {"dll", "maa"}:
        raise RuntimeCommandProtocolError("input_backend must be dll or maa")
    return backend


class RuntimeCommandError(RuntimeError):
    pass


class RuntimeCommandUnavailable(RuntimeCommandError):
    pass


class RuntimeCommandProtocolError(RuntimeCommandError):
    pass


class RuntimeCommandPending(RuntimeCommandError):
    """Submission outcome is unknown; resume by request ID, never resubmit."""

    def __init__(self, request: "RuntimeCommandRequest", reason: str):
        self.request = request
        self.reason = reason
        super().__init__(f"{request.command}: {reason}; request_id={request.request_id}")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise RuntimeCommandProtocolError(f"invalid {name}")
    return value


def _read_object(path: Path, *, limit: int = _MAX_RESULT_BYTES) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise RuntimeCommandProtocolError(f"bridge file exceeds size limit: {path.name}")
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise RuntimeCommandProtocolError(f"invalid bridge JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise RuntimeCommandProtocolError(f"bridge file must contain an object: {path.name}")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeCommandRequest:
    request_id: str
    session_generation: str
    command: str
    expected_revision: int | str | None = None
    target: Mapping[str, object] | None = None
    continuation_of: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.request_id, "request_id")
        _identifier(self.session_generation, "session_generation")
        if self.command not in READ_COMMANDS | WRITE_COMMANDS:
            raise RuntimeCommandProtocolError("unsupported bridge command")
        if self.command in WRITE_COMMANDS:
            revision = self.expected_revision
            if not ((type(revision) is int and revision >= 0) or
                    (isinstance(revision, str) and bool(revision) and len(revision) <= 128)):
                raise RuntimeCommandProtocolError("mutating command requires a snapshot revision")
        if self.target is not None and not isinstance(self.target, Mapping):
            raise RuntimeCommandProtocolError("target must be an object")
        if self.continuation_of is not None:
            _identifier(self.continuation_of, "continuation_of")
            if (self.command != "outer.action" or not self.target or
                    self.target.get("exam_continuation") is not True or
                    not isinstance(self.target.get("parent_context"), Mapping)):
                raise RuntimeCommandProtocolError("continuation requires an observed Exam selector target")
        if self.command == "exam.play":
            self._require_slot()
            if not isinstance((self.target or {}).get("card_guid"), str) or not self.target["card_guid"]:
                raise RuntimeCommandProtocolError("play requires card_guid")
        elif self.command == "exam.drink":
            self._require_slot()
            if not isinstance((self.target or {}).get("drink_id"), str) or not self.target["drink_id"]:
                raise RuntimeCommandProtocolError("drink requires drink_id")

    def _require_slot(self) -> None:
        slot = (self.target or {}).get("slot")
        if type(slot) is not int or slot < 0:
            raise RuntimeCommandProtocolError("command requires a non-negative slot")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": REQUEST_SCHEMA, "request_id": self.request_id,
            "session_generation": self.session_generation, "command": self.command,
        }
        if self.expected_revision is not None:
            value["expected_revision"] = self.expected_revision
        if self.target is not None:
            value["target"] = dict(self.target)
        if self.continuation_of is not None:
            value["continuation_of"] = self.continuation_of
        return value


@dataclass(frozen=True, slots=True)
class RuntimeCommandResult:
    request: RuntimeCommandRequest
    status: str
    raw: Mapping[str, Any]
    path: Path

    @property
    def submitted(self) -> bool:
        return self.status == "submitted"

    @property
    def snapshot(self) -> Mapping[str, Any] | None:
        value = self.raw.get("snapshot")
        return value if isinstance(value, Mapping) else None

    def require_ok(self) -> "RuntimeCommandResult":
        if self.status not in {"ok", "submitted"}:
            code = str(self.raw.get("error_code", self.status))
            detail = self.raw.get("error_message")
            raise RuntimeCommandError(code + (f": {detail}" if detail else ""))
        return self


class RuntimeCommandClient:
    def __init__(
        self, root: str | Path = DEFAULT_BRIDGE_ROOT, *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = Path(root)
        self.monotonic = monotonic
        self.sleep = sleep

    def read_status(self) -> Mapping[str, Any]:
        deadline = self.monotonic() + 1.0
        while True:
            try:
                status = _read_object(self.root / "status.json", limit=_MAX_REQUEST_BYTES)
                break
            except FileNotFoundError as exc:
                raise RuntimeCommandUnavailable("DLL command bridge is not running") from exc
            except PermissionError as exc:
                if self.monotonic() >= deadline:
                    raise RuntimeCommandUnavailable("DLL heartbeat file remained unavailable") from exc
                self.sleep(min(0.01, max(0.0, deadline - self.monotonic())))
        if status.get("schema") != STATUS_SCHEMA or status.get("protocol_version") != 1:
            raise RuntimeCommandProtocolError("DLL command bridge protocol differs")
        _identifier(status.get("session_generation"), "session_generation")
        if type(status.get("pid")) is not int or status["pid"] <= 0:
            raise RuntimeCommandProtocolError("DLL command bridge PID is invalid")
        timestamp = status.get("updated_at")
        if timestamp is not None:
            try:
                updated = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    raise ValueError("timezone required")
            except (AttributeError, TypeError, ValueError) as exc:
                raise RuntimeCommandProtocolError("DLL heartbeat timestamp is invalid") from exc
            if (datetime.now(timezone.utc) - updated).total_seconds() > 10.0:
                raise RuntimeCommandUnavailable("DLL heartbeat expired; enter the running game before retrying")
        capabilities = status.get("capabilities")
        if not isinstance(capabilities, list) or any(not isinstance(v, str) for v in capabilities):
            raise RuntimeCommandProtocolError("DLL capabilities must be a string array")
        return status

    def make_request(
        self, command: str, *, target: Mapping[str, object] | None = None,
        expected_revision: int | str | None = None,
        continuation_of: str | None = None,
    ) -> RuntimeCommandRequest:
        status = self.read_status()
        if command != "status" and command not in status["capabilities"]:
            raise RuntimeCommandUnavailable(f"DLL capability is unavailable: {command}")
        return RuntimeCommandRequest(
            uuid.uuid4().hex, status["session_generation"], command,
            expected_revision, target,
            continuation_of,
        )

    def submit(self, request: RuntimeCommandRequest) -> None:
        status = self.read_status()
        if status["session_generation"] != request.session_generation:
            raise RuntimeCommandUnavailable("DLL process generation changed before submission")
        if request.command != "status" and request.command not in status["capabilities"]:
            raise RuntimeCommandUnavailable(f"DLL capability is unavailable: {request.command}")
        raw = json.dumps(request.to_dict(), ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
        if len(raw) > _MAX_REQUEST_BYTES:
            raise RuntimeCommandProtocolError("request exceeds bridge size limit")
        if request.continuation_of is not None:
            with _claim_lock():
                try:
                    parent = _read_object(self.root / "pending_action.json", limit=_MAX_REQUEST_BYTES)
                except FileNotFoundError as exc:
                    raise RuntimeCommandUnavailable("Exam continuation has no pending parent action") from exc
                if (parent.get("request_id") != request.continuation_of or
                        parent.get("session_generation") != request.session_generation or
                        parent.get("command") not in {"exam.play", "exam.drink", "exam.end_turn"}):
                    raise RuntimeCommandUnavailable("Exam continuation does not belong to the pending action")
        elif request.command in WRITE_COMMANDS:
            # The claim spans all domains and survives the controller mutex
            # being released after a timeout. No Outer/loadout action may
            # overtake an unresolved Exam action, or vice versa.
            with _claim_lock():
                self.root.mkdir(parents=True, exist_ok=True)
                try:
                    with (self.root / "pending_action.json").open("xb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                except FileExistsError as exc:
                    pending = _read_object(self.root / "pending_action.json", limit=_MAX_REQUEST_BYTES)
                    if pending.get("request_id") == request.request_id:
                        raise RuntimeCommandPending(request, "already submitted; await the existing receipt") from exc
                    raise RuntimeCommandUnavailable(
                        f"another DLL action is pending: {pending.get('command')} ({pending.get('request_id')})"
                    ) from exc
        # A durable host descriptor is also the atomic idempotency reservation.
        # It persists on timeout/restart so a caller cannot re-enqueue a mutation.
        requests = self.root / "requests"
        requests.mkdir(parents=True, exist_ok=True)
        descriptor = requests / f"{request.request_id}.json"
        try:
            with descriptor.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            self.release_action(request)
            raise RuntimeCommandPending(request, "already submitted; await the existing receipt") from exc
        inbox = self.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=inbox, suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, inbox / f"{request.request_id}.json")
        except Exception as exc:
            raise RuntimeCommandPending(request, "publication interrupted; inspect the existing request") from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def release_action(self, request: RuntimeCommandRequest) -> None:
        """Retire only this action's claim after rejection or proven settlement."""
        if request.command not in WRITE_COMMANDS or request.continuation_of is not None:
            return
        with _claim_lock():
            path = self.root / "pending_action.json"
            try:
                pending = _read_object(path, limit=_MAX_REQUEST_BYTES)
            except FileNotFoundError:
                return
            if (pending.get("request_id") == request.request_id and
                    pending.get("session_generation") == request.session_generation and
                    pending.get("command") == request.command):
                path.unlink()

    def poll_result(self, request: RuntimeCommandRequest) -> RuntimeCommandResult | None:
        path = self.root / "results" / f"{request.request_id}.json"
        try:
            result = _read_object(path)
        except (FileNotFoundError, PermissionError):
            # Windows can briefly expose the renamed result while its writer
            # still owns a non-shared handle. Retry reading this receipt only.
            return None
        if (result.get("schema") != RESULT_SCHEMA or
                result.get("request_id") != request.request_id or
                result.get("session_generation") != request.session_generation):
            raise RuntimeCommandProtocolError("DLL receipt does not match its request")
        outcome = result.get("status")
        if outcome not in {"ok", "submitted", "rejected", "unknown"}:
            raise RuntimeCommandProtocolError("DLL receipt status is invalid")
        if request.command in WRITE_COMMANDS and outcome == "ok":
            raise RuntimeCommandProtocolError("mutating receipt must distinguish submission from completion")
        if outcome == "rejected":
            self.release_action(request)
        return RuntimeCommandResult(request, outcome, result, path)

    def await_result(
        self, request: RuntimeCommandRequest, *, timeout: float = 15.0,
        cancelled: Callable[[], bool] | None = None,
    ) -> RuntimeCommandResult:
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and non-negative")
        deadline = self.monotonic() + timeout
        while True:
            result = self.poll_result(request)
            if result is not None:
                return result
            if cancelled is not None and cancelled():
                raise RuntimeCommandPending(request, "cancelled while awaiting receipt")
            if self.monotonic() >= deadline:
                raise RuntimeCommandPending(request, "receipt timeout")
            self.sleep(min(0.05, max(0.0, deadline - self.monotonic())))

    def execute(self, command: str, *, target: Mapping[str, object] | None = None,
                expected_revision: int | str | None = None, timeout: float = 15.0,
                cancelled: Callable[[], bool] | None = None,
                continuation_of: str | None = None) -> RuntimeCommandResult:
        request = self.make_request(command, target=target, expected_revision=expected_revision, continuation_of=continuation_of)
        self.submit(request)
        return self.await_result(request, timeout=timeout, cancelled=cancelled)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read the managed DLL command bridge")
    parser.add_argument("command", choices=sorted(READ_COMMANDS))
    parser.add_argument("--root", type=Path, default=DEFAULT_BRIDGE_ROOT)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    result = RuntimeCommandClient(args.root).execute(args.command, timeout=args.timeout)
    print(json.dumps(result.raw, ensure_ascii=False, indent=2))
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
