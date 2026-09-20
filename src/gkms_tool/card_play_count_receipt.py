"""Plan-neutral receipts for exact per-card ``PlayCardCount`` mutations.

A receipt is execution evidence for one native increment on one GUID.  It is
not a capability flag and must be emitted only by the operation owner that
performed the mutation.  Plan 2 keeps its historical public names as aliases;
Plan 3 owner names are reserved for its later isolated ordinary-play slice.
"""

from __future__ import annotations

from dataclasses import dataclass


CARD_PLAY_COUNT_RECEIPT_FAMILY = "card-play-count"
CARD_PLAY_COUNT_INCREMENT_OPERATION = "increment"

# Historical Plan 2 owner values are immutable compatibility contracts.
PLAN2_CARD_PLAY_COUNT_BUILD_OWNER = "plan2-native-horizon.execute-play.build"
PLAN2_CARD_PLAY_COUNT_MOVE_OWNER = "plan2-native-horizon.execute-play.move"

# Reserved only.  No Plan 3 producer emits these in this extraction slice.
PLAN3_ORDINARY_CARD_PLAY_COUNT_BUILD_OWNER = (
    "plan3-native-search.ordinary.build"
)
PLAN3_ORDINARY_CARD_PLAY_COUNT_MOVE_OWNER = (
    "plan3-native-search.ordinary.move"
)

# Legacy spellings retained for Plan 2 imports and serialized expectations.
CARD_PLAY_COUNT_BUILD_OWNER = PLAN2_CARD_PLAY_COUNT_BUILD_OWNER
CARD_PLAY_COUNT_MOVE_OWNER = PLAN2_CARD_PLAY_COUNT_MOVE_OWNER

PLAN2_CARD_PLAY_COUNT_RECEIPT_OWNERS = frozenset(
    (PLAN2_CARD_PLAY_COUNT_BUILD_OWNER, PLAN2_CARD_PLAY_COUNT_MOVE_OWNER)
)
PLAN3_ORDINARY_CARD_PLAY_COUNT_RECEIPT_OWNERS = frozenset(
    (
        PLAN3_ORDINARY_CARD_PLAY_COUNT_BUILD_OWNER,
        PLAN3_ORDINARY_CARD_PLAY_COUNT_MOVE_OWNER,
    )
)
CARD_PLAY_COUNT_RECEIPT_OWNERS = (
    PLAN2_CARD_PLAY_COUNT_RECEIPT_OWNERS
    | PLAN3_ORDINARY_CARD_PLAY_COUNT_RECEIPT_OWNERS
)
CARD_PLAY_COUNT_ALLOWED_OWNER_SEQUENCES = frozenset(
    {
        (
            PLAN2_CARD_PLAY_COUNT_BUILD_OWNER,
            PLAN2_CARD_PLAY_COUNT_MOVE_OWNER,
        ),
        (
            PLAN3_ORDINARY_CARD_PLAY_COUNT_BUILD_OWNER,
            PLAN3_ORDINARY_CARD_PLAY_COUNT_MOVE_OWNER,
        ),
    }
)


def _plain_int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class CardPlayCountOperationReceipt:
    """Proof that one registered owner completed one GUID-local increment."""

    family: str
    operation: str
    subject_id: str
    owner: str
    before_value: int
    after_value: int

    def __post_init__(self) -> None:
        family = _nonempty(self.family, "receipt family")
        operation = _nonempty(self.operation, "receipt operation")
        _nonempty(self.subject_id, "receipt subject_id")
        owner = _nonempty(self.owner, "receipt owner")
        before = _plain_int(self.before_value, "receipt before_value")
        after = _plain_int(self.after_value, "receipt after_value")
        if family != CARD_PLAY_COUNT_RECEIPT_FAMILY:
            raise ValueError(f"unsupported operation receipt family: {family}")
        if operation != CARD_PLAY_COUNT_INCREMENT_OPERATION:
            raise ValueError(
                f"unsupported card play-count operation: {operation}"
            )
        if owner not in CARD_PLAY_COUNT_RECEIPT_OWNERS:
            raise ValueError(f"unsupported card play-count owner: {owner}")
        if after != before + 1:
            raise ValueError(
                "card play-count receipt must describe one increment"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "family": self.family,
            "operation": self.operation,
            "subject_id": self.subject_id,
            "owner": self.owner,
            "before_value": self.before_value,
            "after_value": self.after_value,
        }


__all__ = [
    "CARD_PLAY_COUNT_ALLOWED_OWNER_SEQUENCES",
    "CARD_PLAY_COUNT_BUILD_OWNER",
    "CARD_PLAY_COUNT_INCREMENT_OPERATION",
    "CARD_PLAY_COUNT_MOVE_OWNER",
    "CARD_PLAY_COUNT_RECEIPT_FAMILY",
    "CARD_PLAY_COUNT_RECEIPT_OWNERS",
    "PLAN2_CARD_PLAY_COUNT_BUILD_OWNER",
    "PLAN2_CARD_PLAY_COUNT_MOVE_OWNER",
    "PLAN2_CARD_PLAY_COUNT_RECEIPT_OWNERS",
    "PLAN3_ORDINARY_CARD_PLAY_COUNT_BUILD_OWNER",
    "PLAN3_ORDINARY_CARD_PLAY_COUNT_MOVE_OWNER",
    "PLAN3_ORDINARY_CARD_PLAY_COUNT_RECEIPT_OWNERS",
    "CardPlayCountOperationReceipt",
]
