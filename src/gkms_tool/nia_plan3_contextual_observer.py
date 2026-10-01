"""Contextual sidecar writer for executed Plan3 orchestration steps."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
import tempfile

from .nia_plan3_native_sidecar import build_plan3_native_step_record
from .nia_native_action_contract import NiaNativeStepRecord
from .plan3_exam_orchestrator import Plan3ExamOrchestrationStep
from .plan3_native_search import (
    Plan3NativeLegalCandidate,
    Plan3NativeLegalCandidateEnumeration,
)


DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "nia_training"
    / "plan3_legal_decisions.jsonl"
)


@dataclass(frozen=True, slots=True)
class Plan3NativeLearningBoundary:
    source_id: str
    state_before: Mapping[str, object]
    enumeration: Plan3NativeLegalCandidateEnumeration
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id:
            raise ValueError("source_id must be non-empty text")
        if not isinstance(self.state_before, Mapping):
            raise TypeError("state_before must be a mapping")
        if not isinstance(
            self.enumeration,
            Plan3NativeLegalCandidateEnumeration,
        ):
            raise TypeError("enumeration must be typed")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")


Plan3BoundaryProvider = Callable[[Path], Plan3NativeLearningBoundary | None]
Plan3ObservedStateReader = Callable[[Path], Mapping[str, object] | None]


def _chosen_candidate(
    step: Plan3ExamOrchestrationStep,
    enumeration: Plan3NativeLegalCandidateEnumeration,
) -> Plan3NativeLegalCandidate | None:
    execution = step.execution
    plan = execution.get("plan") if isinstance(execution, Mapping) else None
    action = plan.get("action") if isinstance(plan, Mapping) else None
    kind = plan.get("kind") if isinstance(plan, Mapping) else None
    if not isinstance(action, Mapping):
        return None
    for candidate in enumeration.candidates:
        if kind == "card" and candidate.kind == "play":
            if candidate.card_guid == action.get("card_guid"):
                return candidate
        elif kind == "drink" and candidate.kind == "drink":
            if (
                candidate.drink_slot_index
                == action.get("drink_slot_index")
                and candidate.drink_id == action.get("drink_id")
                and (candidate.selected_card_guid or "")
                == (action.get("selected_card_guid") or "")
            ):
                return candidate
        elif kind == "skip" and candidate.kind == "turn-end":
            return candidate
    return None


@dataclass(slots=True)
class Plan3ContextualSidecarWriter:
    boundary_provider: Plan3BoundaryProvider
    output: Path = field(default_factory=lambda: DEFAULT_OUTPUT)
    observed_state_reader: Plan3ObservedStateReader | None = None
    native_boundary_provider: Callable[[object], Plan3NativeLearningBoundary] | None = None
    _rows: list[NiaNativeStepRecord] = field(default_factory=list, init=False)
    _seen: set[tuple[str, int]] = field(default_factory=set, init=False)
    _rejections: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if not callable(self.boundary_provider):
            raise TypeError("boundary_provider must be callable")
        if self.observed_state_reader is not None and not callable(
            self.observed_state_reader
        ):
            raise TypeError("observed_state_reader must be callable or None")
        self.output = Path(self.output)
        if not self.output.exists():
            return
        for line_number, line in enumerate(
            self.output.read_text(encoding="utf-8").splitlines(),
            1,
        ):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                raise ValueError(
                    f"invalid Plan3 sidecar line {line_number}"
                )
            key = (str(raw.get("source_id", "")), int(raw.get("step", 0)))
            if not key[0] or key[1] < 1 or key in self._seen:
                raise ValueError("invalid or duplicate Plan3 sidecar boundary")
            self._seen.add(key)

    @property
    def rows(self) -> tuple[NiaNativeStepRecord, ...]:
        return tuple(self._rows)

    @property
    def rejections(self) -> tuple[str, ...]:
        return tuple(self._rejections)

    def _append(self, record: NiaNativeStepRecord) -> None:
        key = (record.source_id, record.step)
        if key in self._seen:
            return
        self.output.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            self.output.read_text(encoding="utf-8")
            if self.output.exists()
            else ""
        )
        payload = existing + json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.output.name}.",
                suffix=".tmp",
                dir=str(self.output.parent),
            )
            with open(
                descriptor,
                "w",
                encoding="utf-8",
                newline="\n",
                closefd=True,
            ) as stream:
                stream.write(payload)
            Path(temporary).replace(self.output)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        self._seen.add(key)
        self._rows.append(record)

    def __call__(
        self,
        step: Plan3ExamOrchestrationStep,
        pre_action_snapshot: Path | None,
        post_action_snapshot: Path | None,
    ) -> None:
        if not isinstance(step, Plan3ExamOrchestrationStep):
            raise TypeError("step must be Plan3ExamOrchestrationStep")
        if not step.semantic_action_executed or pre_action_snapshot is None:
            return
        try:
            boundary = self.boundary_provider(pre_action_snapshot)
            observed = (self.observed_state_reader(post_action_snapshot)
                        if post_action_snapshot is not None and self.observed_state_reader is not None else None)
            self._record(step, boundary, observed, {
                "kind": "plan3-orchestrator-executed",
                "input_count": step.execution.get("input_count"),
                "pre_action_snapshot": str(pre_action_snapshot.resolve()),
            })
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._rejections.append(f"{type(error).__name__}:{error}")

    def observe_native(self, step: Plan3ExamOrchestrationStep, before, after) -> None:
        """Record already-observed DLL payloads without a fake encrypted file."""
        if not step.semantic_action_executed:
            return
        try:
            if self.native_boundary_provider is None:
                raise ValueError("native-candidate-boundary-provider-unavailable")
            if step.execution.get("executor_backend") != "dll" or step.execution.get("native_action_status") != "settled":
                raise ValueError("native-submission-settlement-proof-missing")
            actual = step.execution.get("prediction_actual")
            if not isinstance(actual, Mapping) or not isinstance(actual.get("runtime_provenance"), Mapping) or not actual.get("request_id"):
                raise ValueError("native-request-provenance-missing")
            boundary = self.native_boundary_provider(before)
            state = after.exam_state
            terminal = state.root_runtime is not None and state.root_runtime.is_exam_end_complete
            if not (state.is_native_actionable_settled or terminal):
                raise ValueError("native-after-state-not-settled")
            self._record(step, boundary, state.to_dict(), {
                "kind": "runtime-command-native-settlement",
                "input_count": step.execution.get("input_count"),
                "request_id": actual["request_id"],
                "runtime_provenance": dict(actual["runtime_provenance"]),
            })
        except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._rejections.append(f"{type(error).__name__}:{error}")

    def _record(self, step, boundary, observed, submission_proof) -> None:
        if boundary is None:
            raise ValueError("candidate-boundary-unavailable")
        if not boundary.enumeration.complete:
            raise ValueError("candidate-enumeration-incomplete")
        chosen = _chosen_candidate(step, boundary.enumeration)
        if chosen is None:
            raise ValueError("executed-action-not-in-candidates")
        input_count = step.execution.get("input_count")
        if (
            isinstance(input_count, bool)
            or not isinstance(input_count, int)
            or input_count < 1
        ):
            raise ValueError("executed-input-proof-missing")
        state_after = None
        reward = None
        terminal = False
        if observed is not None:
            if (
                isinstance(observed, Mapping)
                and dict(observed) != dict(boundary.state_before)
            ):
                before_score = boundary.state_before.get("score")
                after_score = observed.get("score")
                if (
                    isinstance(before_score, int)
                    and not isinstance(before_score, bool)
                    and isinstance(after_score, int)
                    and not isinstance(after_score, bool)
                ):
                    state_after = observed
                    reward = after_score - before_score
                    terminal = step.terminal
        record = build_plan3_native_step_record(
            source_id=boundary.source_id,
            step=step.index,
            state_before=boundary.state_before,
            enumeration=boundary.enumeration,
            chosen=chosen,
            submission_proof=submission_proof,
            state_after=state_after,
            reward=reward,
            terminal=terminal,
            metadata=boundary.metadata,
        )
        self._append(record)

def build_nia_plan3_contextual_sidecar(
    outer_reader: Callable[[], object] | None,
    *,
    source_id: str,
    expected_produce_id: str | None = None,
    expected_idol_card_id: str | None = None,
    output: str | Path = DEFAULT_OUTPUT,
    native_candidate_provider=None,
) -> Plan3ContextualSidecarWriter:
    """Compose the NIA-bound provider with the orchestration observer."""

    from .nia_plan3_native_candidates import (
        build_nia_plan3_native_candidate_provider,
    )
    from .plan3_local_save_bridge import decode_plan3_local_save_file

    provider = None if outer_reader is None else build_nia_plan3_native_candidate_provider(
        outer_reader,
        expected_produce_id=expected_produce_id,
        expected_idol_card_id=expected_idol_card_id,
    )
    native_provider = native_candidate_provider or build_nia_plan3_native_candidate_provider(
        outer_reader, expected_produce_id=expected_produce_id,
        expected_idol_card_id=expected_idol_card_id, prefer_exam_save_runtime=True)

    def native_boundary(decoded) -> Plan3NativeLearningBoundary:
        result = native_provider(decoded)
        return Plan3NativeLearningBoundary(
            source_id=source_id, state_before=decoded.exam_state.to_dict(), enumeration=result.enumeration,
            metadata={"scope": None if result.scope is None else result.scope.to_dict(),
                      "state_source": "runtime-command-native-json"})

    def boundary(path: Path) -> Plan3NativeLearningBoundary:
        if provider is None:
            raise ValueError("native sidecar accepts observed payloads, not encrypted-file boundaries")
        decoded = decode_plan3_local_save_file(path)
        result = provider(decoded)
        return Plan3NativeLearningBoundary(
            source_id=source_id,
            state_before=decoded.exam_state.to_dict(),
            enumeration=result.enumeration,
            metadata={
                "scope": (
                    None
                    if result.scope is None
                    else result.scope.to_dict()
                ),
                "snapshot_path": str(path.resolve()),
            },
        )

    def observed_state(path: Path) -> Mapping[str, object] | None:
        decoded = decode_plan3_local_save_file(path)
        state = decoded.exam_state
        runtime = state.root_runtime
        if not (
            state.is_native_actionable_settled
            or (runtime is not None and runtime.is_exam_end_complete)
        ):
            return None
        return state.to_dict()

    return Plan3ContextualSidecarWriter(
        boundary,
        Path(output),
        observed_state,
        native_boundary,
    )


__all__ = [
    "DEFAULT_OUTPUT",
    "Plan3BoundaryProvider",
    "Plan3ObservedStateReader",
    "Plan3ContextualSidecarWriter",
    "Plan3NativeLearningBoundary",
    "build_nia_plan3_contextual_sidecar",
]
