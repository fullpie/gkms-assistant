"""Pure Android v3.2.3 exam-context construction rules.

The functions in this module consume already parsed Master values and already
observed/shadow state.  They do not read process memory, inspect screenshots,
or guess missing values.  Invalid or incomplete input raises
``ExamContextDomainError``.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import Enum

from .native_exam_formula import ProduceParameterType


INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1


class ExamContextDomainError(ValueError):
    """The supplied Master/observed state is incomplete or out of domain."""


def _int32(value: object, label: str, *, non_negative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExamContextDomainError(f"{label} must be a present integer")
    if not INT32_MIN <= value <= INT32_MAX:
        raise ExamContextDomainError(f"{label} is outside Int32: {value}")
    if non_negative and value < 0:
        raise ExamContextDomainError(f"{label} must be non-negative: {value}")
    return value


def _checked_add(left: int, right: int, label: str) -> int:
    return _int32(left + right, label)


def _checked_sub(left: int, right: int, label: str) -> int:
    return _int32(left - right, label)


def _checked_mul(left: int, right: int, label: str) -> int:
    return _int32(left * right, label)


def _parameter_type(value: object) -> ProduceParameterType:
    if isinstance(value, bool):
        raise ExamContextDomainError(f"unsupported parameter type: {value!r}")
    try:
        resolved = ProduceParameterType(value)
    except (TypeError, ValueError) as error:
        raise ExamContextDomainError(
            f"unsupported parameter type: {value!r}"
        ) from error
    if resolved not in {
        ProduceParameterType.VOCAL,
        ProduceParameterType.DANCE,
        ProduceParameterType.VISUAL,
    }:
        raise ExamContextDomainError(f"unsupported parameter type: {resolved!r}")
    return resolved


def _f32(value: int | float) -> float:
    try:
        result = struct.unpack("<f", struct.pack("<f", float(value)))[0]
    except (OverflowError, struct.error) as error:
        raise ExamContextDomainError(f"value is outside float32: {value!r}") from error
    if not math.isfinite(result):
        raise ExamContextDomainError(f"non-finite float32 is unsupported: {value!r}")
    return result


def _fadd(left: int | float, right: int | float) -> float:
    return _f32(_f32(left) + _f32(right))


def _fmul(left: int | float, right: int | float) -> float:
    return _f32(_f32(left) * _f32(right))


def _fdiv(left: int | float, right: int | float) -> float:
    denominator = _f32(right)
    if denominator == 0.0:
        raise ExamContextDomainError("float32 division by zero")
    return _f32(_f32(left) / denominator)


def _float_from_permil(value: int) -> float:
    return _fdiv(_f32(value), _f32(1000.0))


@dataclass(frozen=True, slots=True)
class ParameterValues:
    vocal: int
    dance: int
    visual: int

    def validated(self, label: str) -> "ParameterValues":
        return ParameterValues(
            vocal=_int32(self.vocal, f"{label}.vocal", non_negative=True),
            dance=_int32(self.dance, f"{label}.dance", non_negative=True),
            visual=_int32(self.visual, f"{label}.visual", non_negative=True),
        )

    def for_type(self, parameter_type: ProduceParameterType) -> int:
        resolved = _parameter_type(parameter_type)
        if resolved is ProduceParameterType.VOCAL:
            return self.vocal
        if resolved is ProduceParameterType.DANCE:
            return self.dance
        if resolved is ProduceParameterType.VISUAL:
            return self.visual
        raise AssertionError(resolved)  # pragma: no cover - exhaustive enum


@dataclass(frozen=True, slots=True)
class LessonMasterValues:
    """Resolved ``ProduceStepLessonLevel`` fields needed by the factory."""

    success_threshold: int
    result_target_value_limit: int


@dataclass(frozen=True, slots=True)
class LessonShadowState:
    parameter_type: ProduceParameterType
    current_parameters: ParameterValues
    parameter_limits: ParameterValues
    lesson_limit_up_score: int
    is_hard: bool


@dataclass(frozen=True, slots=True)
class LessonBorders:
    clear_border: int
    origin_clear_border: int
    limit_border: int


def calculate_lesson_borders(
    master: LessonMasterValues,
    state: LessonShadowState,
) -> LessonBorders:
    """Build the normal/Hard Lesson borders used by ``ExamData``.

    ``master`` must be the already selected ``ProduceStepLessonLevel`` row.
    ``state`` supplies every runtime-dependent input explicitly.
    """

    if not isinstance(master, LessonMasterValues):
        raise ExamContextDomainError("master must be LessonMasterValues")
    if not isinstance(state, LessonShadowState):
        raise ExamContextDomainError("state must be LessonShadowState")
    success = _int32(
        master.success_threshold,
        "success_threshold",
        non_negative=True,
    )
    target_limit = _int32(
        master.result_target_value_limit,
        "result_target_value_limit",
        non_negative=True,
    )
    limit_up = _int32(
        state.lesson_limit_up_score,
        "lesson_limit_up_score",
        non_negative=True,
    )
    if not isinstance(state.is_hard, bool):
        raise ExamContextDomainError("is_hard must be a present boolean")

    current = state.current_parameters.validated("current_parameters")
    limits = state.parameter_limits.validated("parameter_limits")
    remaining = ParameterValues(
        vocal=_checked_sub(limits.vocal, current.vocal, "remaining vocal"),
        dance=_checked_sub(limits.dance, current.dance, "remaining dance"),
        visual=_checked_sub(limits.visual, current.visual, "remaining visual"),
    )
    if min(remaining.vocal, remaining.dance, remaining.visual) < 0:
        raise ExamContextDomainError("current parameter exceeds its parameter limit")

    parameter_type = _parameter_type(state.parameter_type)
    selected_remaining = remaining.for_type(parameter_type)
    clear = min(success, selected_remaining)
    base_limit = _checked_add(
        target_limit,
        limit_up,
        "result target value limit plus lesson limit-up score",
    )
    if not state.is_hard:
        limit = min(base_limit, selected_remaining)
    else:
        after_clear = ParameterValues(
            vocal=(
                _checked_sub(remaining.vocal, clear, "hard vocal remainder")
                if parameter_type is ProduceParameterType.VOCAL
                else remaining.vocal
            ),
            dance=(
                _checked_sub(remaining.dance, clear, "hard dance remainder")
                if parameter_type is ProduceParameterType.DANCE
                else remaining.dance
            ),
            visual=(
                _checked_sub(remaining.visual, clear, "hard visual remainder")
                if parameter_type is ProduceParameterType.VISUAL
                else remaining.visual
            ),
        )
        maximum_remainder = max(
            after_clear.vocal,
            after_clear.dance,
            after_clear.visual,
        )
        adjusted_master_limit = _checked_add(
            base_limit,
            _checked_sub(clear, success, "hard clear adjustment"),
            "hard adjusted master limit",
        )
        three_parameter_limit = _checked_add(
            _checked_mul(3, maximum_remainder, "hard three-parameter remainder"),
            clear,
            "hard three-parameter limit",
        )
        limit = min(adjusted_master_limit, three_parameter_limit)

    return LessonBorders(
        clear_border=clear,
        origin_clear_border=success,
        limit_border=limit,
    )


@dataclass(frozen=True, slots=True)
class AuditionDifficultyValues:
    """Resolved ``IProduceStepAuditionDifficulty`` factory inputs."""

    rank_threshold: int
    force_end_score: int


@dataclass(frozen=True, slots=True)
class AuditionBorders:
    clear_border: int
    limit_border: int


def calculate_audition_borders(
    difficulty: AuditionDifficultyValues,
) -> AuditionBorders:
    """Apply the audition factory's ForceEndScore-to--1 conversion."""

    if not isinstance(difficulty, AuditionDifficultyValues):
        raise ExamContextDomainError(
            "difficulty must be AuditionDifficultyValues"
        )
    clear = _int32(
        difficulty.rank_threshold,
        "rank_threshold",
        non_negative=True,
    )
    force_end = _int32(difficulty.force_end_score, "force_end_score")
    return AuditionBorders(
        clear_border=clear,
        limit_border=force_end if force_end > 0 else -1,
    )


