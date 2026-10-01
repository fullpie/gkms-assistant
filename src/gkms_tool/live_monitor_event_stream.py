"""Event-driven telemetry bridge from the elevated Maa worker to the GUI.

The formal Maa action thread only calls :meth:`LiveMonitorEventPublisher.publish`.
That method is a non-blocking in-memory queue put.  A single daemon owns JSONL
serialization, append I/O, and the Windows auto-reset event notification.

The medium-integrity GUI blocks on that notification and incrementally reads
only complete newly appended lines.  This module contains no game input,
capture, model loading, or policy selection.
"""

from __future__ import annotations

import ctypes
import itertools
import json
import os
import queue
import threading
import time
from collections import deque
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MONITOR_EVENT_PATH = (
    PROJECT_ROOT / "var" / "elevated_controller" / "live_monitor_events.jsonl"
)
DEFAULT_MONITOR_EVENT_NAME = r"Local\gkms_tool.live_monitor.transition.v1"
MONITOR_EVENT_SCHEMA = "gkms.live-monitor-transition.v1"
MONITOR_SNAPSHOT_SCHEMA = "gkms.live-monitor.v1"

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000


class _WindowsAutoResetEvent:
    """Small CreateEvent/WaitForSingleObject wrapper shared by both sides."""

    def __init__(self, name: str) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.SetEvent.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateEventW(None, False, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._kernel32 = kernel32
        self._handle = handle

    def set(self) -> None:
        if not self._kernel32.SetEvent(self._handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, timeout_seconds: float | None = None) -> bool:
        timeout_ms = (
            0xFFFFFFFF
            if timeout_seconds is None
            else max(0, min(round(float(timeout_seconds) * 1000), 0xFFFFFFFE))
        )
        result = self._kernel32.WaitForSingleObject(self._handle, timeout_ms)
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle:
            self._kernel32.CloseHandle(handle)
            self._handle = None


class _LocalAutoResetEvent:
    """Same-process seam used on non-Windows hosts and in focused tests."""

    _registry: dict[str, threading.Event] = {}
    _registry_lock = threading.Lock()

    def __init__(self, name: str) -> None:
        with self._registry_lock:
            self._event = self._registry.setdefault(name, threading.Event())

    def set(self) -> None:
        self._event.set()

    def wait(self, timeout_seconds: float | None = None) -> bool:
        signaled = self._event.wait(timeout_seconds)
        if signaled:
            self._event.clear()
        return signaled

    def close(self) -> None:
        return None


def _open_auto_reset_event(name: str):
    return _WindowsAutoResetEvent(name) if os.name == "nt" else _LocalAutoResetEvent(name)


def _compact_transition_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Build the disk row on the daemon, retaining only monitor semantics."""

    raw_transition = event.get("transition")
    transition = raw_transition if isinstance(raw_transition, Mapping) else {}
    raw_after = transition.get("state_after")
    if not isinstance(raw_after, Mapping):
        raw_after = transition.get("next_state")
    state_after: dict[str, Any] = {}
    if isinstance(raw_after, Mapping):
        for name in (
            "current_turn",
            "remain_turn",
            "limit_turn",
            "score",
            "stamina",
            "max_stamina",
            "block",
            "turn_card_play_count",
            "exam_card_play_count",
            "step_type_value",
            "phase",
        ):
            value = raw_after.get(name)
            if isinstance(value, (str, int, float, bool)):
                state_after[name] = value
        zones = raw_after.get("zones")
        hand = zones.get("hand") if isinstance(zones, Mapping) else None
        if isinstance(hand, list):
            compact_hand: list[dict[str, Any]] = []
            for raw_card in hand:
                if not isinstance(raw_card, Mapping):
                    continue
                card = {
                    name: value
                    for name in (
                        "guid",
                        "card_id",
                        "effective_upgrade",
                    )
                    if isinstance(
                        (value := raw_card.get(name)), (str, int, float, bool)
                    )
                }
                runtime = raw_card.get("runtime_state")
                play_count = (
                    runtime.get("play_count")
                    if isinstance(runtime, Mapping)
                    else None
                )
                if isinstance(play_count, int) and not isinstance(play_count, bool):
                    card["runtime_state"] = {"play_count": play_count}
                compact_hand.append(card)
            state_after["zones"] = {"hand": compact_hand}
        root_runtime = raw_after.get("root_runtime")
        opaque = (
            root_runtime.get("opaque_fields")
            if isinstance(root_runtime, Mapping)
            else None
        )
        raw_drinks = opaque.get("drinkList") if isinstance(opaque, Mapping) else None
        if isinstance(raw_drinks, list):
            compact_drinks = []
            for raw_drink in raw_drinks:
                if not isinstance(raw_drink, Mapping):
                    continue
                drink_id = raw_drink.get("_id", raw_drink.get("id"))
                if isinstance(drink_id, str) and drink_id:
                    compact_drinks.append({"_id": drink_id})
            state_after["root_runtime"] = {
                "opaque_fields": {"drinkList": compact_drinks}
            }

    action = transition.get("action")
    compact_transition = {
        "state_after": state_after,
        "action": dict(action) if isinstance(action, Mapping) else {},
        "reward": transition.get("reward"),
        "terminal": bool(transition.get("terminal", False)),
    }
    return {
        "schema": event.get("schema"),
        "event_id": event.get("event_id"),
        "source_run_id": event.get("source_run_id"),
        "published_at": event.get("published_at"),
        "transition": compact_transition,
    }


class LiveMonitorEventPublisher:
    """Non-blocking transition publisher with one I/O-owning daemon."""

    def __init__(
        self,
        path: str | Path = DEFAULT_MONITOR_EVENT_PATH,
        *,
        event_name: str = DEFAULT_MONITOR_EVENT_NAME,
        max_pending: int = 1024,
        signal_factory: Callable[[str], Any] = _open_auto_reset_event,
    ) -> None:
        self.path = Path(path)
        self.event_name = str(event_name)
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=max(1, int(max_pending))
        )
        self._sequence = itertools.count(1)
        self._signal_factory = signal_factory
        self._thread = threading.Thread(
            target=self._writer,
            daemon=True,
            name="gkms-live-monitor-event-writer",
        )
        self._thread.start()

    def publish(self, source_run_id: str, transition: Mapping[str, Any]) -> bool:
        """Queue one immutable exact row without waiting or doing I/O."""

        if not isinstance(source_run_id, str) or not source_run_id.strip():
            return False
        if not isinstance(transition, Mapping):
            return False
        sequence = next(self._sequence)
        event = {
            "schema": MONITOR_EVENT_SCHEMA,
            "event_id": f"{os.getpid()}:{sequence}",
            "source_run_id": source_run_id.strip(),
            "published_at": time.time(),
            # Exact rows are freshly constructed by ProduceCardsAuto and are
            # not mutated after observer fanout.  A shallow copy prevents the
            # top-level owner from being shared while keeping this callback
            # bounded and non-blocking.
            "transition": dict(transition),
        }
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            return False
        return True

    def _writer(self) -> None:
        signal = None
        try:
            signal = self._signal_factory(self.event_name)
        except Exception:
            # File append can still serve a later reader.  Signaling failures
            # never flow back into Maa.
            signal = None
        while True:
            event = self._queue.get()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(
                    _compact_transition_event(event),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(line + "\n")
                    stream.flush()
                if signal is not None:
                    signal.set()
            except Exception:
                # Observability is best-effort and cannot change formal input.
                pass
            finally:
                self._queue.task_done()


_PUBLISHERS: dict[tuple[str, str], LiveMonitorEventPublisher] = {}
_PUBLISHERS_LOCK = threading.Lock()


def get_live_monitor_event_publisher(
    path: str | Path = DEFAULT_MONITOR_EVENT_PATH,
    *,
    event_name: str = DEFAULT_MONITOR_EVENT_NAME,
) -> LiveMonitorEventPublisher:
    """Return the process-wide single writer for one stream."""

    key = (str(Path(path).resolve(strict=False)), str(event_name))
    with _PUBLISHERS_LOCK:
        publisher = _PUBLISHERS.get(key)
        if publisher is None:
            publisher = LiveMonitorEventPublisher(path, event_name=event_name)
            _PUBLISHERS[key] = publisher
        return publisher


def project_exact_transition_snapshot(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a settled exact transition into the existing GUI schema."""

    if event.get("schema") != MONITOR_EVENT_SCHEMA:
        return None
    transition = event.get("transition")
    if not isinstance(transition, Mapping):
        return None
    state_after = transition.get("state_after")
    if not isinstance(state_after, Mapping):
        state_after = transition.get("next_state")
    if not isinstance(state_after, Mapping):
        return None

    scalar_names = (
        "current_turn",
        "remain_turn",
        "limit_turn",
        "score",
        "stamina",
        "max_stamina",
        "block",
        "turn_card_play_count",
        "exam_card_play_count",
        "step_type_value",
        "phase",
    )
    state = {
        name: value
        for name in scalar_names
        if isinstance((value := state_after.get(name)), (str, int, float, bool))
    }

    cards: list[dict[str, Any]] = []
    zones = state_after.get("zones")
    hand = zones.get("hand") if isinstance(zones, Mapping) else None
    if isinstance(hand, list):
        for index, raw in enumerate(hand):
            if not isinstance(raw, Mapping):
                continue
            card: dict[str, Any] = {"hand_index": index}
            for name in ("guid", "card_id", "effective_upgrade"):
                value = raw.get(name)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    card[name] = value
            runtime = raw.get("runtime_state")
            if isinstance(runtime, Mapping):
                play_count = runtime.get("play_count")
                if isinstance(play_count, int) and not isinstance(play_count, bool):
                    card["play_count"] = play_count
            cards.append(card)

    drinks: list[dict[str, Any]] = []
    root_runtime = state_after.get("root_runtime")
    opaque = root_runtime.get("opaque_fields") if isinstance(root_runtime, Mapping) else None
    raw_drinks = opaque.get("drinkList") if isinstance(opaque, Mapping) else None
    if isinstance(raw_drinks, list):
        for slot, raw in enumerate(raw_drinks):
            if not isinstance(raw, Mapping):
                continue
            drink_id = raw.get("_id", raw.get("id"))
            if isinstance(drink_id, str) and drink_id:
                drinks.append({"slot": slot, "drink_id": drink_id})

    published_at = event.get("published_at")
    snapshot: dict[str, Any] = {
        "schema": MONITOR_SNAPSHOT_SCHEMA,
        "page": "exam",
        "source_run_id": event.get("source_run_id"),
        "event_id": event.get("event_id"),
        "state_update": {
            "timestamp": (
                float(published_at)
                if isinstance(published_at, (int, float))
                and not isinstance(published_at, bool)
                else time.time()
            ),
            "kind": "exact-transition",
        },
        "state": state,
        "cards": cards,
        "drinks": drinks,
        "transition": {
            "action": dict(transition.get("action", {}))
            if isinstance(transition.get("action"), Mapping)
            else {},
            "reward": transition.get("reward"),
            "terminal": bool(transition.get("terminal", False)),
        },
    }
    return snapshot


class LiveMonitorEventWatcher:
    """Blocking event waiter plus partial-line-safe incremental JSONL reader."""

    def __init__(
        self,
        source_run_id: str,
        path: str | Path = DEFAULT_MONITOR_EVENT_PATH,
        *,
        event_name: str = DEFAULT_MONITOR_EVENT_NAME,
        start_at_end: bool = False,
        signal_factory: Callable[[str], Any] = _open_auto_reset_event,
    ) -> None:
        if not isinstance(source_run_id, str) or not source_run_id.strip():
            raise ValueError("source_run_id is required for live monitor binding")
        self.source_run_id = source_run_id.strip()
        self.path = Path(path)
        try:
            self._offset = self.path.stat().st_size if start_at_end else 0
        except OSError:
            self._offset = 0
        self._partial = b""
        self._seen_order: deque[str] = deque(maxlen=2048)
        self._seen: set[str] = set()
        self._signal = signal_factory(str(event_name))

    def wait(self, timeout_seconds: float | None = 1.0) -> bool:
        return bool(self._signal.wait(timeout_seconds))

    def read_available(self) -> list[dict[str, Any]]:
        """Return matching snapshots from complete, newly appended lines."""

        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            self._offset = 0
            self._partial = b""
        if size == self._offset:
            return []
        try:
            with self.path.open("rb") as stream:
                stream.seek(self._offset)
                chunk = stream.read()
        except OSError:
            return []
        self._offset += len(chunk)
        data = self._partial + chunk
        parts = data.split(b"\n")
        self._partial = parts.pop()
        snapshots: list[dict[str, Any]] = []
        for raw_line in parts:
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, Mapping):
                continue
            if event.get("source_run_id") != self.source_run_id:
                continue
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                continue
            if event_id in self._seen:
                continue
            if len(self._seen_order) == self._seen_order.maxlen:
                oldest = self._seen_order.popleft()
                self._seen.discard(oldest)
            self._seen_order.append(event_id)
            self._seen.add(event_id)
            snapshot = project_exact_transition_snapshot(event)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    def run(
        self,
        *,
        stop_requested: Callable[[], bool],
        publish_snapshot: Callable[[Mapping[str, Any]], None],
        wait_timeout_seconds: float = 1.0,
    ) -> None:
        """Block until signaled and fan out the latest settled snapshots."""

        # The writer may append and signal between run binding and this
        # daemon's first WaitForSingleObject.  Read the incremental backlog
        # once so Turn 1 cannot be lost to that startup race.
        for snapshot in self.read_available():
            if stop_requested():
                return
            publish_snapshot(snapshot)
        while not stop_requested():
            if not self.wait(wait_timeout_seconds):
                continue
            for snapshot in self.read_available():
                if stop_requested():
                    return
                publish_snapshot(snapshot)

    def close(self) -> None:
        self._signal.close()


__all__ = [
    "DEFAULT_MONITOR_EVENT_NAME",
    "DEFAULT_MONITOR_EVENT_PATH",
    "LiveMonitorEventPublisher",
    "LiveMonitorEventWatcher",
    "MONITOR_EVENT_SCHEMA",
    "MONITOR_SNAPSHOT_SCHEMA",
    "get_live_monitor_event_publisher",
    "project_exact_transition_snapshot",
]
