"""Explicit execution policies for live Produce exams.

The exact simulator and MaaGakumasu's completion player serve different
purposes.  Keeping the choice in a typed policy prevents simulator coverage
from accidentally becoming a navigation prerequisite, while also preventing
an implicit mid-input fallback from submitting the same action twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class ExamExecutionMode(StrEnum):
    """Supported live exam execution modes."""

    EXACT = "exact"
    MAA_COMPLETION_BASELINE = "maa-completion-baseline"


@dataclass(frozen=True, slots=True)
class ExamExecutionPolicy:
    """One immutable contract for a complete exam execution.

    A policy is selected before an exam submits input.  The first completion
    slice intentionally does not switch policies after an exact action has
    been submitted; transitional ExamSave states remain fail-closed.
    """

    mode: ExamExecutionMode
    policy_id: str
    simulator_required: bool
    game_recommendation_allowed: bool
    mid_input_fallback_allowed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ExamExecutionMode):
            raise TypeError("mode must be ExamExecutionMode")
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise ValueError("policy_id must be non-empty text")
        for name in (
            "simulator_required",
            "game_recommendation_allowed",
            "mid_input_fallback_allowed",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "policy_id": self.policy_id,
            "simulator_required": self.simulator_required,
            "game_recommendation_allowed": self.game_recommendation_allowed,
            "mid_input_fallback_allowed": self.mid_input_fallback_allowed,
        }


EXACT_EXAM_POLICY = ExamExecutionPolicy(
    mode=ExamExecutionMode.EXACT,
    policy_id="exact-simulator-v1",
    simulator_required=True,
    game_recommendation_allowed=False,
)

MAA_COMPLETION_EXAM_POLICY = ExamExecutionPolicy(
    mode=ExamExecutionMode.MAA_COMPLETION_BASELINE,
    policy_id="maa-game-recommendation-completion-v1",
    simulator_required=False,
    game_recommendation_allowed=True,
)

_POLICIES: Mapping[ExamExecutionMode, ExamExecutionPolicy] = {
    EXACT_EXAM_POLICY.mode: EXACT_EXAM_POLICY,
    MAA_COMPLETION_EXAM_POLICY.mode: MAA_COMPLETION_EXAM_POLICY,
}


def resolve_exam_execution_policy(
    value: ExamExecutionPolicy | ExamExecutionMode | str,
) -> ExamExecutionPolicy:
    """Resolve a CLI/config value to one of the fixed production policies."""

    if isinstance(value, ExamExecutionPolicy):
        canonical = _POLICIES.get(value.mode)
        if canonical != value:
            raise ValueError("custom exam execution policies are not supported")
        return canonical
    try:
        mode = value if isinstance(value, ExamExecutionMode) else ExamExecutionMode(value)
    except (TypeError, ValueError) as error:
        supported = ", ".join(item.value for item in ExamExecutionMode)
        raise ValueError(f"unsupported exam execution mode; expected {supported}") from error
    return _POLICIES[mode]


__all__ = [
    "EXACT_EXAM_POLICY",
    "MAA_COMPLETION_EXAM_POLICY",
    "ExamExecutionMode",
    "ExamExecutionPolicy",
    "resolve_exam_execution_policy",
]
