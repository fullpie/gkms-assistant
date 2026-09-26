"""Total-terminal-score objectives; never add score_delta a second time."""
from dataclasses import dataclass
from .contracts import ContractError, Outcome, finite


@dataclass(frozen=True, slots=True)
class ScoreObjective:
    objective_id: str = "exam_score_v1"
    scale: float = 10_000.0

    def __post_init__(self):
        if self.objective_id not in ("exam_score_v1", "produce_final_exam_v1"):
            raise ContractError("unknown objective")
        if finite(self.scale, "scale", 0) == 0:
            raise ContractError("scale must be positive")

    def encode(self, score: float) -> float:
        return finite(score, "score", 0) / self.scale

    def decode(self, value: float) -> float:
        return finite(value, "value") * self.scale

    def terminal_value(self, outcome: Outcome) -> float | None:
        if outcome.truncated:
            return None
        if not outcome.goal_done:
            return None
        if outcome.actual_game_failure:
            if self.objective_id != "produce_final_exam_v1":
                raise ContractError("exam objective requires actual score, not invented failure reward")
            return 0.0
        return self.encode(outcome.observed_terminal_score)
