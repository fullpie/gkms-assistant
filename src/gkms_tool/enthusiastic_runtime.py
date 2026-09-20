"""Pure typed runtime for native one-turn ``Enthusiastic`` status.

Preservation-family release creates one passing, turn-limited status or
merges into the existing status with the same one-turn lifetime.  Creation
owns the next global status UID; merging preserves identity and the cursor.
The following turn boundary removes the passing status atomically.

This module has no engine, LocalSave, card, or native-search dependency.
Callers provide the released value, global status cursor, and provenance.
Every successful operation returns a self-validating immutable receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


INT32_MAX = (1 << 31) - 1
ENTHUSIASTIC_TURN = 1


class EnthusiasticOperation(str, Enum):
    """Exact status collection operation represented by one receipt."""

    ADD_CREATE = "add-create"
    ADD_MERGE = "add-merge"
    SPEND_REMOVE = "spend-remove"


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
class EnthusiasticStatus:
    """The single exact native release status supported by this kernel."""

    uid: int
    value: int
    turn: int = ENTHUSIASTIC_TURN
    passing_turn_start: bool = True

    def __post_init__(self) -> None:
        _plain_int(self.uid, "enthusiastic status uid", minimum=1)
        _plain_int(self.value, "enthusiastic status value", minimum=1)
        _plain_int(self.turn, "enthusiastic status turn", minimum=1)
        if self.turn != ENTHUSIASTIC_TURN:
            raise ValueError("enthusiastic status turn must be exactly 1")
        if self.passing_turn_start is not True:
            raise ValueError(
                "enthusiastic status must be passing at construction"
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
class EnthusiasticRuntime:
    """Zero-or-one active native Enthusiastic status."""

    status: EnthusiasticStatus | None = None

    def __post_init__(self) -> None:
        if self.status is not None and not isinstance(
            self.status, EnthusiasticStatus
        ):
            raise TypeError(
                "enthusiastic runtime status must be EnthusiasticStatus or None"
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
    runtime: EnthusiasticRuntime,
    next_status_uid: object,
) -> int:
    if not isinstance(runtime, EnthusiasticRuntime):
        raise TypeError("runtime must be EnthusiasticRuntime")
    cursor = _plain_int(next_status_uid, "next_status_uid", minimum=1)
    if runtime.uid is not None and cursor <= runtime.uid:
        raise ValueError(
            "next_status_uid must be greater than the active status uid"
        )
    return cursor


@dataclass(frozen=True, slots=True)
class EnthusiasticReceipt:
    """Typed proof of one completed Enthusiastic collection operation."""

    operation: EnthusiasticOperation
    source: str
    before: EnthusiasticRuntime
    after: EnthusiasticRuntime
    cursor_before: int
    cursor_after: int
    created_uid: int | None = None
    merged_uid: int | None = None
    removed_uid: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, EnthusiasticOperation):
            raise TypeError("operation must be EnthusiasticOperation")
        _source(self.source)
        before_cursor = _validate_cursor(self.before, self.cursor_before)
        after_cursor = _validate_cursor(self.after, self.cursor_after)
        for name in ("created_uid", "merged_uid", "removed_uid"):
            value = getattr(self, name)
            if value is not None:
                _plain_int(value, name, minimum=1)

        before_status = self.before.status
        after_status = self.after.status
        no_created = self.created_uid is None
        no_merged = self.merged_uid is None
        no_removed = self.removed_uid is None
        if self.operation is EnthusiasticOperation.ADD_CREATE:
            valid = (
                before_status is None
                and after_status is not None
                and before_cursor < INT32_MAX
                and after_cursor == before_cursor + 1
                and self.created_uid == before_cursor == after_status.uid
                and no_merged
                and no_removed
            )
        elif self.operation is EnthusiasticOperation.ADD_MERGE:
            valid = (
                before_status is not None
                and after_status is not None
                and after_cursor == before_cursor
                and no_created
                and self.merged_uid == before_status.uid == after_status.uid
                and no_removed
                and after_status.turn == before_status.turn
                and after_status.passing_turn_start
                == before_status.passing_turn_start
                and after_status.value > before_status.value
            )
        else:
            valid = (
                self.operation is EnthusiasticOperation.SPEND_REMOVE
                and before_status is not None
                and before_status.turn == ENTHUSIASTIC_TURN
                and before_status.passing_turn_start
                and after_status is None
                and after_cursor == before_cursor
                and no_created
                and no_merged
                and self.removed_uid == before_status.uid
            )
        if not valid:
            raise ValueError(
                f"inconsistent enthusiastic receipt: {self.operation.value}"
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
            "merged_uid": self.merged_uid,
            "removed_uid": self.removed_uid,
            "value_before": self.value_before,
            "value_after": self.value_after,
        }


def add_enthusiastic(
    runtime: EnthusiasticRuntime,
    value: int,
    *,
    next_status_uid: int,
    source: str,
) -> EnthusiasticReceipt:
    """Create or same-turn merge one positive released value atomically."""

    cursor = _validate_cursor(runtime, next_status_uid)
    amount = _plain_int(value, "enthusiastic add value", minimum=1)
    provenance = _source(source)
    current = runtime.status
    if current is None:
        if cursor == INT32_MAX:
            raise OverflowError("enthusiastic status uid cursor overflow")
        after = EnthusiasticRuntime(
            EnthusiasticStatus(uid=cursor, value=amount)
        )
        return EnthusiasticReceipt(
            operation=EnthusiasticOperation.ADD_CREATE,
            source=provenance,
            before=runtime,
            after=after,
            cursor_before=cursor,
            cursor_after=cursor + 1,
            created_uid=cursor,
        )

    if current.value > INT32_MAX - amount:
        raise OverflowError("enthusiastic status value overflow")
    after = EnthusiasticRuntime(
        replace(current, value=current.value + amount)
    )
    return EnthusiasticReceipt(
        operation=EnthusiasticOperation.ADD_MERGE,
        source=provenance,
        before=runtime,
        after=after,
        cursor_before=cursor,
        cursor_after=cursor,
        merged_uid=current.uid,
    )


def spend_enthusiastic_turn(
    runtime: EnthusiasticRuntime,
    *,
    next_status_uid: int,
    source: str,
) -> EnthusiasticReceipt:
    """Remove the exact passing turn-one status at the next boundary."""

    cursor = _validate_cursor(runtime, next_status_uid)
    provenance = _source(source)
    current = runtime.status
    if current is None:
        raise ValueError("cannot spend absent enthusiastic status")
    return EnthusiasticReceipt(
        operation=EnthusiasticOperation.SPEND_REMOVE,
        source=provenance,
        before=runtime,
        after=EnthusiasticRuntime(),
        cursor_before=cursor,
        cursor_after=cursor,
        removed_uid=current.uid,
    )


def replay_enthusiastic_add_receipts(runtime, receipts, *, cursor_before, cursor_after):
    """Replay completed stance-owner additions without trusting a final mirror.

    Other status families may allocate UIDs between these operations; their
    cursor gaps are retained. Backwards cursors and a changed source runtime
    are refused. No receipt or UID is synthesized by this replay boundary.
    """
    working = runtime
    previous_cursor = _validate_cursor(runtime, cursor_before)
    final_cursor = _plain_int(cursor_after, "final status cursor", minimum=1)
    if final_cursor < previous_cursor:
        raise ValueError("enthusiastic-receipt-cursor-regressed")
    for receipt in receipts:
        if (not isinstance(receipt, EnthusiasticReceipt)
                or receipt.operation not in {EnthusiasticOperation.ADD_CREATE, EnthusiasticOperation.ADD_MERGE}
                or receipt.before != working or receipt.cursor_before < previous_cursor
                or receipt.cursor_after > final_cursor):
            raise ValueError("enthusiastic-receipt-mismatch")
        reproduced = add_enthusiastic(working, receipt.after.value-receipt.before.value,
            next_status_uid=receipt.cursor_before, source=receipt.source)
        if reproduced != receipt:
            raise ValueError("enthusiastic-owner-replay-mismatch")
        working, previous_cursor = reproduced.after, reproduced.cursor_after
    return working


__all__ = [
    "ENTHUSIASTIC_TURN",
    "INT32_MAX",
    "EnthusiasticOperation",
    "EnthusiasticReceipt",
    "EnthusiasticRuntime",
    "EnthusiasticStatus",
    "add_enthusiastic",
    "spend_enthusiastic_turn",
    "replay_enthusiastic_add_receipts",
]
