"""Immutable cursor for one source-neutral Produce outer transaction.

The cursor owns no recognition, controller, persistence, or retry policy.  It
only records an already-authoritative ordered surface contract and moves when
its caller supplies an explicit successor or terminal receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable


PHASE_AWAITING = "awaiting"
PHASE_SUBMITTED = "submitted"
PHASE_SETTLING = "settling"
PHASE_COMPLETED = "completed"
_PHASES = frozenset(
    {PHASE_AWAITING, PHASE_SUBMITTED, PHASE_SETTLING, PHASE_COMPLETED}
)


@runtime_checkable
class SemanticSurface(Protocol):
    """Structural contract shared with ``ExpectedSurface``."""

    kind: str
    target: str
    source_id: str
    ordinal: int
    optional: bool


@dataclass(frozen=True, slots=True)
class TransactionOwner:
    run_id: str
    produce_id: str
    week: int
    selected_action: str

    def __post_init__(self) -> None:
        for name in ("run_id", "produce_id", "selected_action"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"owner {name} must be non-empty text")
        if isinstance(self.week, bool) or not isinstance(self.week, int):
            raise TypeError("owner week must be an integer")
        if self.week < 1:
            raise ValueError("owner week must be positive")


def _surface_signature(surface: SemanticSurface) -> tuple[str, str, str, int, bool]:
    try:
        signature = (
            surface.kind,
            surface.target,
            surface.source_id,
            surface.ordinal,
            surface.optional,
        )
    except AttributeError as error:
        raise TypeError("surface does not satisfy SemanticSurface") from error
    kind, target, source_id, ordinal, optional = signature
    if any(not isinstance(value, str) or not value for value in (kind, target, source_id)):
        raise ValueError("surface kind, target, and source_id must be non-empty text")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        raise TypeError("surface ordinal must be an integer")
    if type(optional) is not bool:
        raise TypeError("surface optional must be boolean")
    return signature


def _validate_surfaces(
    surfaces: tuple[SemanticSurface, ...],
) -> tuple[SemanticSurface, ...]:
    values = tuple(surfaces)
    if not values:
        raise ValueError("transaction surfaces must not be empty")
    for expected_ordinal, surface in enumerate(values, start=1):
        signature = _surface_signature(surface)
        if signature[3] != expected_ordinal:
            raise ValueError("surface ordinals must be contiguous and start at 1")
    return values


def _validate_receipt_id(receipt_id: str) -> None:
    if not isinstance(receipt_id, str) or not receipt_id:
        raise ValueError("receipt_id must be non-empty text")


@dataclass(frozen=True, slots=True)
class ProduceOuterTransaction:
    """Cursor over exactly one selected outer action's semantic surfaces."""

    transaction_id: str
    owner: TransactionOwner
    surfaces: tuple[SemanticSurface, ...]
    current_ordinal: int = 1
    phase: str = PHASE_AWAITING
    receipt_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.transaction_id, str) or not self.transaction_id:
            raise ValueError("transaction_id must be non-empty text")
        if not isinstance(self.owner, TransactionOwner):
            raise TypeError("owner must be TransactionOwner")
        surfaces = _validate_surfaces(tuple(self.surfaces))
        object.__setattr__(self, "surfaces", surfaces)
        if isinstance(self.current_ordinal, bool) or not isinstance(
            self.current_ordinal, int
        ):
            raise TypeError("current_ordinal must be an integer")
        maximum = len(surfaces) + (1 if self.phase == PHASE_COMPLETED else 0)
        if self.current_ordinal < 1 or self.current_ordinal > maximum:
            raise ValueError("current_ordinal is outside the surface chain")
        if self.phase not in _PHASES:
            raise ValueError(f"unsupported transaction phase {self.phase!r}")
        receipts = tuple(self.receipt_ids)
        if any(not isinstance(value, str) or not value for value in receipts):
            raise ValueError("receipt_ids must contain non-empty text")
        if len(receipts) != len(set(receipts)):
            raise ValueError("receipt_ids must not contain duplicates")
        object.__setattr__(self, "receipt_ids", receipts)

    @classmethod
    def begin(
        cls,
        transaction_id: str,
        owner: TransactionOwner,
        surfaces: tuple[SemanticSurface, ...],
    ) -> ProduceOuterTransaction:
        return cls(transaction_id, owner, tuple(surfaces))

    @property
    def current_surface(self) -> SemanticSurface | None:
        if self.phase == PHASE_COMPLETED:
            return None
        return self.surfaces[self.current_ordinal - 1]

    @property
    def consumed_surfaces(self) -> tuple[SemanticSurface, ...]:
        consumed = min(self.current_ordinal - 1, len(self.surfaces))
        return self.surfaces[:consumed]

    def _with_receipt(self, receipt_id: str, **changes: object) -> ProduceOuterTransaction:
        _validate_receipt_id(receipt_id)
        if receipt_id in self.receipt_ids:
            return self
        if self.phase == PHASE_COMPLETED:
            raise ValueError("completed transaction cannot accept another receipt")
        return replace(self, receipt_ids=(*self.receipt_ids, receipt_id), **changes)

    def record_submission(self, receipt_id: str) -> ProduceOuterTransaction:
        """Record command acceptance without consuming the current surface."""

        if self.phase == PHASE_COMPLETED and receipt_id not in self.receipt_ids:
            return self._with_receipt(receipt_id)
        if self.phase != PHASE_AWAITING and receipt_id not in self.receipt_ids:
            raise ValueError("submission requires an awaiting transaction")
        return self._with_receipt(receipt_id, phase=PHASE_SUBMITTED)

    def record_settling(self, receipt_id: str) -> ProduceOuterTransaction:
        """Record settlement activity without consuming the current surface."""

        if self.phase == PHASE_COMPLETED and receipt_id not in self.receipt_ids:
            return self._with_receipt(receipt_id)
        if (
            self.phase not in {PHASE_SUBMITTED, PHASE_SETTLING}
            and receipt_id not in self.receipt_ids
        ):
            raise ValueError("settling requires a submitted transaction")
        return self._with_receipt(receipt_id, phase=PHASE_SETTLING)

    def record_successor(
        self,
        receipt_id: str,
        successor_ordinal: int,
    ) -> ProduceOuterTransaction:
        """Advance only to an explicitly authoritative successor surface.

        Intermediate surfaces may be skipped only when every skipped surface is
        marked optional.  The current surface itself is consumed by the
        successor authority.
        """

        _validate_receipt_id(receipt_id)
        if receipt_id in self.receipt_ids:
            return self
        if isinstance(successor_ordinal, bool) or not isinstance(
            successor_ordinal, int
        ):
            raise TypeError("successor_ordinal must be an integer")
        if successor_ordinal <= self.current_ordinal or successor_ordinal > len(
            self.surfaces
        ):
            raise ValueError("successor must be a later surface in this transaction")
        skipped = self.surfaces[self.current_ordinal : successor_ordinal - 1]
        if any(not surface.optional for surface in skipped):
            raise ValueError("successor cannot skip a required surface")
        return self._with_receipt(
            receipt_id,
            current_ordinal=successor_ordinal,
            phase=PHASE_AWAITING,
        )

    def record_terminal(self, receipt_id: str) -> ProduceOuterTransaction:
        """Complete on explicit terminal authority; no inferred advancement."""

        return self._with_receipt(
            receipt_id,
            current_ordinal=len(self.surfaces) + 1,
            phase=PHASE_COMPLETED,
        )

    def record_week_successor(
        self,
        receipt_id: str,
        *,
        run_id: str,
        produce_id: str,
        week: int,
    ) -> ProduceOuterTransaction:
        """Treat an authoritative next week in the same run as terminal proof."""

        _validate_receipt_id(receipt_id)
        if receipt_id in self.receipt_ids:
            return self
        if run_id != self.owner.run_id or produce_id != self.owner.produce_id:
            raise ValueError("week successor must belong to the same run and produce")
        if isinstance(week, bool) or not isinstance(week, int):
            raise TypeError("successor week must be an integer")
        if week <= self.owner.week:
            raise ValueError("successor week must be later than the owner week")
        return self.record_terminal(receipt_id)

    def rebind(
        self,
        surfaces: tuple[SemanticSurface, ...],
    ) -> ProduceOuterTransaction:
        """Expand/refine a chain while preserving its already-consumed prefix."""

        if self.phase == PHASE_COMPLETED:
            raise ValueError("completed transaction cannot be rebound")
        rebound = _validate_surfaces(tuple(surfaces))
        consumed_count = self.current_ordinal - 1
        if len(rebound) < self.current_ordinal:
            raise ValueError("rebound chain cannot remove the current cursor")
        old_prefix = tuple(
            _surface_signature(value) for value in self.surfaces[:consumed_count]
        )
        new_prefix = tuple(
            _surface_signature(value) for value in rebound[:consumed_count]
        )
        if old_prefix != new_prefix:
            raise ValueError("rebound chain does not preserve consumed prefix")
        if self.phase in {PHASE_SUBMITTED, PHASE_SETTLING} and _surface_signature(
            self.surfaces[self.current_ordinal - 1]
        ) != _surface_signature(rebound[self.current_ordinal - 1]):
            raise ValueError("rebound chain cannot replace an in-flight surface")
        return replace(self, surfaces=rebound)


__all__ = [
    "PHASE_AWAITING",
    "PHASE_COMPLETED",
    "PHASE_SETTLING",
    "PHASE_SUBMITTED",
    "ProduceOuterTransaction",
    "SemanticSurface",
    "TransactionOwner",
]
