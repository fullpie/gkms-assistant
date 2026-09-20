"""Observed-state rating targets for the Initial scenario.

This module deliberately starts at the final-exam boundary.  Ordinary lesson,
memory, support, and event gains are allowed to settle in the game; callers
then provide the observed pre-final Vo/Da/Vi values.  The estimator therefore
does not pretend to predict every intermediate attribute mutation.

Grade thresholds and per-attribute caps come from the local Master tables.
The Initial rating decomposition is the community-verified calculator
contract: capped post-rank parameters contribute ``floor(sum * 2.3)``, final
placement contributes a fixed amount, and final-exam score uses diminishing
piecewise rates.  Keeping this in a separate module makes that non-Master
provenance explicit and easy to recalibrate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .produce_grade_targets import ProduceGradeTargets, load_produce_grade_targets


INITIAL_RATING_FORMULA_VERSION = "community-initial-rating-2024-09-v1"

# (exclusive upper score boundary, rating numerator / 100)
_SCORE_SEGMENTS: tuple[tuple[int | None, int], ...] = (
    (5_000, 30),
    (10_000, 15),
    (20_000, 8),
    (30_000, 4),
    (40_000, 2),
    (None, 1),
)

_PLACEMENT: dict[int, tuple[int, int]] = {
    1: (1_700, 30),
    2: (900, 20),
    3: (500, 10),
}


def _non_negative_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def initial_exam_score_rating(score: int) -> int:
    """Convert an Initial final-exam score to its diminishing rating points."""

    remaining = _non_negative_int(score, "score")
    lower = 0
    rating = 0
    for upper, numerator in _SCORE_SEGMENTS:
        width = remaining if upper is None else min(remaining, upper - lower)
        rating += width * numerator // 100
        remaining -= width
        if remaining == 0:
            break
        if upper is None:
            raise AssertionError("unbounded score segment did not consume input")
        lower = upper
    return rating


def required_initial_exam_score(required_rating_points: int) -> int:
    """Return the smallest exam score producing ``required_rating_points``."""

    target = _non_negative_int(required_rating_points, "required_rating_points")
    if target == 0:
        return 0
    high = 1
    while initial_exam_score_rating(high) < target:
        high *= 2
    low = 0
    while low < high:
        middle = (low + high) // 2
        if initial_exam_score_rating(middle) >= target:
            high = middle
        else:
            low = middle + 1
    return low


@dataclass(frozen=True, slots=True)
class InitialGradeEstimate:
    produce_id: str
    formula_version: str
    target_grade: str
    target_rating_points: int
    placement: int
    observed_pre_final_parameters: tuple[int, int, int]
    post_placement_parameters: tuple[int, int, int]
    parameter_rating_points: int
    placement_rating_points: int
    exam_score: int
    exam_rating_points: int
    total_rating_points: int
    achieved_grade: str
    rating_gap: int
    required_exam_score_for_target: int


def _achieved_grade(targets: ProduceGradeTargets, rating_points: int) -> str:
    achieved = targets.thresholds[0].grade
    for row in targets.thresholds:
        if row.rating_points > rating_points:
            break
        achieved = row.grade
    return achieved


def estimate_initial_grade(
    produce_id: str,
    *,
    vocal: int,
    dance: int,
    visual: int,
    final_exam_score: int,
    final_placement: int = 1,
    target_grade: str = "SSS",
) -> InitialGradeEstimate:
    """Estimate Initial rating from observed pre-final parameters.

    ``vocal``/``dance``/``visual`` must be the values immediately before the
    final exam.  The placement parameter bonus is applied and capped here.
    """

    if produce_id not in {"produce-001", "produce-002", "produce-003"}:
        raise ValueError("Initial grade estimator only supports produce-001..003")
    parameters = tuple(
        _non_negative_int(value, field)
        for value, field in zip(
            (vocal, dance, visual), ("vocal", "dance", "visual"), strict=True
        )
    )
    score = _non_negative_int(final_exam_score, "final_exam_score")
    try:
        placement_points, parameter_bonus = _PLACEMENT[final_placement]
    except (KeyError, TypeError) as error:
        raise ValueError("final_placement must be 1, 2, or 3") from error

    targets = load_produce_grade_targets(produce_id)
    target_points = targets.rating_points_for(target_grade)
    post = tuple(min(targets.attribute_cap, value + parameter_bonus) for value in parameters)
    parameter_points = sum(post) * 23 // 10
    exam_points = initial_exam_score_rating(score)
    total = parameter_points + placement_points + exam_points
    non_exam_points = parameter_points + placement_points
    required_exam_points = max(0, target_points - non_exam_points)
    return InitialGradeEstimate(
        produce_id=produce_id,
        formula_version=INITIAL_RATING_FORMULA_VERSION,
        target_grade=target_grade,
        target_rating_points=target_points,
        placement=final_placement,
        observed_pre_final_parameters=parameters,
        post_placement_parameters=post,
        parameter_rating_points=parameter_points,
        placement_rating_points=placement_points,
        exam_score=score,
        exam_rating_points=exam_points,
        total_rating_points=total,
        achieved_grade=_achieved_grade(targets, total),
        rating_gap=max(0, target_points - total),
        required_exam_score_for_target=required_initial_exam_score(
            required_exam_points
        ),
    )


__all__ = [
    "INITIAL_RATING_FORMULA_VERSION",
    "InitialGradeEstimate",
    "estimate_initial_grade",
    "initial_exam_score_rating",
    "required_initial_exam_score",
]
