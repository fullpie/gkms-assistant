"""Pure typed runtime for native ``ExamPlayableValueAdd`` statuses.

The native status collection owns at most one active
``PlayableValueAddStatusEffect``.  Adding a value creates a one-turn status
with the next global status UID, or merges into the existing status without
allocating another UID.  Card-play settlement spends one value and removes
the status at zero.  A newly created status is fresh until the outer turn
start marker sets ``passing_turn_start``; only an already-passing status is
expired at the following turn boundary.

This module deliberately has no dependency on a plan engine, card catalog,
or native-zone simulator.  Callers supply a provenance ``source`` and the
global next-status cursor.  Every operation returns one immutable receipt so
later integration can validate native create/use/remove evidence without
copying a captured post-state into the simulator.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


INT32_MAX = (1 << 31) - 1
PLAYABLE_VALUE_ADD_TURN = 1


class PlayableValueAddOperation(str, Enum):
    """Exact operation shape represented by one runtime receipt."""

    ADD_CREATE = "add-create"
    ADD_MERGE = "add-merge"
    USE_RETAIN = "use-retain"
    USE_REMOVE = "use-remove"
    USE_ABSENT = "use-absent"
    SPEND_RETAIN = "spend-retain"
    SPEND_REMOVE = "spend-remove"
    SPEND_ABSENT = "spend-absent"
    MARK_PASSING = "mark-passing"
    MARK_NOOP = "mark-noop"


def _plain_int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not minimum <= value <= INT32_MAX:
        raise ValueError(
            f"{label} must be between {minimum} and {INT32_MAX}"
        )
    return value


def _source(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source must be non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class PlayableValueAddStatus:
    """The single active native PlayableValueAdd status, when present."""

    uid: int
    value: int
    turn: int = PLAYABLE_VALUE_ADD_TURN
    passing_turn_start: bool = False

    def __post_init__(self) -> None:
        _plain_int(self.uid, "playable status uid", minimum=1)
        _plain_int(self.value, "playable status value", minimum=1)
        _plain_int(self.turn, "playable status turn", minimum=1)
        if self.turn != PLAYABLE_VALUE_ADD_TURN:
            raise ValueError("playable status turn must be exactly 1")
        if type(self.passing_turn_start) is not bool:
            raise TypeError(
                "playable status passing_turn_start must be boolean"
            )

    @property
    def is_turn_limited(self) -> bool:
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "uid": self.uid,
            "value": self.value,
            "turn": self.turn,
            "passing_turn_start": self.passing_turn_start,
            "is_turn_limited": True,
        }


@dataclass(frozen=True, slots=True)
class PlayableValueAddRuntime:
    """Immutable projection of the zero-or-one native status collection."""

    status: PlayableValueAddStatus | None = None

    def __post_init__(self) -> None:
        if self.status is not None and not isinstance(
            self.status, PlayableValueAddStatus
        ):
            raise TypeError(
                "playable runtime status must be PlayableValueAddStatus or None"
            )

    @property
    def status_present(self) -> bool:
        return self.status is not None

    @property
    def uid(self) -> int | None:
        return None if self.status is None else self.status.uid

    @property
    def value(self) -> int:
        return 0 if self.status is None else self.status.value

    def to_dict(self) -> dict[str, object]:
        return {
            "status_present": self.status_present,
            "status": None if self.status is None else self.status.to_dict(),
        }


def _validate_cursor(
    runtime: PlayableValueAddRuntime,
    next_status_uid: object,
) -> int:
    if not isinstance(runtime, PlayableValueAddRuntime):
        raise TypeError("runtime must be PlayableValueAddRuntime")
    cursor = _plain_int(next_status_uid, "next_status_uid", minimum=1)
    if runtime.uid is not None and cursor <= runtime.uid:
        raise ValueError(
            "next_status_uid must be greater than the active status uid"
        )
    return cursor


@dataclass(frozen=True, slots=True)
class PlayableValueAddReceipt:
    """Typed proof of one completed PlayableValueAdd runtime operation."""

    operation: PlayableValueAddOperation
    source: str
    before: PlayableValueAddRuntime
    after: PlayableValueAddRuntime
    cursor_before: int
    cursor_after: int
    created_uid: int | None = None
    used_uid: int | None = None
    removed_uid: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, PlayableValueAddOperation):
            raise TypeError("operation must be PlayableValueAddOperation")
        _source(self.source)
        before_cursor = _validate_cursor(self.before, self.cursor_before)
        after_cursor = _validate_cursor(self.after, self.cursor_after)
        for name in ("created_uid", "used_uid", "removed_uid"):
            value = getattr(self, name)
            if value is not None:
                _plain_int(value, name, minimum=1)

        unchanged_cursor = after_cursor == before_cursor
        before_status = self.before.status
        after_status = self.after.status
        no_created = self.created_uid is None
        no_used = self.used_uid is None
        no_removed = self.removed_uid is None

        if self.operation is PlayableValueAddOperation.ADD_CREATE:
            valid = (
                before_status is None
                and after_status is not None
                and before_cursor < INT32_MAX
                and after_cursor == before_cursor + 1
                and self.created_uid == before_cursor == after_status.uid
                and no_used
                and no_removed
            )
        elif self.operation is PlayableValueAddOperation.ADD_MERGE:
            valid = (
                before_status is not None
                and after_status is not None
                and unchanged_cursor
                and no_created
                and no_used
                and no_removed
                and after_status.uid == before_status.uid
                and after_status.turn == before_status.turn
                and after_status.passing_turn_start
                == before_status.passing_turn_start
                and after_status.value > before_status.value
            )
        elif self.operation is PlayableValueAddOperation.USE_RETAIN:
            valid = (
                before_status is not None
                and after_status is not None
                and before_status.value > 1
                and after_status
                == replace(before_status, value=before_status.value - 1)
                and self.used_uid == before_status.uid
                and no_created
                and no_removed
                and unchanged_cursor
            )
        elif self.operation is PlayableValueAddOperation.USE_REMOVE:
            valid = (
                before_status is not None
                and before_status.value == 1
                and after_status is None
                and self.used_uid == before_status.uid
                and self.removed_uid == before_status.uid
                and no_created
                and unchanged_cursor
            )
        elif self.operation is PlayableValueAddOperation.USE_ABSENT:
            valid = (
                before_status is None
                and after_status is None
                and no_created
                and no_used
                and no_removed
                and unchanged_cursor
            )
        elif self.operation is PlayableValueAddOperation.SPEND_RETAIN:
            valid = (
                before_status is not None
                and not before_status.passing_turn_start
                and after_status == before_status
                and no_created
                and no_used
                and no_removed
                and unchanged_cursor
            )
        elif self.operation is PlayableValueAddOperation.SPEND_REMOVE:
            valid = (
                before_status is not None
                and before_status.passing_turn_start
                and after_status is None
                and self.removed_uid == before_status.uid
                and no_created
                and no_used
                and unchanged_cursor
            )
        elif self.operation is PlayableValueAddOperation.SPEND_ABSENT:
            valid = (
                before_status is None
                and after_status is None
                and no_created
                and no_used
                and no_removed
                and unchanged_cursor
            )
        elif self.operation is PlayableValueAddOperation.MARK_PASSING:
            valid = (
                before_status is not None
                and not before_status.passing_turn_start
                and after_status
                == replace(before_status, passing_turn_start=True)
                and no_created
                and no_used
                and no_removed
                and unchanged_cursor
            )
        else:
            valid = (
                self.operation is PlayableValueAddOperation.MARK_NOOP
                and self.before == self.after
                and (
                    before_status is None
                    or before_status.passing_turn_start
                )
                and no_created
                and no_used
                and no_removed
                and unchanged_cursor
            )
        if not valid:
            raise ValueError(
                f"inconsistent playable operation receipt: {self.operation.value}"
            )

    @property
    def value_before(self) -> int:
        return self.before.value

    @property
    def value_after(self) -> int:
        return self.after.value

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation.value,
            "source": self.source,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            "created_uid": self.created_uid,
            "used_uid": self.used_uid,
            "removed_uid": self.removed_uid,
            "value_before": self.value_before,
            "value_after": self.value_after,
        }


def add_playable_value_add(
    runtime: PlayableValueAddRuntime,
    count: int,
    *,
    next_status_uid: int,
    source: str,
) -> PlayableValueAddReceipt:
    """Create or merge one positive PlayableValueAdd amount atomically."""

    cursor = _validate_cursor(runtime, next_status_uid)
    amount = _plain_int(count, "playable add count", minimum=1)
    provenance = _source(source)
    current = runtime.status
    if current is None:
        if cursor == INT32_MAX:
            raise OverflowError("playable status uid cursor overflow")
        after = PlayableValueAddRuntime(
            PlayableValueAddStatus(uid=cursor, value=amount)
        )
        return PlayableValueAddReceipt(
            operation=PlayableValueAddOperation.ADD_CREATE,
            source=provenance,
            before=runtime,
            after=after,
            cursor_before=cursor,
            cursor_after=cursor + 1,
            created_uid=cursor,
        )

    if current.value > INT32_MAX - amount:
        raise OverflowError("playable status value overflow")
    after = PlayableValueAddRuntime(
        replace(current, value=current.value + amount)
    )
    return PlayableValueAddReceipt(
        operation=PlayableValueAddOperation.ADD_MERGE,
        source=provenance,
        before=runtime,
        after=after,
        cursor_before=cursor,
        cursor_after=cursor,
    )


def use_playable_value_add(
    runtime: PlayableValueAddRuntime,
    *,
    next_status_uid: int,
    source: str,
) -> PlayableValueAddReceipt:
    """Spend one playable value, preserving or removing native identity."""

    cursor = _validate_cursor(runtime, next_status_uid)
    provenance = _source(source)
    current = runtime.status
    if current is None:
        operation = PlayableValueAddOperation.USE_ABSENT
        after = runtime
        used_uid = None
        removed_uid = None
    elif current.value == 1:
        operation = PlayableValueAddOperation.USE_REMOVE
        after = PlayableValueAddRuntime()
        used_uid = current.uid
        removed_uid = current.uid
    else:
        operation = PlayableValueAddOperation.USE_RETAIN
        after = PlayableValueAddRuntime(
            replace(current, value=current.value - 1)
        )
        used_uid = current.uid
        removed_uid = None
    return PlayableValueAddReceipt(
        operation=operation,
        source=provenance,
        before=runtime,
        after=after,
        cursor_before=cursor,
        cursor_after=cursor,
        used_uid=used_uid,
        removed_uid=removed_uid,
    )


def spend_playable_value_add_turn(
    runtime: PlayableValueAddRuntime,
    *,
    next_status_uid: int,
    source: str,
) -> PlayableValueAddReceipt:
    """Apply the native turn-boundary spend gate to the active status."""

    cursor = _validate_cursor(runtime, next_status_uid)
    provenance = _source(source)
    current = runtime.status
    if current is None:
        operation = PlayableValueAddOperation.SPEND_ABSENT
        after = runtime
        removed_uid = None
    elif not current.passing_turn_start:
        operation = PlayableValueAddOperation.SPEND_RETAIN
        after = runtime
        removed_uid = None
    else:
        # The native status always has turn=1, so a passing spend reaches zero
        # and removes the status in the same boundary.
        operation = PlayableValueAddOperation.SPEND_REMOVE
        after = PlayableValueAddRuntime()
        removed_uid = current.uid
    return PlayableValueAddReceipt(
        operation=operation,
        source=provenance,
        before=runtime,
        after=after,
        cursor_before=cursor,
        cursor_after=cursor,
        removed_uid=removed_uid,
    )


def mark_playable_value_add_passing(
    runtime: PlayableValueAddRuntime,
    *,
    next_status_uid: int,
    source: str,
) -> PlayableValueAddReceipt:
    """Mirror the outer TurnStart marker without spending status value."""

    cursor = _validate_cursor(runtime, next_status_uid)
    provenance = _source(source)
    current = runtime.status
    if current is None or current.passing_turn_start:
        operation = PlayableValueAddOperation.MARK_NOOP
        after = runtime
    else:
        operation = PlayableValueAddOperation.MARK_PASSING
        after = PlayableValueAddRuntime(
            replace(current, passing_turn_start=True)
        )
    return PlayableValueAddReceipt(
        operation=operation,
        source=provenance,
        before=runtime,
        after=after,
        cursor_before=cursor,
        cursor_after=cursor,
    )


__all__ = [
    "INT32_MAX",
    "PLAYABLE_VALUE_ADD_TURN",
    "PlayableValueAddOperation",
    "PlayableValueAddReceipt",
    "PlayableValueAddRuntime",
    "PlayableValueAddStatus",
    "add_playable_value_add",
    "mark_playable_value_add_passing",
    "spend_playable_value_add_turn",
    "use_playable_value_add",
]
