"""Observed-state N.I.A. grade estimator.

Grade thresholds and attribute caps come from the local Master snapshot.  The
component formula is the published community N.I.A. formula and is labelled
as such; it is used for goals and diagnostics, never to override an observed
game result.
"""

from __future__ import annotations

from dataclasses import dataclass

from .produce_grade_targets import load_produce_grade_targets


NIA_RATING_FORMULA_SOURCE = "community-nia-rating-v1"
_NIA_PRODUCE_IDS = frozenset({"produce-004", "produce-005"})


@dataclass(frozen=True, slots=True)
class NiaFanVoteBand:
    minimum: int
    maximum: int | None
    permille: int
    fixed: int


_FAN_BANDS = (
    NiaFanVoteBand(0, 20_000, 100, 0),
    NiaFanVoteBand(20_001, 40_000, 85, 300),
    NiaFanVoteBand(40_001, 60_000, 70, 900),
    NiaFanVoteBand(60_001, 80_000, 65, 1_200),
    NiaFanVoteBand(80_001, 100_000, 60, 1_600),
    NiaFanVoteBand(100_001, None, 55, 2_100),
)


def nia_fan_vote_rating(vote_count: int) -> int:
    if isinstance(vote_count, bool) or not isinstance(vote_count, int):
        raise TypeError("vote_count must be an integer")
    if vote_count < 0:
        raise ValueError("vote_count must be non-negative")
    band = next(
        value
        for value in _FAN_BANDS
        if vote_count >= value.minimum
        and (value.maximum is None or vote_count <= value.maximum)
    )
    return vote_count * band.permille // 1000 + band.fixed


def nia_parameter_rating(vocal: int, dance: int, visual: int) -> int:
    values = (vocal, dance, visual)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise TypeError("N.I.A. parameters must be integers")
    if any(value < 0 for value in values):
        raise ValueError("N.I.A. parameters must be non-negative")
    return sum(values) * 23 // 10


@dataclass(frozen=True, slots=True)
class NiaGradeEstimate:
    produce_id: str
    vocal: int
    dance: int
    visual: int
    vote_count: int
    parameter_rating: int
    fan_vote_rating: int
    total_rating: int
    achieved_grade: str
    target_grade: str
    target_rating: int
    target_gap: int
    required_parameter_total_at_current_votes: int | None
    required_vote_count_at_current_parameters: int
    formula_source: str = NIA_RATING_FORMULA_SOURCE


def _ceil_div(value: int, divisor: int) -> int:
    return -(-value // divisor)


def estimate_nia_grade(
    *,
    produce_id: str,
    vocal: int,
    dance: int,
    visual: int,
    vote_count: int,
    target_grade: str = "SSS",
) -> NiaGradeEstimate:
    if produce_id not in _NIA_PRODUCE_IDS:
        raise ValueError("N.I.A. estimator accepts produce-004 or produce-005")
    targets = load_produce_grade_targets(produce_id)
    values = (vocal, dance, visual)
    parameter = nia_parameter_rating(*values)
    if any(value > targets.attribute_cap for value in values):
        raise ValueError("observed parameter exceeds the mode's Master cap")
    votes = nia_fan_vote_rating(vote_count)
    total = parameter + votes
    target = targets.rating_points_for(target_grade)
    achieved = targets.thresholds[0].grade
    for threshold in targets.thresholds:
        if total >= threshold.rating_points:
            achieved = threshold.grade
        else:
            break
    needed_parameter_rating = max(0, target - votes)
    required_parameter_total = _ceil_div(needed_parameter_rating * 10, 23)
    if required_parameter_total > targets.attribute_cap * 3:
        required_parameter_total_or_none = None
    else:
        required_parameter_total_or_none = required_parameter_total

    needed_vote_rating = max(0, target - parameter)
    low, high = 0, max(100_001, vote_count, 1)
    while nia_fan_vote_rating(high) < needed_vote_rating:
        high *= 2
    while low < high:
        middle = (low + high) // 2
        if nia_fan_vote_rating(middle) >= needed_vote_rating:
            high = middle
        else:
            low = middle + 1
    return NiaGradeEstimate(
        produce_id=produce_id,
        vocal=vocal,
        dance=dance,
        visual=visual,
        vote_count=vote_count,
        parameter_rating=parameter,
        fan_vote_rating=votes,
        total_rating=total,
        achieved_grade=achieved,
        target_grade=target_grade,
        target_rating=target,
        target_gap=max(0, target - total),
        required_parameter_total_at_current_votes=(
            required_parameter_total_or_none
        ),
        required_vote_count_at_current_parameters=low,
    )


__all__ = [
    "NIA_RATING_FORMULA_SOURCE",
    "NiaFanVoteBand",
    "NiaGradeEstimate",
    "estimate_nia_grade",
    "nia_fan_vote_rating",
    "nia_parameter_rating",
]
