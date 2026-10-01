"""Incrementally wait for one native Exam action to physically settle.

Only bytes appended after the supplied offset are read.  ``captured_action``
is intentionally not completion evidence: the watcher accepts either the
typed native queue-drain receipt or a stronger completed transition.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Callable

RECORDER_SCHEMA = "gkms.runtime-exam-recorder.shadow.v1"
SETTLEMENT_SCHEMA = "gkms.runtime-action-settlement.v1"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.1
DEFAULT_TRANSITION_GRACE_SECONDS = 0.25
DEFAULT_CHUNK_BYTES = 64 * 1024
_ACTION_KIND_ALIASES = {
    "play": "use-hand",
    "use-hand": "use-hand",
    "drink": "use-drink",
    "use-drink": "use-drink",
    "end_turn": "turn-end",
    "turn-end": "turn-end",
}


class RuntimeActionSettlementWatcherError(ValueError):
    """Raised for an invalid watcher request or a rewritten source."""


@dataclass(frozen=True, slots=True)
class ActionSettlementReceipt:
    path: Path
    start_offset: int
    end_offset: int
    source_record: str
    recorder_sequence: int | None
    runtime_sequence: str | int | None
    action_order: int
    official_action_order: int
    action_kind: str
    slot: int
    card_guid: str | None
    drink_id: str | None
    physically_settled: bool
    transition_ready: bool
    terminal: bool
    # Canonical JSON is immutable and can be compared/hashed without exposing
    # the mutable object returned by json.loads.
    state_after: str | None


def _integer(value: object, *, minimum: int = 0) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    return None


class RuntimeActionSettlementWatcher:
    """Read an append-only recorder suffix until one expected action settles."""

    def __init__(
        self,
        path: str | Path,
        *,
        start_offset: int,
        expected_action_kind: str,
        expected_slot: int | None = None,
        expected_card_guid: str | None = None,
        expected_drink_id: str | None = None,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> None:
        if _integer(start_offset) is None:
            raise RuntimeActionSettlementWatcherError(
                "start_offset must be a non-negative integer"
            )
        if not isinstance(expected_action_kind, str) or not expected_action_kind:
            raise RuntimeActionSettlementWatcherError(
                "expected_action_kind must be a non-empty string"
            )
        native_action_kind = _ACTION_KIND_ALIASES.get(expected_action_kind.lower())
        if native_action_kind is None:
            raise RuntimeActionSettlementWatcherError(
                "expected_action_kind must be play, drink, end_turn, "
                "use-hand, use-drink, or turn-end"
            )
        if expected_slot is not None and _integer(expected_slot) is None:
            raise RuntimeActionSettlementWatcherError(
                "expected_slot must be a non-negative integer or None"
            )
        for name, value in (
            ("expected_card_guid", expected_card_guid),
            ("expected_drink_id", expected_drink_id),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise RuntimeActionSettlementWatcherError(
                    f"{name} must be a non-empty string or None"
                )
        if _integer(chunk_bytes, minimum=1) is None:
            raise RuntimeActionSettlementWatcherError(
                "chunk_bytes must be a positive integer"
            )
        self.path = Path(path)
        self.start_offset = start_offset
        self.expected_action_kind = native_action_kind
        self.expected_slot = expected_slot
        self.expected_card_guid = expected_card_guid
        self.expected_drink_id = expected_drink_id
        self.chunk_bytes = chunk_bytes
        self.read_offset = start_offset
        self._buffer_start_offset = start_offset
        self._buffer = b""
        self._pending_ready_settlement: ActionSettlementReceipt | None = None

    def _matches_action(
        self, body: Mapping[str, object]
    ) -> tuple[str, int, str | None, str | None] | None:
        action = body.get("action")
        if not isinstance(action, Mapping):
            return None
        if action.get("known") is not True or action.get("isManual") is not True:
            return None
        kind = action.get("action_type")
        slot = _integer(action.get("play_index"))
        if not isinstance(kind, str) or slot is None:
            return None
        source_card = action.get("source_card")
        card_guid = (
            source_card.get("guid")
            if isinstance(source_card, Mapping)
            and isinstance(source_card.get("guid"), str)
            else None
        )
        drink_id = action.get("source_drink_id")
        if not isinstance(drink_id, str):
            drink_id = None
        if kind != self.expected_action_kind:
            return None
        if self.expected_slot is not None and slot != self.expected_slot:
            return None
        if self.expected_card_guid is not None and card_guid != self.expected_card_guid:
            return None
        if self.expected_drink_id is not None and drink_id != self.expected_drink_id:
            return None
        return kind, slot, card_guid, drink_id

    def _receipt(
        self,
        row: Mapping[str, object],
        *,
        end_offset: int,
    ) -> ActionSettlementReceipt | None:
        if row.get("schema") != RECORDER_SCHEMA:
            return None
        body = row.get("body")
        if not isinstance(body, Mapping):
            return None
        record = body.get("record")
        is_settlement = record == "action_settlement"
        is_transition = record == "transition"
        if not is_settlement and not is_transition:
            return None
        if body.get("official_action_captured") is not True:
            return None
        if is_settlement:
            if (
                body.get("schema") != SETTLEMENT_SCHEMA
                or body.get("queue_drain_observed") is not True
                or body.get("physically_settled") is not True
            ):
                return None
        elif (
            body.get("state_after_captured") is not True
            or not isinstance(body.get("state_after"), Mapping)
        ):
            return None
        identity = self._matches_action(body)
        if identity is None:
            return None
        action_order = _integer(body.get("action_order"))
        official_order = _integer(body.get("official_action_order"))
        if action_order is None or official_order is None:
            return None
        recorder_sequence = _integer(row.get("sequence"), minimum=1)
        runtime_sequence = body.get("runtime_sequence")
        if not isinstance(runtime_sequence, (str, int)) or isinstance(
            runtime_sequence, bool
        ):
            runtime_sequence = None
        kind, slot, card_guid, drink_id = identity
        state_after: str | None = None
        if isinstance(body.get("state_after"), Mapping):
            try:
                state_after = json.dumps(
                    body["state_after"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError):
                return None
        return ActionSettlementReceipt(
            path=self.path,
            start_offset=self.start_offset,
            end_offset=end_offset,
            source_record=str(record),
            recorder_sequence=recorder_sequence,
            runtime_sequence=runtime_sequence,
            action_order=action_order,
            official_action_order=official_order,
            action_kind=kind,
            slot=slot,
            card_guid=card_guid,
            drink_id=drink_id,
            physically_settled=True,
            transition_ready=(
                True if is_transition else body.get("transition_ready") is True
            ),
            terminal=body.get("terminal") is True,
            state_after=state_after,
        )

    @staticmethod
    def _same_transaction(
        left: ActionSettlementReceipt,
        right: ActionSettlementReceipt,
    ) -> bool:
        return (
            left.action_order == right.action_order
            and left.official_action_order == right.official_action_order
            and (
                left.runtime_sequence is None
                or right.runtime_sequence is None
                or left.runtime_sequence == right.runtime_sequence
            )
        )

    def poll(self) -> ActionSettlementReceipt | None:
        """Read newly appended complete lines once."""

        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RuntimeActionSettlementWatcherError(str(error)) from error
        if size < self.read_offset:
            raise RuntimeActionSettlementWatcherError(
                "append-only settlement source was truncated or replaced"
            )
        settlements: list[ActionSettlementReceipt] = []
        transitions: list[ActionSettlementReceipt] = []
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.read_offset)
                while chunk := stream.read(self.chunk_bytes):
                    self.read_offset += len(chunk)
                    self._buffer += chunk
                    while b"\n" in self._buffer:
                        raw, self._buffer = self._buffer.split(b"\n", 1)
                        end_offset = self._buffer_start_offset + len(raw) + 1
                        self._buffer_start_offset = end_offset
                        try:
                            row = json.loads(raw.rstrip(b"\r").decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if not isinstance(row, Mapping):
                            continue
                        receipt = self._receipt(row, end_offset=end_offset)
                        if receipt is not None:
                            if receipt.source_record == "transition":
                                transitions.append(receipt)
                            else:
                                settlements.append(receipt)
        except OSError as error:
            raise RuntimeActionSettlementWatcherError(str(error)) from error
        anchors = settlements or (
            [self._pending_ready_settlement]
            if self._pending_ready_settlement is not None
            else []
        )
        for transition in transitions:
            if transition.terminal or any(
                self._same_transaction(anchor, transition) for anchor in anchors
            ):
                self._pending_ready_settlement = None
                return transition
        if settlements:
            settlement = settlements[0]
            # A prior action can emit its delayed transition only when this
            # action is enqueued.  Retain every matching post-cursor
            # settlement as the transaction anchor, including the state-less
            # form, so a repeated card/drink identity cannot claim S' from the
            # preceding action.
            self._pending_ready_settlement = settlement
            return settlement
        return None

    def watch(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        transition_grace_seconds: float = DEFAULT_TRANSITION_GRACE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> ActionSettlementReceipt | None:
        if (
            timeout_seconds < 0
            or poll_interval_seconds <= 0
            or transition_grace_seconds < 0
        ):
            raise RuntimeActionSettlementWatcherError(
                "timeout/grace must be non-negative and poll interval positive"
            )
        deadline = monotonic() + timeout_seconds
        fallback = self._pending_ready_settlement
        grace_deadline: float | None = None
        while True:
            receipt = self.poll()
            if receipt is not None:
                if receipt.source_record == "transition" or not receipt.transition_ready:
                    return receipt
                fallback = receipt
                grace_deadline = min(
                    deadline, monotonic() + transition_grace_seconds
                )
            now = monotonic()
            if fallback is not None and grace_deadline is None:
                grace_deadline = min(deadline, now + transition_grace_seconds)
            if fallback is not None and grace_deadline is not None and now >= grace_deadline:
                return fallback
            remaining = deadline - now
            if remaining <= 0:
                return fallback
            if grace_deadline is not None:
                remaining = min(remaining, grace_deadline - now)
            sleep(min(poll_interval_seconds, remaining))

    def watch_transition(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> ActionSettlementReceipt | None:
        """Wait the full bounded window for a matching state transition.

        The ordinary :meth:`watch` API intentionally returns a state-less
        ``action_settlement`` as physical completion evidence.  Native state
        priority has a narrower contract: only a matching ``transition`` can
        replace an already-complete logical replay.  Keep consuming the same
        append-only cursor when settlement arrives first, and return ``None``
        rather than that weaker receipt when the bounded window expires.
        """

        if timeout_seconds < 0 or poll_interval_seconds <= 0:
            raise RuntimeActionSettlementWatcherError(
                "timeout must be non-negative and poll interval positive"
            )
        deadline = monotonic() + timeout_seconds
        while True:
            receipt = self.poll()
            if receipt is not None and receipt.source_record == "transition":
                return receipt
            remaining = deadline - monotonic()
            if remaining <= 0:
                return None
            sleep(min(poll_interval_seconds, remaining))


def watch_action_settlement(
    path: str | Path,
    *,
    start_offset: int,
    expected_action_kind: str,
    expected_slot: int | None = None,
    expected_card_guid: str | None = None,
    expected_drink_id: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    transition_grace_seconds: float = DEFAULT_TRANSITION_GRACE_SECONDS,
) -> ActionSettlementReceipt | None:
    """Convenience wrapper for a single blocking settlement wait."""

    return RuntimeActionSettlementWatcher(
        path,
        start_offset=start_offset,
        expected_action_kind=expected_action_kind,
        expected_slot=expected_slot,
        expected_card_guid=expected_card_guid,
        expected_drink_id=expected_drink_id,
    ).watch(
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        transition_grace_seconds=transition_grace_seconds,
    )


__all__ = [
    "ActionSettlementReceipt",
    "DEFAULT_TRANSITION_GRACE_SECONDS",
    "RECORDER_SCHEMA",
    "RuntimeActionSettlementWatcher",
    "RuntimeActionSettlementWatcherError",
    "SETTLEMENT_SCHEMA",
    "watch_action_settlement",
]
