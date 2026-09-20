"""Result projection for a Produce pursuit lesson (追い込みレッスン)."""

from __future__ import annotations

from dataclasses import dataclass

from .lesson_targets import LessonScoreTargets


@dataclass(frozen=True, slots=True)
class PursuitLessonResult:
    score: int
    capped_score: int
    tier: str
    pursuit_bonus_total: int
    parameter_gain_each: int
    undistributed_remainder: int

    @property
    def split_is_exact(self) -> bool:
        return self.undistributed_remainder == 0


def calculate_pursuit_lesson_result(
    score: int,
    targets: LessonScoreTargets,
) -> PursuitLessonResult:
    """Project the result using the two values stored in lesson-level Master.

    The official help defines pursuit bonus as the parameter earned after
    CLEAR and says that it is distributed evenly to Vocal, Dance, and Visual.
    We expose any integer remainder instead of guessing the game's tie-break.
    """

    if score < 0:
        raise ValueError("lesson score cannot be negative")

    capped_score = min(score, targets.perfect)
    if score < targets.clear:
        tier = "FAIL"
        pursuit_bonus_total = 0
    else:
        tier = "PERFECT" if score >= targets.perfect else "CLEAR"
        pursuit_bonus_total = capped_score - targets.clear

    parameter_gain_each, remainder = divmod(pursuit_bonus_total, 3)
    return PursuitLessonResult(
        score=score,
        capped_score=capped_score,
        tier=tier,
        pursuit_bonus_total=pursuit_bonus_total,
        parameter_gain_each=parameter_gain_each,
        undistributed_remainder=remainder,
    )


def verified_pursuit_parameter_split(
    score: int,
    targets: LessonScoreTargets,
) -> tuple[int, int, int]:
    """Return the equal Vo/Da/Vi pursuit bonus only when it is integral.

    Master provides score gates, while the client receives final attribute
    growth from the server.  This helper therefore exposes only the documented
    equal split of the post-CLEAR pursuit bonus and fails closed when the
    integer tie-break would be needed.
    """

    result = calculate_pursuit_lesson_result(score, targets)
    if not result.split_is_exact:
        raise ValueError(
            "pursuit parameter split has an unresolved native remainder: "
            f"{result.undistributed_remainder}"
        )
    return (result.parameter_gain_each,) * 3