@dataclass(frozen=True, slots=True)
class AuditionProgressBonusValues:
    vocal_permil: int
    dance_permil: int
    visual_permil: int
    vote_bonus_permil: int
    star_bonus_permil: int
    audition_effect_parameter_bonus_permil: int


@dataclass(frozen=True, slots=True)
class AuditionBonusPermils:
    vocal: int
    dance: int
    visual: int


def _calculate_one_audition_bonus_permil(
    base_permil: int,
    *,
    vote_and_star_permil: int,
    effect_permil: int,
) -> int:
    # Native order at 0x7FD32E0..0x7FD33FC:
    # (1 + effect) * float(base), then * (1 + vote+star), / 10,
    # FCVTPS (ceil), and finally integer * 10.
    effect_factor = _fadd(_float_from_permil(effect_permil), _f32(1.0))
    vote_and_star_factor = _fadd(
        _float_from_permil(vote_and_star_permil),
        _f32(1.0),
    )
    scaled = _fmul(effect_factor, _f32(base_permil))
    scaled = _fmul(scaled, vote_and_star_factor)
    scaled = _fdiv(scaled, _f32(10.0))
    rounded_tens = _int32(math.ceil(scaled), "audition bonus ceil result")
    return _checked_mul(rounded_tens, 10, "audition bonus permil")


