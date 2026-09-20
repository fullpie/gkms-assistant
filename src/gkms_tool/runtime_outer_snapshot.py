"""Shared read-only Outer DTO validation and correlated snapshot reading.

Maintenance and gameplay use exactly this reader. Policy selection and game
mutation remain in runtime_outer_runner and are not privileged dependencies.
"""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import time
from .runtime_command_client import (RuntimeCommandClient, RuntimeCommandError,
    RuntimeCommandPending, RuntimeCommandProtocolError)
from .training_artifact_io import canonical_json_bytes

@dataclass(frozen=True, slots=True)
class RuntimeOuterSnapshot:
    session_generation: str
    raw: Mapping[str, Any]
    source_path: Path

    def __post_init__(self):
        if self.raw.get("schema") != "gkms.outer-runtime-snapshot.v1":
            raise RuntimeCommandProtocolError("native Outer snapshot schema differs")
        if not isinstance(self.raw.get("revision"), str) or not self.raw["revision"]:
            raise RuntimeCommandProtocolError("native Outer revision is absent")
        for key in ("surface", "screen_type"):
            if not isinstance(self.raw.get(key), str) or not self.raw[key]:
                raise RuntimeCommandProtocolError(f"native Outer {key} is absent")
        if type(self.raw.get("busy")) is not bool or type(self.raw.get("actions_complete")) is not bool:
            raise RuntimeCommandProtocolError("native Outer readiness is incomplete")
        actions = self.raw.get("legal_actions")
        if not isinstance(actions, list):
            raise RuntimeCommandProtocolError("native Outer candidate set is absent")
        targets: set[bytes] = set()
        for action in actions:
            if not isinstance(action, Mapping) or not isinstance(action.get("target"), Mapping):
                raise RuntimeCommandProtocolError("native Outer candidate is invalid")
            if not isinstance(action.get("action_id"), str) or not action["action_id"]:
                raise RuntimeCommandProtocolError("native Outer action ID is absent")
            target = canonical_json_bytes(action["target"])
            if target in targets:
                raise RuntimeCommandProtocolError("native Outer candidates have duplicate targets")
            targets.add(target)

    @property
    def revision(self) -> str:
        return self.raw["revision"]

    @property
    def actions(self) -> tuple[Mapping[str, Any], ...]:
        if self.raw["busy"] or not self.raw["actions_complete"]:
            return ()
        return tuple(self.raw["legal_actions"])


class RuntimeOuterReader:
    def __init__(self, client=None, *, timeout=30.0, cancelled=None,
                 monotonic=time.monotonic, sleep=time.sleep):
        self.client = client or RuntimeCommandClient()
        self.timeout, self.cancelled = timeout, cancelled
        self.monotonic, self.sleep = monotonic, sleep

    def read(self) -> RuntimeOuterSnapshot:
        deadline = self.monotonic() + self.timeout
        last_unavailable = None
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0 and self.timeout > 0:
                if last_unavailable is not None:
                    last_unavailable.require_ok()
                raise RuntimeCommandError("native Outer read deadline expired before another request")
            try:
                # Explicit timeout=0 retains its one nonblocking observation;
                # an exhausted positive budget never starts another request.
                result = self.client.execute("read_outer_snapshot", timeout=max(0.0, min(15.0, remaining)), cancelled=self.cancelled)
            except RuntimeCommandPending as pending:
                # A delayed read is still that same request. Never publish a
                # replacement merely because its first receipt wait expired.
                remaining = deadline - self.monotonic()
                if pending.request.command != "read_outer_snapshot" or remaining <= 0 or (
                        self.cancelled is not None and self.cancelled()):
                    raise
                result = self.client.await_result(pending.request, timeout=remaining, cancelled=self.cancelled)
            # The game's Window root can briefly have no active top screen
            # during a normal scene change. Retry this read, never its action.
            changing_scene = (result.status == "rejected"
                              and result.raw.get("error_message") in {
                                  "native-top-screen-not-active", "native-screen-root-unavailable",
                                  "stale session generation",
                              })
            if changing_scene and self.monotonic() < deadline:
                last_unavailable = result
                if self.cancelled is not None and self.cancelled():
                    raise RuntimeCommandError("cancelled while the native scene is changing")
                self.sleep(min(0.25, max(0.0, deadline - self.monotonic())))
                continue
            result.require_ok()
            if result.snapshot is None:
                raise RuntimeCommandProtocolError("DLL did not return an Outer snapshot")
            return RuntimeOuterSnapshot(result.request.session_generation, result.snapshot, result.path)

