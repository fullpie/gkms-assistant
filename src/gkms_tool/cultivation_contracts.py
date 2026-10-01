"""Shared cultivation records and policy context, with no controller imports.

Historical public class names and the serialized schema are retained so saved
records and explicit legacy callers use the same types as the native runner.
"""
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

SCHEMA_NAME = "gkms.initial-regular-autopilot.v1"
STATUS_COMPLETED = "completed"
STATUS_HARD_STOP = "hard-stop"
STATUS_STOPPED = "stopped"
STATUS_FAILED_SETTLED = "failed-settled"


@dataclass(frozen=True, slots=True)
class InitialRegularAutopilotStep:
    index: int
    page: str
    action: str
    target: str | None
    outcome: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class InitialRegularAutopilotResult:
    status: str
    stop_reason: str
    plan_type: str
    cycles: int
    steps: tuple[InitialRegularAutopilotStep, ...]
    schema: str = SCHEMA_NAME
    last_surface: Mapping[str, Any] | None = None
    failure_settlement: Mapping[str, Any] | None = None
    # Transport-only ownership: the native runner already wrote this report.
    # It is deliberately excluded from the formal result's serialized payload.
    native_completed_run_evidence: Mapping[str, Any] | None = None
    # Local cleanup must not change the hash-bound formal gameplay result.
    # This transport-only warning stops the GUI batch while the active pointer
    # continues to guard new input. Its durable journal is separate evidence.
    terminal_bookkeeping: Mapping[str, Any] | None = None

    @property
    def completed(self) -> bool:
        return self.status == STATUS_COMPLETED

    def to_dict(self) -> dict[str, Any]:
        payload = {"schema": self.schema, "status": self.status, "stop_reason": self.stop_reason,
                "plan_type": self.plan_type, "cycles": self.cycles,
                "steps": [value.to_dict() for value in self.steps],
                "last_surface": None if self.last_surface is None else dict(self.last_surface)}
        if self.failure_settlement is not None:
            payload.update(failure_settlement=dict(self.failure_settlement), game_outcome="failed",
                           successful_cultivation=False, training_eligible=False, dataset_ready=False)
        return payload


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2ExamContext:
    """Stage identity and model settings, independent of input backend."""

    idol_card_id: str
    produce_id: str = "produce-001"
    stage_number: int = 1
    draw_count: int | None = None
    hand_limit: int | None = None
    max_actions: int = 100
    learned_policy_bundle_path: Path | None = None

    def __post_init__(self) -> None:
        if not self.idol_card_id or not self.produce_id:
            raise ValueError("Plan2 exam context requires idol_card_id and produce_id")
        if self.stage_number < 1 or self.max_actions < 1:
            raise ValueError("Plan2 stage_number and max_actions must be positive")
        if self.learned_policy_bundle_path is not None:
            object.__setattr__(self, "learned_policy_bundle_path", Path(self.learned_policy_bundle_path))
