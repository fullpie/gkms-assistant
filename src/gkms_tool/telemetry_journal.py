"""Strict reader for the read-only in-process exam telemetry journal."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Final

from .telemetry_injector import _validate_ready_event


TELEMETRY_SCHEMA: Final = "gkms.telemetry.v1"


class TelemetryProtocolError(RuntimeError):
    """The observer journal is not an exact, ordered v1 event stream."""


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    sequence: int
    call_id: int
    parent_call_id: int
    unix_time_ms: int
    thread_id: int
    kind: str
    hook: str
    stage: str
    parameter: str | None
    before: dict[str, object] | None
    after: dict[str, object] | None
    data: dict[str, object]
    raw: dict[str, object]


@dataclass(frozen=True, slots=True)
class TelemetryJournalSummary:
    path: Path
    event_count: int
    first_sequence: int
    last_sequence: int
    ready_sequence: int
    counts_by_kind: dict[str, int]


@dataclass(frozen=True, slots=True)
class NativeCardAction:
    sequence: int
    manual_command_sequence: int
    call_id: int
    thread_id: int
    parameter: str
    position: int
    use_playable: bool
    card_id: str
    card_guid: str
    before: dict[str, object]
    after: dict[str, object]
    nested_sequences: tuple[int, ...]


def _event_from_raw(raw: object, *, line_number: int) -> TelemetryEvent:
    if not isinstance(raw, dict):
        raise TelemetryProtocolError(f"line {line_number}: event must be an object")
    if raw.get("schema") != TELEMETRY_SCHEMA:
        raise TelemetryProtocolError(
            f"line {line_number}: unexpected schema {raw.get('schema')!r}"
        )
    integer_fields = (
        "sequence",
        "call_id",
        "parent_call_id",
        "unix_time_ms",
        "thread_id",
        "dropped_events",
    )
    for name in integer_fields:
        if not isinstance(raw.get(name), int) or isinstance(raw.get(name), bool):
            raise TelemetryProtocolError(f"line {line_number}: {name} must be an integer")
    sequence = int(raw["sequence"])
    if sequence <= 0:
        raise TelemetryProtocolError(f"line {line_number}: sequence must be positive")
    if int(raw["call_id"]) < 0 or int(raw["parent_call_id"]) < 0:
        raise TelemetryProtocolError(f"line {line_number}: call IDs cannot be negative")
    if int(raw["call_id"]) == 0 and int(raw["parent_call_id"]) != 0:
        raise TelemetryProtocolError(
            f"line {line_number}: a bootstrap event cannot have a parent call"
        )
    if raw.get("dropped_events") != 0:
        raise TelemetryProtocolError(
            f"line {line_number}: observer dropped {raw.get('dropped_events')} events"
        )
    strings: dict[str, str] = {}
    for name in ("kind", "hook", "stage"):
        value = raw.get(name)
        if not isinstance(value, str):
            raise TelemetryProtocolError(f"line {line_number}: {name} must be a string")
        strings[name] = value
    parameter = raw.get("parameter")
    if parameter is not None and not isinstance(parameter, str):
        raise TelemetryProtocolError(
            f"line {line_number}: parameter must be a string or null"
        )
    before = raw.get("before")
    after = raw.get("after")
    data = raw.get("data")
    if before is not None and not isinstance(before, dict):
        raise TelemetryProtocolError(f"line {line_number}: before must be an object or null")
    if after is not None and not isinstance(after, dict):
        raise TelemetryProtocolError(f"line {line_number}: after must be an object or null")
    if not isinstance(data, dict):
        raise TelemetryProtocolError(f"line {line_number}: data must be an object")
    return TelemetryEvent(
        sequence=sequence,
        call_id=int(raw["call_id"]),
        parent_call_id=int(raw["parent_call_id"]),
        unix_time_ms=int(raw["unix_time_ms"]),
        thread_id=int(raw["thread_id"]),
        kind=strings["kind"],
        hook=strings["hook"],
        stage=strings["stage"],
        parameter=parameter,
        before=before,
        after=after,
        data=data,
        raw=raw,
    )


def read_telemetry_events(
    path: str | Path,
    *,
    after_sequence: int = 0,
    allow_partial_tail: bool = True,
) -> tuple[TelemetryEvent, ...]:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as error:
        raise TelemetryProtocolError(f"cannot read telemetry journal {source}: {error}") from error
    lines = text.splitlines(keepends=True)
    result: list[TelemetryEvent] = []
    previous_sequence = 0
    for index, original in enumerate(lines, start=1):
        line = original.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            is_partial_tail = index == len(lines) and not original.endswith(("\n", "\r"))
            if allow_partial_tail and is_partial_tail:
                break
            raise TelemetryProtocolError(f"line {index}: invalid JSON: {error}") from error
        event = _event_from_raw(raw, line_number=index)
        if event.sequence != previous_sequence + 1:
            raise TelemetryProtocolError(
                f"line {index}: non-contiguous sequence "
                f"{event.sequence} after {previous_sequence}"
            )
        previous_sequence = event.sequence
        if event.kind == "observer_error":
            raise TelemetryProtocolError(f"observer reported an error: {event.data}")
        if event.sequence > after_sequence:
            result.append(event)
    return tuple(result)


def summarize_telemetry_journal(path: str | Path) -> TelemetryJournalSummary:
    source = Path(path)
    events = read_telemetry_events(source)
    if not events:
        raise TelemetryProtocolError("telemetry journal contains no complete events")
    ready = [event for event in events if event.kind == "observer_ready"]
    if len(ready) != 1:
        raise TelemetryProtocolError(
            f"telemetry journal must contain exactly one ready event, found {len(ready)}"
        )
    if ready[0] is not events[0]:
        raise TelemetryProtocolError("observer_ready must be the first complete event")
    if ready[0].call_id != 0 or ready[0].parent_call_id != 0:
        raise TelemetryProtocolError("observer_ready must be outside managed hook calls")
    try:
        _validate_ready_event(ready[0].raw)
    except RuntimeError as error:
        raise TelemetryProtocolError(str(error)) from error
    counts = Counter(event.kind for event in events)
    return TelemetryJournalSummary(
        path=source.resolve(),
        event_count=len(events),
        first_sequence=events[0].sequence,
        last_sequence=events[-1].sequence,
        ready_sequence=ready[0].sequence,
        counts_by_kind=dict(sorted(counts.items())),
    )


def extract_normal_user_card_actions(
    events: tuple[TelemetryEvent, ...],
) -> tuple[NativeCardAction, ...]:
    """Return only non-forecast user card calls with exact native identity."""

    children: dict[int, list[TelemetryEvent]] = {}
    for event in events:
        if event.parent_call_id:
            children.setdefault(event.parent_call_id, []).append(event)

    def descendants(call_id: int) -> tuple[int, ...]:
        pending = list(children.get(call_id, ()))
        sequences: list[int] = []
        while pending:
            child = pending.pop(0)
            sequences.append(child.sequence)
            pending.extend(children.get(child.call_id, ()))
        return tuple(sorted(sequences))

    result: list[NativeCardAction] = []
    by_sequence = {event.sequence: event for event in events}
    for event in events:
        # Protocol 3 emits an additive ``action_state`` row with the same
        # hook name as the transaction.  It deliberately has no forecast
        # object; only the transaction boundary is card execution evidence.
        # Never promote its ``unknown``/missing provenance to ``normal``.
        if event.hook != "ExamSequence.ExecuteCardCommandImpl":
            continue
        if event.kind == "action_state":
            continue
        if event.kind != "transaction":
            raise TelemetryProtocolError(
                f"card hook {event.sequence} has unsupported event kind {event.kind!r}"
            )
        forecast = event.data.get("forecast")
        if not isinstance(forecast, dict):
            raise TelemetryProtocolError(
                f"card action {event.sequence} has no forecast provenance"
            )
        forecast_state = forecast.get("state")
        if forecast_state == "forecast":
            continue
        if forecast_state != "normal":
            raise TelemetryProtocolError(
                f"card action {event.sequence} has unknown forecast provenance"
            )
        if event.call_id <= 0 or event.parameter is None:
            raise TelemetryProtocolError(
                f"card action {event.sequence} has no native call identity"
            )
        if event.before is None or event.after is None:
            raise TelemetryProtocolError(
                f"card action {event.sequence} has no scalar boundary"
            )
        card = event.data.get("card")
        if not isinstance(card, dict):
            raise TelemetryProtocolError(f"card action {event.sequence} has no card")
        card_id = card.get("id")
        card_guid = card.get("guid")
        position = event.data.get("position")
        use_playable = event.data.get("use_playable")
        if not isinstance(card_id, str) or not card_id:
            raise TelemetryProtocolError(f"card action {event.sequence} has no card ID")
        if not isinstance(card_guid, str) or not card_guid:
            raise TelemetryProtocolError(f"card action {event.sequence} has no card GUID")
        if not isinstance(position, int) or isinstance(position, bool):
            raise TelemetryProtocolError(f"card action {event.sequence} has invalid position")
        if not isinstance(use_playable, bool):
            raise TelemetryProtocolError(
                f"card action {event.sequence} has invalid playable-count flag"
            )
        manual_event = by_sequence.get(event.sequence - 1)
        manual_command = (
            manual_event.data.get("command")
            if manual_event is not None
            and manual_event.kind == "command_dequeue"
            and manual_event.hook == "ExamCommandStack.RemoveCurrentCommand"
            else None
        )
        manual_card = (
            manual_command.get("card")
            if isinstance(manual_command, dict)
            else None
        )
        if not (
            isinstance(manual_command, dict)
            and manual_command.get("manual") is True
            and manual_command.get("play_type") == 2
            and isinstance(manual_card, dict)
            and manual_card.get("id") == card_id
            and manual_card.get("guid") == card_guid
            and manual_event is not None
            and manual_event.thread_id == event.thread_id
        ):
            raise TelemetryProtocolError(
                f"normal card call {event.sequence} lacks the adjacent manual command"
            )
        result.append(
            NativeCardAction(
                sequence=event.sequence,
                manual_command_sequence=manual_event.sequence,
                call_id=event.call_id,
                thread_id=event.thread_id,
                parameter=event.parameter,
                position=position,
                use_playable=use_playable,
                card_id=card_id,
                card_guid=card_guid,
                before=event.before,
                after=event.after,
                nested_sequences=descendants(event.call_id),
            )
        )
    return tuple(result)
