"""Canonical NIA Pro/Master week and audition-stage identities."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


_POSITIONS: Mapping[str, Mapping[str, tuple[int, int]]] = MappingProxyType(
    {
        "produce-004": MappingProxyType(
            {
                "exam-step:16": (9, 1),
                "exam-step:17": (18, 2),
                "exam-step:18": (27, 3),
            }
        ),
        "produce-005": MappingProxyType(
            {
                "exam-step:16": (9, 1),
                "exam-step:17": (17, 2),
                "exam-step:18": (26, 3),
            }
        ),
    }
)


def nia_stage_positions(produce_id: str) -> Mapping[str, tuple[int, int]]:
    try:
        return _POSITIONS[produce_id]
    except KeyError as error:
        raise ValueError(f"unsupported NIA produce identity: {produce_id!r}") from error


def nia_final_week(produce_id: str) -> int:
    return nia_stage_positions(produce_id)["exam-step:18"][0]


def nia_phase_for_week(produce_id: str, week: int) -> int | None:
    if isinstance(week, bool) or not isinstance(week, int):
        raise TypeError("NIA route week must be an integer")
    positions = nia_stage_positions(produce_id)
    mid1 = positions["exam-step:16"][0]
    mid2 = positions["exam-step:17"][0]
    final = positions["exam-step:18"][0]
    if 1 <= week <= mid1:
        return 1
    if week <= mid2:
        return 2
    if week <= final:
        return 3
    return None


__all__ = ["nia_final_week", "nia_phase_for_week", "nia_stage_positions"]