def calculate_audition_bonus_permils(
    progress: AuditionProgressBonusValues,
) -> AuditionBonusPermils:
    """Calculate the three final ``ExamData`` battle bonus permils."""

    if not isinstance(progress, AuditionProgressBonusValues):
        raise ExamContextDomainError(
            "progress must be AuditionProgressBonusValues"
        )
    vocal = _int32(progress.vocal_permil, "vocal_permil", non_negative=True)
    dance = _int32(progress.dance_permil, "dance_permil", non_negative=True)
    visual = _int32(progress.visual_permil, "visual_permil", non_negative=True)
    vote = _int32(
        progress.vote_bonus_permil,
        "vote_bonus_permil",
        non_negative=True,
    )
    star = _int32(
        progress.star_bonus_permil,
        "star_bonus_permil",
        non_negative=True,
    )
    effect = _int32(
        progress.audition_effect_parameter_bonus_permil,
        "audition_effect_parameter_bonus_permil",
        non_negative=True,
    )
    vote_and_star = _checked_add(vote, star, "vote plus star bonus permil")
    return AuditionBonusPermils(
        vocal=_calculate_one_audition_bonus_permil(
            vocal,
            vote_and_star_permil=vote_and_star,
            effect_permil=effect,
        ),
        dance=_calculate_one_audition_bonus_permil(
            dance,
            vote_and_star_permil=vote_and_star,
            effect_permil=effect,
        ),
        visual=_calculate_one_audition_bonus_permil(
            visual,
            vote_and_star_permil=vote_and_star,
            effect_permil=effect,
        ),
    )


class CurrentTurnBoundary(Enum):
    NEXT_TURN = "next_turn"
    EXAM_END = "exam_end"


@dataclass(frozen=True, slots=True)
class CurrentTurnState:
    current_turn: int
    current_turn_total_add_parameter: int
    exam_end_complete: bool = False


@dataclass(frozen=True, slots=True)
class CurrentTurnBoundaryResult:
    logged_turn: int
    logged_add_parameter: int
    after_log_reset: CurrentTurnState
    after_boundary: CurrentTurnState


def close_current_turn(
    state: CurrentTurnState,
    boundary: CurrentTurnBoundary,
) -> CurrentTurnBoundaryResult:
    """Model AddCurrentTurnLog reset followed by turn advance/exam complete.

    This helper starts with an active turn (turn 1 or later).  It intentionally
    exposes the intermediate post-log state, where the total is already zero
    but CurrentTurn/ExamEndComplete have not changed yet.
    """

    if not isinstance(state, CurrentTurnState):
        raise ExamContextDomainError("state must be CurrentTurnState")
    try:
        resolved_boundary = CurrentTurnBoundary(boundary)
    except (TypeError, ValueError) as error:
        raise ExamContextDomainError(f"unsupported boundary: {boundary!r}") from error
    turn = _int32(state.current_turn, "current_turn", non_negative=True)
    total = _int32(
        state.current_turn_total_add_parameter,
        "current_turn_total_add_parameter",
    )
    if turn < 1:
        raise ExamContextDomainError("cannot close the pre-first-turn state")
    if not isinstance(state.exam_end_complete, bool):
        raise ExamContextDomainError("exam_end_complete must be a present boolean")
    if state.exam_end_complete:
        raise ExamContextDomainError("cannot close a completed exam")

    after_reset = CurrentTurnState(
        current_turn=turn,
        current_turn_total_add_parameter=0,
        exam_end_complete=False,
    )
    if resolved_boundary is CurrentTurnBoundary.NEXT_TURN:
        after_boundary = CurrentTurnState(
            current_turn=_checked_add(turn, 1, "next current turn"),
            current_turn_total_add_parameter=0,
            exam_end_complete=False,
        )
    else:
        after_boundary = CurrentTurnState(
            current_turn=turn,
            current_turn_total_add_parameter=0,
            exam_end_complete=True,
        )
    return CurrentTurnBoundaryResult(
        logged_turn=turn,
        logged_add_parameter=total,
        after_log_reset=after_reset,
        after_boundary=after_boundary,
    )
