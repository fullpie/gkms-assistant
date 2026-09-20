"""Exact Android 3.2.3 exam RNG and card-pool ordering.

The implementation is intentionally small and stateless.  It mirrors the
ARM64 bodies of ``ExamParameterModel.GetRandomInt`` and both proven branches
of ``ExamCardPoolModel.Shuffle``.  Ordinary pools use Fisher--Yates.  A pool
containing a positive ``fixedDeckOrder`` is sorted by that signed integer and
consumes no RNG.  Equal comparator keys remain rejected because native
``List.Sort`` does not promise a stable tie order.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar


UINT32_MASK = 0xFFFFFFFF
INT32_MIN = -(1 << 31)
INT32_MAX = (1 << 31) - 1

T = TypeVar("T")


class FixedDeckOrderError(ValueError):
    """Raised when an exact native fixed-order result cannot be projected."""


def _uint32(value: int, *, name: str = "state") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= UINT32_MASK:
        raise ValueError(f"{name} must be in [0, 0xFFFFFFFF]")
    return value


def _int32(value: int) -> int:
    value &= UINT32_MASK
    return value if value <= INT32_MAX else value - (1 << 32)


def advance_state(state: int) -> int:
    """Advance one native exam RNG step using xorshift32 (13, 17, 5)."""

    value = _uint32(state)
    value ^= (value << 13) & UINT32_MASK
    value ^= value >> 17
    value ^= (value << 5) & UINT32_MASK
    return value & UINT32_MASK


def next_int32(state: int) -> tuple[int, int]:
    """Return ``(value, next_state)`` for parameterless ``GetRandomInt``.

    The native method returns the *old* state's sign-bit-flipped bit pattern,
    then stores the xorshift32 successor.
    """

    old_state = _uint32(state)
    return _int32(old_state ^ 0x80000000), advance_state(old_state)


def next_range(state: int, minimum: int, maximum: int) -> tuple[int, int]:
    """Return ``(value, next_state)`` for ``GetRandomInt(minimum, maximum)``.

    ``maximum`` is exclusive.  The native mapping is multiply-high rather
    than modulo: ``minimum + high32(old_state * (maximum - minimum))``.
    """

    old_state = _uint32(state)
    for name, value in (("minimum", minimum), ("maximum", maximum)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if not INT32_MIN <= value <= INT32_MAX:
            raise ValueError(f"{name} must be a signed 32-bit integer")
    if minimum >= maximum:
        raise ValueError("minimum must be less than maximum")

    width = maximum - minimum
    # With signed 32-bit endpoints, every non-full-width valid interval fits
    # the uint32 multiplier used by the native UMULL instruction.
    if width > UINT32_MASK:
        raise ValueError("range width must fit in an unsigned 32-bit integer")
    offset = (old_state * width) >> 32
    result = _int32((minimum & UINT32_MASK) + offset)
    return result, advance_state(old_state)


def shuffle_unfixed_pool(
    values: Sequence[T],
    state: int,
    *,
    fixed_deck_orders: Sequence[int] | None = None,
) -> tuple[list[T], int]:
    """Copy and shuffle an ordinary native card pool.

    The native loop is descending Fisher--Yates: for ``count = n .. 2``, it
    samples ``GetRandomInt(0, count)`` and swaps that element with
    ``count - 1``.  If any card has ``fixedDeckOrder > 0``,
    ``ExamCardPoolModel`` sorts instead and consumes no random state.  This
    function rejects that separate path so callers cannot accidentally treat
    it as a random shuffle.
    """

    current_state = _uint32(state)
    result = list(values)
    if fixed_deck_orders is not None:
        if len(fixed_deck_orders) != len(result):
            raise ValueError("fixed_deck_orders must match values length")
        for order in fixed_deck_orders:
            if isinstance(order, bool) or not isinstance(order, int):
                raise TypeError("fixed_deck_orders must contain integers")
            if not INT32_MIN <= order <= INT32_MAX:
                raise ValueError("fixed_deck_orders must contain signed 32-bit integers")
        if any(order > 0 for order in fixed_deck_orders):
            raise FixedDeckOrderError(
                "native ExamCardPoolModel sorts pools containing fixedDeckOrder > 0"
            )

    for count in range(len(result), 1, -1):
        index, current_state = next_range(current_state, 0, count)
        result[index], result[count - 1] = result[count - 1], result[index]
    return result, current_state


def order_native_pool(
    values: Sequence[T],
    state: int,
    *,
    fixed_deck_orders: Sequence[int],
) -> tuple[list[T], int]:
    """Run the exact observable ``ExamCardPoolModel.Shuffle`` branch.

    Android v3.2.3 first checks whether any card has ``fixedDeckOrder > 0``.
    With no positive value it delegates to the ordinary Fisher--Yates loop.
    Otherwise it calls ``List.Sort`` with the signed comparison
    ``a.fixedDeckOrder - b.fixedDeckOrder`` and consumes no RNG.  Distinct,
    non-negative keys therefore have one exact ascending result.  Negative or
    duplicate keys stay fail-closed because subtraction overflow or an
    unstable equal-key order would otherwise be guessed.
    """

    current_state = _uint32(state)
    result = list(values)
    orders = tuple(fixed_deck_orders)
    if len(orders) != len(result):
        raise ValueError("fixed_deck_orders must match values length")
    for order in orders:
        if isinstance(order, bool) or not isinstance(order, int):
            raise TypeError("fixed_deck_orders must contain integers")
        if not INT32_MIN <= order <= INT32_MAX:
            raise ValueError(
                "fixed_deck_orders must contain signed 32-bit integers"
            )

    if not any(order > 0 for order in orders):
        return shuffle_unfixed_pool(
            result,
            current_state,
            fixed_deck_orders=orders,
        )
    if any(order < 0 for order in orders):
        raise FixedDeckOrderError(
            "native fixed-order comparison with negative keys is outside the "
            "observed card-order domain"
        )
    if len(set(orders)) != len(orders):
        raise FixedDeckOrderError(
            "native List.Sort equal fixedDeckOrder tie order is unproven"
        )
    ordered = [
        value
        for _order, value in sorted(
            zip(orders, result, strict=True),
            key=lambda item: item[0],
        )
    ]
    return ordered, current_state
