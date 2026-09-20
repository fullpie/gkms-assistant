from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class RunState:
    """Minimal First Star REGULAR run state.

    The first vertical slice tracks the 13-step schedule and the values that are
    visible without reading game memory.  Support cards and event flags will be
    added after the replay model is validated against a real run.
    """

    step: int = 1
    total_steps: int = 13
    stamina: int = 31
    max_stamina: int = 31
    vocal: int = 65
    dance: int = 55
    visual: int = 45
    vocal_growth: int = 200
    dance_growth: int = 40
    visual_growth: int = 180

    def validate(self) -> None:
        if not 1 <= self.step <= self.total_steps:
            raise ValueError(f"培育進度必須介於 1 與 {self.total_steps}。")
        if self.max_stamina <= 0:
            raise ValueError("最大體力必須大於 0。")
        if not 0 <= self.stamina <= self.max_stamina:
            raise ValueError("目前體力必須介於 0 與最大體力之間。")
        if min(self.vocal, self.dance, self.visual) < 0:
            raise ValueError("三維屬性不可為負數。")
        if min(self.vocal_growth, self.dance_growth, self.visual_growth) < 0:
            raise ValueError("三維成長率不可為負數。")

    def with_step(self, step: int) -> "RunState":
        return replace(self, step=max(1, min(self.total_steps, step)))


@dataclass(frozen=True, slots=True)
class CardDefinition:
    id: str
    name: str
    stamina_cost: int
    parameter: int = 0
    block: int = 0
    favorable_turns: int = 0
    conditional_parameter: int = 0
    requires_favorable: bool = False
    once_per_exam: bool = False
    source_note: str = ""


@dataclass(frozen=True, slots=True)
class ExamState:
    turns_remaining: int = 5
    stamina: int = 20
    block: int = 0
    favorable_turns: int = 0
    score: int = 0

    def validate(self) -> None:
        if self.turns_remaining < 1:
            raise ValueError("剩餘回合必須至少為 1。")
        if min(self.stamina, self.block, self.favorable_turns, self.score) < 0:
            raise ValueError("考試數值不可為負數。")


@dataclass(frozen=True, slots=True)
class ExamTransition:
    before: ExamState
    after: ExamState
    card: CardDefinition
    score_gain: int
    stamina_paid: int
    block_paid: int
    legal: bool
    unsupported_effects: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Recommendation:
    card: CardDefinition
    transition: ExamTransition
    utility: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunRecommendation:
    action: str
    utility: float
    reasons: tuple[str, ...]
