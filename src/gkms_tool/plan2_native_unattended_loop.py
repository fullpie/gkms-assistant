"""Dependency-injected unattended Plan2 loop over settled ExamSaveData evidence.

The loop owns no controller, screen, polling, clock, MAA, or agent dependency.
Its injected reader is responsible for returning the current/next settled
evidence, while its injected executor accepts one semantic Plan2 action.  The
loop only coordinates planning, dispatch, comparison, hard stops, and records.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
import json
from pathlib import Path
from typing import Final

from .audition_local_save_state import AuditionLocalSaveStateEvidence
from .master_db import DEFAULT_DATABASE
from .plan2_native_exam_save_orchestrator import (
    PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
    Plan2NativeExamSaveDecision,
    Plan2NativeExamSaveDependencies,
    _coverage_report,
    decide_plan2_native_exam_save,
    decide_plan2_native_horizon,
)
from .plan2_native_expectimax import (
    Plan2NativeEvaluationWeights,
    Plan2NativeExpectimaxLimits,
    enumerate_plan2_native_actions,
)
from .plan2_native_horizon import (
    Plan2NativeAction,
    Plan2NativeBlocker,
    Plan2NativeDrinkAction,
    Plan2NativeHorizonState,
    Plan2NativeOfflineAction,
)
from .plan2_native_local_save_bootstrap import Plan2NativeLocalSaveBootstrapAudit
from .plan2_native_program_catalog import Plan2NativeProgramCatalogCompilation
from .plan2_symbolic_generated_inputs import prepare_plan2_symbolic_generated_inputs


PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION: Final = 1

RUN_COMPLETED: Final = "completed"
RUN_STOPPED: Final = "stopped"

STOP_TERMINAL: Final = "terminal"
STOP_MAX_ACTIONS: Final = "max-actions-reached"
STOP_EVIDENCE_READ_ERROR: Final = "evidence-read-error"
STOP_DECISION_UNAVAILABLE: Final = "decision-unavailable"
STOP_ACTION_REJECTED: Final = "action-rejected"
STOP_ACTION_EXECUTOR_ERROR: Final = "action-executor-error"
STOP_SAME_STATE_NO_PROGRESS: Final = "same-state-no-progress"
STOP_RECORD_SINK_ERROR: Final = "record-sink-error"

SettledEvidenceReader = Callable[
    [AuditionLocalSaveStateEvidence | None],
    AuditionLocalSaveStateEvidence,
]
DecisionOrchestrator = Callable[
    [AuditionLocalSaveStateEvidence], Plan2NativeExamSaveDecision
]
ActionExecutor = Callable[[Plan2NativeOfflineAction], "Plan2NativeActionExecution"]
RecordSink = Callable[["Plan2NativeUnattendedStepRecord"], None]
LogicalStateReader = Callable[
    [AuditionLocalSaveStateEvidence], Plan2NativeHorizonState | None
]
LogicalDecisionOrchestrator = Callable[
    [Plan2NativeHorizonState], Plan2NativeExamSaveDecision
]
Plan2NativePlannerCandidateProvider = Callable[
    [Plan2NativeExamSaveDecision, AuditionLocalSaveStateEvidence | None],
    tuple[Plan2NativeOfflineAction, ...] | None,
]


class ExamBoundaryDisposition(StrEnum):
    """Typed physical boundary observed after an Exam action or read."""

    ACTIONABLE = "actionable"
    TERMINAL = "terminal"
    SUBMITTED_PENDING = "submitted-pending"
    TRANSITIONAL = "transitional"
    RETRYABLE_MOVING = "retryable-moving"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class Plan2NativeLearnedBoundary:
    """Planner-free exact root and legal actions for one learned decision."""

    base_decision: Plan2NativeExamSaveDecision
    root: Plan2NativeHorizonState | None
    catalog: object | None
    actions: tuple[Plan2NativeOfflineAction, ...] = ()
    blockers: tuple[object, ...] = ()
    diagnostics: tuple[object, ...] = ()

    @property
    def ready(self) -> bool:
        return (
            self.root is not None
            and self.catalog is not None
            and bool(self.actions)
            and not self.blockers
            and not self.root.terminal
        )


@dataclass(frozen=True, slots=True)
class Plan2NativeLoopIssue:
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if type(self.code) is not str or not self.code:
            raise ValueError("issue code must be non-empty text")
        if type(self.detail) is not str:
            raise TypeError("issue detail must be text")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Plan2NativeActionExecution:
    """Typed acknowledgement returned by the injected action executor."""

    accepted: bool
    detail: str = ""
    replan: bool = False
    metadata: Mapping[str, object] | None = None
    boundary_disposition: ExamBoundaryDisposition | None = None
    issues: tuple[Plan2NativeLoopIssue, ...] = ()

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be bool")
        if type(self.detail) is not str:
            raise TypeError("detail must be text")
        if type(self.replan) is not bool:
            raise TypeError("replan must be bool")
        if self.metadata is not None and not isinstance(self.metadata, Mapping):
            raise TypeError("execution metadata must be a mapping or None")
        if self.accepted and self.replan:
            raise ValueError("accepted execution cannot also request a replan")
        disposition = self.boundary_disposition
        if disposition is None:
            disposition = (
                ExamBoundaryDisposition.ACTIONABLE
                if self.accepted
                else ExamBoundaryDisposition.RETRYABLE_MOVING
                if self.replan
                else ExamBoundaryDisposition.TRANSITIONAL
            )
        if not isinstance(disposition, ExamBoundaryDisposition):
            raise TypeError("boundary_disposition must be typed or None")
        issues = tuple(self.issues)
        if any(not isinstance(value, Plan2NativeLoopIssue) for value in issues):
            raise TypeError("execution issues must be typed")
        if self.accepted and issues:
            raise ValueError("accepted execution cannot carry issues")
        if disposition is ExamBoundaryDisposition.TERMINAL and not self.accepted:
            raise ValueError("terminal disposition requires accepted execution")
        if disposition is ExamBoundaryDisposition.SUBMITTED_PENDING and self.replan:
            raise ValueError("submitted pending execution cannot request a replan")
        object.__setattr__(self, "boundary_disposition", disposition)
        object.__setattr__(self, "issues", issues)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "accepted": self.accepted,
            "detail": self.detail,
            "replan": self.replan,
            "boundary_disposition": self.boundary_disposition.value,
            "issues": [value.to_dict() for value in self.issues],
        }
        if self.metadata is not None:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True, slots=True)
class Plan2NativeStateDifference:
    path: str
    predicted: object
    actual: object

    def __post_init__(self) -> None:
        if type(self.path) is not str or not self.path:
            raise ValueError("difference path must be non-empty text")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "predicted": self.predicted,
            "actual": self.actual,
        }


@dataclass(frozen=True, slots=True)
class Plan2NativePlayCountAuthorityDiagnostic:
    """Non-fatal solver-vs-LocalSave card counter observation."""

    guid: str
    solver_semantic_count: int
    local_save_runtime_count: int
    code: str = "card-play-count-authority-divergence"

    def __post_init__(self) -> None:
        if type(self.guid) is not str or not self.guid:
            raise ValueError("diagnostic GUID must be non-empty text")
        for name in ("solver_semantic_count", "local_save_runtime_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.code != "card-play-count-authority-divergence":
            raise ValueError("unsupported play-count diagnostic code")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "guid": self.guid,
            "solver_semantic_count": self.solver_semantic_count,
            "local_save_runtime_count": self.local_save_runtime_count,
            "fatal": False,
            "resolution": "preserve-both-authorities",
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeStateComparison:
    differences: tuple[Plan2NativeStateDifference, ...]
    provenance_differences: tuple[Plan2NativeStateDifference, ...] = ()
    play_count_diagnostics: tuple[Plan2NativePlayCountAuthorityDiagnostic, ...] = ()

    @property
    def matches(self) -> bool:
        return not self.differences

    def to_dict(self) -> dict[str, object]:
        return {
            "matches": self.matches,
            "differences": [value.to_dict() for value in self.differences],
            "provenance_differences": [
                value.to_dict() for value in self.provenance_differences
            ],
            "play_count_diagnostics": [
                value.to_dict() for value in self.play_count_diagnostics
            ],
        }


def _action_to_dict(
    action: Plan2NativeOfflineAction | None,
) -> dict[str, object] | None:
    if action is None:
        return None
    if isinstance(action, Plan2NativeDrinkAction):
        return {
            "kind": action.kind,
            "slot_index": action.slot_index,
            "instance_id": action.instance_id,
            "drink_id": action.drink_id,
            "selected_card_guid": action.selected_card_guid,
            "action_id": action.action_id,
        }
    return {
        "kind": action.kind,
        "card_guid": action.card_guid,
        "action_id": action.action_id,
    }


def _queued_action_to_dict(value: object) -> dict[str, object]:
    return {
        "kind": getattr(value, "kind", ""),
        "value": getattr(value, "value", 0),
        "card_guid": getattr(value, "card_guid", ""),
        "pay_cost": getattr(value, "pay_cost", False),
        "spend_play": getattr(value, "spend_play", False),
    }


_PROVENANCE_BINDING_FIELDS: Final = (
    "zone_checkpoint_digest",
    "local_save_source_sha256",
    "local_save_evidence_digest",
    "runtime_evidence_digest",
)


def _semantic_card_to_dict(
    value: object,
    *,
    include_play_count: bool,
) -> dict[str, object]:
    payload = value.to_dict()  # type: ignore[attr-defined]
    runtime = dict(payload["runtime_state"])
    if not include_play_count:
        runtime.pop("play_count")
    payload["runtime_state"] = runtime
    payload.pop("runtime_state_digest")
    return payload


def _semantic_zones_to_dict(
    state: Plan2NativeHorizonState,
    *,
    include_play_count: bool,
) -> dict[str, object]:
    zones = state.zones
    binding = zones.binding.to_dict()
    for name in _PROVENANCE_BINDING_FIELDS:
        binding.pop(name)
    return {
        "schema_version": zones.schema_version,
        "binding": binding,
        "random_state": zones.random_state,
        "card_universe": [
            _semantic_card_to_dict(
                card,
                include_play_count=include_play_count,
            )
            for card in zones.card_universe
        ],
        **{
            name: [
                _semantic_card_to_dict(
                    card,
                    include_play_count=include_play_count,
                )
                for card in getattr(zones, name)
            ]
            for name in ("hand", "deck", "grave", "lost")
        },
        "pending_played": (
            None
            if zones.pending_played is None
            else _semantic_card_to_dict(
                zones.pending_played,
                include_play_count=include_play_count,
            )
        ),
    }


def _state_to_dict(
    state: Plan2NativeHorizonState | None,
    *,
    include_play_count: bool = True,
) -> dict[str, object] | None:
    if state is None:
        return None
    scalar = state.scalar
    active_status_uids = sorted(
        {
            *(value.status_uid for value in scalar.review_multiple_layers),
            *(value.status_uid for value in scalar.end_turn_listeners),
            *(value.status_uid for value in scalar.card_play_listeners),
            *(
                (scalar.stamina_consumption_add_status.status_uid,)
                if scalar.stamina_consumption_add_status is not None
                else ()
            ),
            *(value.status_uid for value in state.stamina_modifiers.down_fix_layers),
            *(
                (state.stamina_modifiers.down.status_uid,)
                if state.stamina_modifiers.down is not None
                else ()
            ),
            *(
                (state.stamina_modifiers.add.layer.status_uid,)
                if state.stamina_modifiers.add.layer is not None
                else ()
            ),
            *(value.status_uid for value in state.status_enchant.listeners),
            *(value.status_uid for value in state.review_dynamic.layers),
            *(value.uid for value in state.debuff_registry.statuses),
            *(value.status_uid for value in state.effect_chains.queue),
            *(value.native_uid for value in state.generated_runtime.statuses),
            *(
                (value.status_uid for value in state.encore_runtime.listeners)
                if state.encore_runtime is not None
                else ()
            ),
            *state.item_runtime.active_status_uids,
        }
    )
    return {
        "schema_version": state.schema_version,
        "source_kind": state.source_kind,
        "phase": state.phase,
        "terminal": state.terminal,
        "remaining_turns": state.remaining_turns,
        "limit_turn": state.limit_turn,
        "extra_turn": state.extra_turn,
        "draw_count": state.draw_count,
        "hand_limit": state.hand_limit,
        "plays_remaining": state.plays_remaining,
        "scalar": {
            "current_turn": scalar.current_turn,
            "review": scalar.review,
            "score": scalar.score,
            "review_count_add": scalar.review_count_add,
            "block": scalar.block,
            "card_play_aggressive": scalar.card_play_aggressive,
            "stamina": scalar.stamina,
            "max_stamina": scalar.max_stamina,
            "exam_card_play_count": scalar.exam_card_play_count,
            "turn_card_play_count": scalar.turn_card_play_count,
            "next_status_uid": scalar.next_status_uid,
        },
        "zones": _semantic_zones_to_dict(
            state,
            include_play_count=include_play_count,
        ),
        "runtime": {
            "active_status_uids": active_status_uids,
            "review_status_present": state.review_dynamic.review_status_present,
            "review_passing_turn_start": (
                state.review_dynamic.review_passing_turn_start
            ),
            "review_dynamic_layer_count": len(state.review_dynamic.layers),
            "stamina_modifier_count": (
                len(state.stamina_modifiers.down_fix_layers)
                + int(state.stamina_modifiers.down is not None)
                + int(state.stamina_modifiers.add.layer is not None)
            ),
            "status_enchant_listener_count": len(state.status_enchant.listeners),
            "debuff_status_count": len(state.debuff_registry.statuses),
            "effect_chain_count": len(state.effect_chains.queue),
            "generated_status_count": len(state.generated_runtime.statuses),
            "encore_listener_count": (
                0
                if state.encore_runtime is None
                else len(state.encore_runtime.listeners)
            ),
            "item_listener_count": len(state.item_runtime.listeners),
        },
        "items": {
            "source_ids": [value.item_id for value in state.item_runtime.sources],
            "usage_counts": [list(value) for value in state.item_runtime.usage_counts],
            "active_status_uids": list(state.item_runtime.active_status_uids),
        },
        # LocalSave does not expose stable per-bottle instance GUIDs.  Ordered
        # drink IDs are the gameplay semantics across a fresh save bootstrap;
        # synthetic instance IDs remain action-binding provenance only.
        "drinks": {
            "drink_ids": [
                value.drink_id for value in state.drink_runtime.inventory
            ],
            "unmapped_slots": list(state.unmapped_drink_slots),
        },
        "total_effect_draw_card_count": state.total_effect_draw_card_count,
        "review_consumption_sum": state.review_consumption_sum,
        "block_consumption_sum_count": state.block_consumption_sum_count,
        "judge_parameter": state.judge_parameter,
        "clear_border": state.clear_border,
        "command_queue": [
            _queued_action_to_dict(value) for value in state.command_queue
        ],
        "opaque_status_queue": list(state.opaque_status_queue),
    }


_MISSING = {"missing": True}


def _json_differences(
    predicted: object,
    actual: object,
    *,
    path: str = "$",
) -> list[Plan2NativeStateDifference]:
    if predicted == actual:
        return []
    if isinstance(predicted, Mapping) and isinstance(actual, Mapping):
        result: list[Plan2NativeStateDifference] = []
        for key in sorted(set(predicted) | set(actual)):
            child = f"{path}.{key}"
            if key not in predicted:
                result.append(Plan2NativeStateDifference(child, _MISSING, actual[key]))
            elif key not in actual:
                result.append(Plan2NativeStateDifference(child, predicted[key], _MISSING))
            else:
                result.extend(
                    _json_differences(predicted[key], actual[key], path=child)
                )
        return result
    if isinstance(predicted, list) and isinstance(actual, list):
        result = []
        for index in range(max(len(predicted), len(actual))):
            child = f"{path}[{index}]"
            if index >= len(predicted):
                result.append(
                    Plan2NativeStateDifference(child, _MISSING, actual[index])
                )
            elif index >= len(actual):
                result.append(
                    Plan2NativeStateDifference(child, predicted[index], _MISSING)
                )
            else:
                result.extend(
                    _json_differences(predicted[index], actual[index], path=child)
                )
        return result
    return [Plan2NativeStateDifference(path, predicted, actual)]


def compare_plan2_native_states(
    predicted: Plan2NativeHorizonState | None,
    actual: Plan2NativeHorizonState | None,
) -> tuple[Plan2NativeStateDifference, ...]:
    """Return deterministic JSON-path differences between two typed states."""

    if predicted is actual:
        return ()
    differences = _json_differences(
        _state_to_dict(predicted),
        _state_to_dict(actual),
    )
    return tuple(differences)


def _card_play_counts_by_guid(
    state: Plan2NativeHorizonState | None,
) -> dict[str, int]:
    if state is None:
        return {}
    return {
        card.guid: card.runtime_state.play_count
        for card in state.zones.card_universe
    }


def compare_plan2_native_prediction_with_local_save(
    predicted: Plan2NativeHorizonState | None,
    actual: Plan2NativeHorizonState | None,
) -> Plan2NativeStateComparison:
    """Compare semantic state while retaining replaceable evidence authority.

    Simulated descendants intentionally keep the pre-action immutable evidence
    binding.  A fresh LocalSave bootstrap supplies a new post-action binding,
    so its four evidence digests are audit provenance rather than gameplay
    semantics.  Card ``_playCount`` is likewise retained as a separate
    LocalSave object-lifecycle authority; the solver semantic counter is never
    rewritten from it.
    """

    differences = tuple(
        _json_differences(
            _state_to_dict(predicted, include_play_count=False),
            _state_to_dict(actual, include_play_count=False),
        )
    )
    provenance: list[Plan2NativeStateDifference] = []
    if predicted is not None and actual is not None:
        predicted_binding = predicted.zones.binding.to_dict()
        actual_binding = actual.zones.binding.to_dict()
        for name in _PROVENANCE_BINDING_FIELDS:
            expected = predicted_binding[name]
            observed = actual_binding[name]
            if expected != observed:
                provenance.append(
                    Plan2NativeStateDifference(
                        f"$.zones.binding.{name}", expected, observed
                    )
                )
    predicted_counts = _card_play_counts_by_guid(predicted)
    actual_counts = _card_play_counts_by_guid(actual)
    diagnostics = tuple(
        Plan2NativePlayCountAuthorityDiagnostic(
            guid,
            predicted_counts[guid],
            actual_counts[guid],
        )
        for guid in sorted(predicted_counts.keys() & actual_counts.keys())
        if predicted_counts[guid] != actual_counts[guid]
    )
    return Plan2NativeStateComparison(
        differences,
        tuple(provenance),
        diagnostics,
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeUnattendedStepRecord:
    schema_version: int
    step_index: int
    evidence_before: AuditionLocalSaveStateEvidence | None
    decision: Plan2NativeExamSaveDecision | None
    action: Plan2NativeOfflineAction | None
    execution: Plan2NativeActionExecution | None
    predicted_before: Plan2NativeHorizonState | None
    predicted_after: Plan2NativeHorizonState | None
    actual_next_evidence: AuditionLocalSaveStateEvidence | None
    actual_next: Plan2NativeHorizonState | None
    differences: tuple[Plan2NativeStateDifference, ...]
    stop_reason_after_step: str | None
    issues: tuple[Plan2NativeLoopIssue, ...] = ()
    provenance_differences: tuple[Plan2NativeStateDifference, ...] = ()
    play_count_diagnostics: tuple[
        Plan2NativePlayCountAuthorityDiagnostic, ...
    ] = ()

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION:
            raise ValueError("unsupported Plan2 unattended record schema")
        if type(self.step_index) is not int or self.step_index < 1:
            raise ValueError("step_index must be a positive integer")
        if self.action is not None and not isinstance(
            self.action, (Plan2NativeAction, Plan2NativeDrinkAction)
        ):
            raise TypeError("action must be a typed Plan2 offline action or None")
        if self.execution is not None and not isinstance(
            self.execution, Plan2NativeActionExecution
        ):
            raise TypeError("execution must be typed or None")
        if type(self.provenance_differences) is not tuple or any(
            not isinstance(value, Plan2NativeStateDifference)
            for value in self.provenance_differences
        ):
            raise TypeError("provenance_differences must be typed")
        if type(self.play_count_diagnostics) is not tuple or any(
            not isinstance(value, Plan2NativePlayCountAuthorityDiagnostic)
            for value in self.play_count_diagnostics
        ):
            raise TypeError("play_count_diagnostics must be typed")

    @property
    def prediction_matches_actual(self) -> bool | None:
        if self.predicted_after is None or self.actual_next is None:
            return None
        return not self.differences

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "step_index": self.step_index,
            "evidence_before": (
                None if self.evidence_before is None else self.evidence_before.to_dict()
            ),
            "decision": None if self.decision is None else self.decision.to_dict(),
            "action": _action_to_dict(self.action),
            "execution": None if self.execution is None else self.execution.to_dict(),
            "predicted_before": _state_to_dict(self.predicted_before),
            "predicted_after": _state_to_dict(self.predicted_after),
            "actual_next_evidence": (
                None
                if self.actual_next_evidence is None
                else self.actual_next_evidence.to_dict()
            ),
            "actual_next": _state_to_dict(self.actual_next),
            "prediction_matches_actual": self.prediction_matches_actual,
            "differences": [value.to_dict() for value in self.differences],
            "provenance_differences": [
                value.to_dict() for value in self.provenance_differences
            ],
            "play_count_diagnostics": [
                value.to_dict() for value in self.play_count_diagnostics
            ],
            "stop_reason_after_step": self.stop_reason_after_step,
            "issues": [value.to_dict() for value in self.issues],
        }

    def to_json_line(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )


@dataclass(frozen=True, slots=True)
class Plan2NativeUnattendedLoopResult:
    schema_version: int
    status: str
    max_actions: int
    actions_executed: int
    terminal_reached: bool
    stop_reason: str
    records: tuple[Plan2NativeUnattendedStepRecord, ...]

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION:
            raise ValueError("unsupported Plan2 unattended loop schema")
        if self.status not in {RUN_COMPLETED, RUN_STOPPED}:
            raise ValueError("unsupported unattended loop status")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "max_actions": self.max_actions,
            "actions_executed": self.actions_executed,
            "terminal_reached": self.terminal_reached,
            "stop_reason": self.stop_reason,
            "records": [value.to_dict() for value in self.records],
        }

    def to_jsonl(self) -> str:
        if not self.records:
            return ""
        return "".join(f"{value.to_json_line()}\n" for value in self.records)


@dataclass(frozen=True, slots=True)
class Plan2NativeUnattendedDependencies:
    settled_evidence_reader: SettledEvidenceReader
    decision_orchestrator: DecisionOrchestrator
    action_executor: ActionExecutor
    record_sink: RecordSink | None = None
    logical_state_reader: LogicalStateReader | None = None
    logical_decision_orchestrator: LogicalDecisionOrchestrator | None = None

    def __post_init__(self) -> None:
        for name in (
            "settled_evidence_reader",
            "decision_orchestrator",
            "action_executor",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be callable")
        if self.record_sink is not None and not callable(self.record_sink):
            raise TypeError("record_sink must be callable or None")
        if self.logical_state_reader is not None and not callable(
            self.logical_state_reader
        ):
            raise TypeError("logical_state_reader must be callable or None")
        if self.logical_decision_orchestrator is not None and not callable(
            self.logical_decision_orchestrator
        ):
            raise TypeError("logical_decision_orchestrator must be callable or None")


@dataclass(frozen=True, slots=True)
class Plan2NativeJsonlRecordSink:
    path: Path

    def __init__(self, path: str | Path) -> None:
        object.__setattr__(self, "path", Path(path))

    def __call__(self, record: Plan2NativeUnattendedStepRecord) -> None:
        append_plan2_native_unattended_jsonl(self.path, record)


def append_plan2_native_unattended_jsonl(
    path: str | Path,
    record: Plan2NativeUnattendedStepRecord,
) -> None:
    """Append one complete structured record as one JSONL line."""

    if not isinstance(record, Plan2NativeUnattendedStepRecord):
        raise TypeError("record must be Plan2NativeUnattendedStepRecord")
    with Path(path).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(record.to_json_line())
        stream.write("\n")


def bind_plan2_native_exam_save_orchestrator(
    *,
    draw_count: int | None,
    hand_limit: int | None,
    limits: Plan2NativeExpectimaxLimits = Plan2NativeExpectimaxLimits(),
    weights: Plan2NativeEvaluationWeights = Plan2NativeEvaluationWeights(),
    database: str | Path = DEFAULT_DATABASE,
    dependencies: Plan2NativeExamSaveDependencies = Plan2NativeExamSaveDependencies(),
) -> DecisionOrchestrator:
    """Bind search/catalog settings into the existing one-state orchestrator."""

    catalog_cache: list[Plan2NativeProgramCatalogCompilation] = []

    def compile_catalog_once(path: Path) -> Plan2NativeProgramCatalogCompilation:
        # Master is immutable for one bound unattended run.  Compiling all
        # supported card versions on every settled ExamSave used to add a
        # fixed multi-second cost before every search and supplied no new
        # authority.  Share this exact typed compilation between fresh-save
        # and retained logical-horizon decisions.
        if not catalog_cache:
            compilation = dependencies.catalog_compiler(path)
            if not isinstance(
                compilation,
                Plan2NativeProgramCatalogCompilation,
            ):
                raise TypeError("catalog compiler returned an invalid compilation")
            catalog_cache.append(compilation)
        return catalog_cache[0]

    cached_dependencies = replace(
        dependencies,
        catalog_compiler=compile_catalog_once,
    )

    def enumerate_bound_root(
        root: Plan2NativeHorizonState,
        evidence: AuditionLocalSaveStateEvidence | None,
    ) -> tuple[Plan2NativeOfflineAction, ...] | None:
        """Enumerate one root and, when physical, bind it to the same save."""

        if root.terminal or root.unmapped_drink_slots:
            return None
        if evidence is not None:
            binding = root.zones.binding
            expected = {
                "run_id": evidence.run_id,
                "step_context_id": evidence.step_context_id,
                "step_context_digest": evidence.step_context_digest,
                "session_transition_id": evidence.session_transition_id,
                "zone_checkpoint_digest": evidence.zone_checkpoint_digest,
                "local_save_source_sha256": evidence.source_sha256,
                "local_save_evidence_digest": evidence.digest(),
            }
            if any(getattr(binding, name) != value for name, value in expected.items()):
                return None

            def card_identity(value: object) -> tuple[object, ...]:
                return (
                    getattr(value, "guid", None),
                    getattr(value, "card_id", None),
                    getattr(value, "base_upgrade", None),
                    getattr(value, "temporary_upgrade", None),
                    getattr(value, "effective_upgrade", None),
                    tuple(getattr(value, "support_upgrade_ids", ())),
                    getattr(value, "fixed_deck_order", None),
                    getattr(value, "runtime_state_digest", None),
                )

            if tuple(map(card_identity, root.zones.hand)) != tuple(
                map(card_identity, evidence.state.zones.hand)
            ):
                return None
        actions = tuple(
            enumerate_plan2_native_actions(
                root, compile_catalog_once(Path(database)).catalog
            ).actions
        )
        if evidence is not None:
            hand_guids = frozenset(card.guid for card in evidence.state.zones.hand)
            runtime = evidence.state.root_runtime
            opaque = None if runtime is None else runtime.opaque_fields.to_value()
            raw_drinks = opaque.get("drinkList") if isinstance(opaque, Mapping) else None
            if not isinstance(raw_drinks, list):
                return None
            drink_ids = []
            for row in raw_drinks:
                if not isinstance(row, Mapping) or not isinstance(row.get("_id"), str):
                    return None
                drink_ids.append(row["_id"])
            for action in actions:
                if isinstance(action, Plan2NativeAction) and action.kind == "play":
                    if action.card_guid not in hand_guids:
                        return None
                elif isinstance(action, Plan2NativeDrinkAction):
                    if (
                        action.slot_index < 0
                        or action.slot_index >= len(drink_ids)
                        or drink_ids[action.slot_index] != action.drink_id
                        or action.instance_id
                        != f"localsave-drink:{action.slot_index}:{action.drink_id}"
                    ):
                        return None
        return actions

    def prepare_learned_root(
        root: Plan2NativeHorizonState,
        base: Plan2NativeExamSaveDecision,
        compilation: Plan2NativeProgramCatalogCompilation,
        evidence: AuditionLocalSaveStateEvidence | None = None,
    ) -> Plan2NativeLearnedBoundary:
        blockers = list(base.blockers)
        diagnostics: list[object] = []
        prepared = root
        if not prepared.terminal and not blockers:
            try:
                symbolic = prepare_plan2_symbolic_generated_inputs(
                    prepared,
                    compilation.catalog,
                    database=compilation.master_database,
                    expansion_depth=limits.max_depth,
                )
            except Exception as error:
                blockers.append(
                    Plan2NativeBlocker(
                        "plan2-symbolic-preparation-failed",
                        f"{type(error).__name__}:{error}",
                    )
                )
            else:
                prepared = symbolic.state
                diagnostics.extend(symbolic.blockers)
        actions: tuple[Plan2NativeOfflineAction, ...] = ()
        if not prepared.terminal and not blockers:
            supplied = enumerate_bound_root(prepared, evidence)
            actions = () if supplied is None else supplied
        prepared_base = replace(
            base,
            logical_root=prepared,
            blockers=tuple(dict.fromkeys(blockers)),
        )
        return Plan2NativeLearnedBoundary(
            prepared_base,
            prepared,
            compilation.catalog,
            tuple(actions),
            prepared_base.blockers,
            tuple(diagnostics),
        )

    def plan2_native_boundary_provider(
        evidence: AuditionLocalSaveStateEvidence,
    ) -> Plan2NativeLearnedBoundary:
        """Prepare one exact boundary without invoking the search planner."""

        if not isinstance(evidence, AuditionLocalSaveStateEvidence):
            raise TypeError("learned boundary evidence must be typed")
        compilation = compile_catalog_once(Path(database))
        coverage = _coverage_report(evidence, compilation)
        blockers: list[Plan2NativeBlocker] = [
            Plan2NativeBlocker(
                "exam-save-current-card-program-missing",
                f"{value.guid}:{value.card_id}@{value.effective_upgrade}",
            )
            for value in coverage.current_cards
            if not value.available
        ]
        try:
            bootstrap = cached_dependencies.bootstrapper(
                evidence,
                compilation.catalog,
                draw_count,
                hand_limit,
            )
        except Exception as error:
            bootstrap = None
            blockers.append(
                Plan2NativeBlocker(
                    "exam-save-bootstrap-failed",
                    f"{type(error).__name__}:{error}",
                )
            )
        if not isinstance(bootstrap, Plan2NativeLocalSaveBootstrapAudit):
            if bootstrap is not None:
                blockers.append(Plan2NativeBlocker("exam-save-bootstrap-contract"))
            base = Plan2NativeExamSaveDecision(
                PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
                "evidence",
                str(evidence.source_path),
                coverage,
                None,
                None,
                None,
                tuple(dict.fromkeys(blockers)),
            )
            return Plan2NativeLearnedBoundary(
                base, None, compilation.catalog, (), base.blockers
            )
        blockers.extend(bootstrap.blockers)
        base = Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            "evidence",
            str(evidence.source_path),
            coverage,
            bootstrap,
            None,
            None,
            tuple(dict.fromkeys(blockers)),
        )
        if bootstrap.state is None:
            return Plan2NativeLearnedBoundary(
                base, None, compilation.catalog, (), base.blockers
            )
        return prepare_learned_root(bootstrap.state, base, compilation, evidence)

    def plan2_native_horizon_boundary_provider(
        root: Plan2NativeHorizonState,
    ) -> Plan2NativeLearnedBoundary:
        if not isinstance(root, Plan2NativeHorizonState):
            raise TypeError("learned horizon root must be typed")
        compilation = compile_catalog_once(Path(database))
        base = Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            "horizon",
            "",
            None,
            None,
            None,
            None,
            (),
            logical_root=root,
        )
        return prepare_learned_root(root, base, compilation)

    def plan(evidence: AuditionLocalSaveStateEvidence) -> Plan2NativeExamSaveDecision:
        return decide_plan2_native_exam_save(
            evidence,
            draw_count=draw_count,
            hand_limit=hand_limit,
            limits=limits,
            weights=weights,
            database=database,
            dependencies=cached_dependencies,
        )

    def plan_horizon(
        state: Plan2NativeHorizonState,
    ) -> Plan2NativeExamSaveDecision:
        compilation = compile_catalog_once(Path(database))
        return decide_plan2_native_horizon(
            state,
            compilation.catalog,
            limits=limits,
            weights=weights,
            planner=dependencies.planner,
            transitioner=dependencies.transitioner,
        )

    def plan2_native_candidate_provider(
        decision: Plan2NativeExamSaveDecision,
        evidence: AuditionLocalSaveStateEvidence | None = None,
    ) -> tuple[Plan2NativeOfflineAction, ...] | None:
        """Return the complete typed action set for one planned boundary.

        This deliberately consumes ``decision.search.root`` and the same
        cached catalog used by the bound planner.  The principal action is
        not treated as the candidate set: every native-enumerated play,
        mapped drink, and explicit END_TURN must be present before a caller
        may claim complete legality evidence.

        ``None`` is the fail-closed result for a terminal/incomplete decision,
        an unmapped ordered drink inventory, a root that is not bound to the
        supplied ExamSave, or a principal action that is absent from the
        enumeration.
        """

        if not isinstance(decision, Plan2NativeExamSaveDecision):
            raise TypeError("decision must be Plan2NativeExamSaveDecision")
        if not decision.decision_ready:
            return None
        root = (
            decision.logical_root
            if decision.logical_root is not None
            else decision.search.root
            if decision.search is not None
            else decision.root_state
        )
        if root is None:
            return None
        supplied = enumerate_bound_root(root, evidence)
        actions = () if supplied is None else supplied
        if not actions or decision.best_action is None:
            return None
        if decision.best_action not in actions:
            return None
        return actions

    # The returned object remains backward-compatible as a one-argument
    # callable, while bridges can discover the exact logical continuation.
    setattr(plan, "logical_horizon_orchestrator", plan_horizon)
    setattr(plan, "plan2_native_boundary_provider", plan2_native_boundary_provider)
    setattr(
        plan,
        "plan2_native_horizon_boundary_provider",
        plan2_native_horizon_boundary_provider,
    )
    # This is an observation-only side channel for transition collection.  It
    # shares the planner's catalog cache and never dispatches an action.
    setattr(plan, "plan2_native_candidate_provider", plan2_native_candidate_provider)
    # Learned policies reuse the exact same immutable catalog and transition
    # boundary.  Exposing these pure providers avoids a second Master compile
    # or a parallel action implementation while leaving the planner callable
    # backward compatible.
    setattr(
        plan,
        "plan2_native_catalog_provider",
        lambda: compile_catalog_once(Path(database)).catalog,
    )
    setattr(plan, "plan2_native_transitioner", cached_dependencies.transitioner)

    return plan


def _root(decision: Plan2NativeExamSaveDecision | None) -> Plan2NativeHorizonState | None:
    return None if decision is None else decision.root_state


def _status(stop_reason: str) -> str:
    return RUN_COMPLETED if stop_reason in {STOP_TERMINAL, STOP_MAX_ACTIONS} else RUN_STOPPED


def _result(
    *,
    max_actions: int,
    actions_executed: int,
    terminal_reached: bool,
    stop_reason: str,
    records: list[Plan2NativeUnattendedStepRecord],
) -> Plan2NativeUnattendedLoopResult:
    return Plan2NativeUnattendedLoopResult(
        PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
        _status(stop_reason),
        max_actions,
        actions_executed,
        terminal_reached,
        stop_reason,
        tuple(records),
    )


def _emit(
    record: Plan2NativeUnattendedStepRecord,
    sink: RecordSink | None,
) -> tuple[Plan2NativeUnattendedStepRecord, Plan2NativeLoopIssue | None]:
    if sink is None:
        return record, None
    try:
        sink(record)
    except Exception as error:
        issue = Plan2NativeLoopIssue(
            "record-sink-failed", f"{type(error).__name__}:{error}"
        )
        return (
            replace(
                record,
                stop_reason_after_step=STOP_RECORD_SINK_ERROR,
                issues=(*record.issues, issue),
            ),
            issue,
        )
    return record, None


def _read(
    reader: SettledEvidenceReader,
    previous: AuditionLocalSaveStateEvidence | None,
) -> tuple[AuditionLocalSaveStateEvidence | None, Plan2NativeLoopIssue | None]:
    try:
        evidence = reader(previous)
    except Exception as error:
        return None, Plan2NativeLoopIssue(
            "settled-evidence-read-failed", f"{type(error).__name__}:{error}"
        )
    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        return None, Plan2NativeLoopIssue("settled-evidence-reader-contract")
    return evidence, None


def _plan(
    orchestrator: DecisionOrchestrator,
    evidence: AuditionLocalSaveStateEvidence,
) -> tuple[Plan2NativeExamSaveDecision | None, Plan2NativeLoopIssue | None]:
    try:
        decision = orchestrator(evidence)
    except Exception as error:
        return None, Plan2NativeLoopIssue(
            "decision-orchestrator-failed", f"{type(error).__name__}:{error}"
        )
    if not isinstance(decision, Plan2NativeExamSaveDecision):
        return None, Plan2NativeLoopIssue("decision-orchestrator-contract")
    return decision, None


def _plan_logical(
    orchestrator: LogicalDecisionOrchestrator | None,
    state: Plan2NativeHorizonState,
) -> tuple[Plan2NativeExamSaveDecision | None, Plan2NativeLoopIssue | None]:
    if orchestrator is None:
        return None, Plan2NativeLoopIssue(
            "logical-continuation-planner-unbound",
            "replay-proven horizon has no logical decision orchestrator",
        )
    try:
        decision = orchestrator(state)
    except Exception as error:
        return None, Plan2NativeLoopIssue(
            "logical-decision-orchestrator-failed",
            f"{type(error).__name__}:{error}",
        )
    if not isinstance(decision, Plan2NativeExamSaveDecision):
        return None, Plan2NativeLoopIssue(
            "logical-decision-orchestrator-contract"
        )
    return decision, None


def _logical_state(
    reader: LogicalStateReader | None,
    evidence: AuditionLocalSaveStateEvidence,
) -> tuple[Plan2NativeHorizonState | None, Plan2NativeLoopIssue | None]:
    if reader is None:
        return None, None
    try:
        state = reader(evidence)
    except Exception as error:
        return None, Plan2NativeLoopIssue(
            "logical-state-read-failed", f"{type(error).__name__}:{error}"
        )
    if state is not None and not isinstance(state, Plan2NativeHorizonState):
        return None, Plan2NativeLoopIssue("logical-state-reader-contract")
    return state, None


def run_plan2_native_unattended_loop(
    *,
    dependencies: Plan2NativeUnattendedDependencies,
    max_actions: int = 100,
) -> Plan2NativeUnattendedLoopResult:
    """Dispatch semantic Plan2 actions until a deterministic hard stop."""

    if not isinstance(dependencies, Plan2NativeUnattendedDependencies):
        raise TypeError("dependencies must be Plan2NativeUnattendedDependencies")
    if type(max_actions) is not int or max_actions < 1:
        raise ValueError("max_actions must be a positive integer")

    records: list[Plan2NativeUnattendedStepRecord] = []
    actions_executed = 0
    evidence, read_issue = _read(dependencies.settled_evidence_reader, None)
    if evidence is None:
        record = Plan2NativeUnattendedStepRecord(
            PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
            1,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            (),
            STOP_EVIDENCE_READ_ERROR,
            () if read_issue is None else (read_issue,),
        )
        record, sink_issue = _emit(record, dependencies.record_sink)
        records.append(record)
        reason = STOP_RECORD_SINK_ERROR if sink_issue is not None else STOP_EVIDENCE_READ_ERROR
        return _result(
            max_actions=max_actions,
            actions_executed=0,
            terminal_reached=False,
            stop_reason=reason,
            records=records,
        )

    initial_logical, logical_issue = _logical_state(
        dependencies.logical_state_reader,
        evidence,
    )
    if logical_issue is not None:
        decision = None
        plan_issue = logical_issue
    elif initial_logical is not None:
        decision, plan_issue = _plan_logical(
            dependencies.logical_decision_orchestrator,
            initial_logical,
        )
    else:
        decision, plan_issue = _plan(
            dependencies.decision_orchestrator,
            evidence,
        )
    step_index = 1
    while True:
        predicted_before = _root(decision)
        predicted_after = None if decision is None else decision.predicted_after_state
        if plan_issue is not None or decision is None:
            record = Plan2NativeUnattendedStepRecord(
                PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
                step_index,
                evidence,
                decision,
                None,
                None,
                predicted_before,
                predicted_after,
                None,
                None,
                (),
                STOP_DECISION_UNAVAILABLE,
                () if plan_issue is None else (plan_issue,),
            )
            record, sink_issue = _emit(record, dependencies.record_sink)
            records.append(record)
            return _result(
                max_actions=max_actions,
                actions_executed=actions_executed,
                terminal_reached=False,
                stop_reason=(
                    STOP_RECORD_SINK_ERROR
                    if sink_issue is not None
                    else STOP_DECISION_UNAVAILABLE
                ),
                records=records,
            )

        if decision.terminal and decision.decision_ready:
            record = Plan2NativeUnattendedStepRecord(
                PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
                step_index,
                evidence,
                decision,
                None,
                None,
                predicted_before,
                predicted_after,
                None,
                None,
                (),
                STOP_TERMINAL,
            )
            record, sink_issue = _emit(record, dependencies.record_sink)
            records.append(record)
            reason = STOP_RECORD_SINK_ERROR if sink_issue is not None else STOP_TERMINAL
            return _result(
                max_actions=max_actions,
                actions_executed=actions_executed,
                terminal_reached=True,
                stop_reason=reason,
                records=records,
            )

        action = decision.best_action
        if not decision.decision_ready or action is None or predicted_before is None:
            record = Plan2NativeUnattendedStepRecord(
                PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
                step_index,
                evidence,
                decision,
                action,
                None,
                predicted_before,
                predicted_after,
                None,
                None,
                (),
                STOP_DECISION_UNAVAILABLE,
            )
            record, sink_issue = _emit(record, dependencies.record_sink)
            records.append(record)
            return _result(
                max_actions=max_actions,
                actions_executed=actions_executed,
                terminal_reached=False,
                stop_reason=(
                    STOP_RECORD_SINK_ERROR
                    if sink_issue is not None
                    else STOP_DECISION_UNAVAILABLE
                ),
                records=records,
            )

        execution_issue: Plan2NativeLoopIssue | None = None
        try:
            execution = dependencies.action_executor(action)
        except Exception as error:
            execution = None
            execution_issue = Plan2NativeLoopIssue(
                "action-executor-failed", f"{type(error).__name__}:{error}"
            )
        if execution is not None and not isinstance(
            execution, Plan2NativeActionExecution
        ):
            execution = None
            execution_issue = Plan2NativeLoopIssue("action-executor-contract")
        if execution is None or execution_issue is not None:
            record = Plan2NativeUnattendedStepRecord(
                PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
                step_index,
                evidence,
                decision,
                action,
                execution,
                predicted_before,
                predicted_after,
                None,
                None,
                (),
                STOP_ACTION_EXECUTOR_ERROR,
                () if execution_issue is None else (execution_issue,),
            )
            record, sink_issue = _emit(record, dependencies.record_sink)
            records.append(record)
            return _result(
                max_actions=max_actions,
                actions_executed=actions_executed,
                terminal_reached=False,
                stop_reason=(
                    STOP_RECORD_SINK_ERROR
                    if sink_issue is not None
                    else STOP_ACTION_EXECUTOR_ERROR
                ),
                records=records,
            )

        if not execution.accepted and not execution.replan:
            record = Plan2NativeUnattendedStepRecord(
                PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
                step_index,
                evidence,
                decision,
                action,
                execution,
                predicted_before,
                predicted_after,
                None,
                None,
                (),
                STOP_ACTION_REJECTED,
            )
            record, sink_issue = _emit(record, dependencies.record_sink)
            records.append(record)
            return _result(
                max_actions=max_actions,
                actions_executed=actions_executed,
                terminal_reached=False,
                stop_reason=(
                    STOP_RECORD_SINK_ERROR
                    if sink_issue is not None
                    else STOP_ACTION_REJECTED
                ),
                records=records,
            )

        if execution.accepted:
            actions_executed += 1
        next_evidence, read_issue = _read(
            dependencies.settled_evidence_reader, evidence
        )
        if next_evidence is None:
            record = Plan2NativeUnattendedStepRecord(
                PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
                step_index,
                evidence,
                decision,
                action,
                execution,
                predicted_before,
                predicted_after,
                None,
                None,
                (),
                STOP_EVIDENCE_READ_ERROR,
                () if read_issue is None else (read_issue,),
            )
            record, sink_issue = _emit(record, dependencies.record_sink)
            records.append(record)
            return _result(
                max_actions=max_actions,
                actions_executed=actions_executed,
                terminal_reached=False,
                stop_reason=(
                    STOP_RECORD_SINK_ERROR
                    if sink_issue is not None
                    else STOP_EVIDENCE_READ_ERROR
                ),
                records=records,
            )

        replayed_next: Plan2NativeHorizonState | None = None
        logical_issue: Plan2NativeLoopIssue | None = None
        if next_evidence.state == evidence.state:
            # A submitted drink can finish in the UI before LocalSave replaces
            # the preceding drink queue.  The MAA bridge may therefore return
            # the *same persisted evidence* with an exact, receipt-backed
            # logical replay.  Accept only that complete predicted transition;
            # ordinary same-state acknowledgements remain a hard stop.
            replayed_next, logical_issue = _logical_state(
                dependencies.logical_state_reader,
                next_evidence,
            )
            exact_logical_progress = (
                execution.accepted
                and not execution.replan
                and logical_issue is None
                and replayed_next is not None
                and predicted_after is not None
                and replayed_next == predicted_after
                and replayed_next != predicted_before
            )
            if not exact_logical_progress:
                actual_next = (
                    replayed_next
                    if logical_issue is None and replayed_next is not None
                    else None
                )
                differences = compare_plan2_native_states(
                    predicted_after,
                    actual_next,
                )
                record = Plan2NativeUnattendedStepRecord(
                    PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
                    step_index,
                    evidence,
                    decision,
                    action,
                    execution,
                    predicted_before,
                    predicted_after,
                    next_evidence,
                    actual_next,
                    differences,
                    STOP_SAME_STATE_NO_PROGRESS,
                    () if logical_issue is None else (logical_issue,),
                )
                record, sink_issue = _emit(record, dependencies.record_sink)
                records.append(record)
                return _result(
                    max_actions=max_actions,
                    actions_executed=actions_executed,
                    terminal_reached=False,
                    stop_reason=(
                        STOP_RECORD_SINK_ERROR
                        if sink_issue is not None
                        else STOP_SAME_STATE_NO_PROGRESS
                    ),
                    records=records,
                )

        # ExamSave's completed flag is the durable terminal authority.  A
        # phase-8 save can still retain presentation/disposition fields that
        # the phase-6 simulator deliberately cannot bootstrap; do not send a
        # completed native exam through that bootstrap merely to prove it a
        # second time.
        next_runtime = next_evidence.state.root_runtime
        if next_runtime is not None and next_runtime.is_exam_end_complete:
            record = Plan2NativeUnattendedStepRecord(
                PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
                step_index,
                evidence,
                decision,
                action,
                execution,
                predicted_before,
                predicted_after,
                next_evidence,
                None,
                (),
                STOP_TERMINAL,
            )
            record, sink_issue = _emit(record, dependencies.record_sink)
            records.append(record)
            return _result(
                max_actions=max_actions,
                actions_executed=actions_executed,
                terminal_reached=True,
                stop_reason=(
                    STOP_RECORD_SINK_ERROR
                    if sink_issue is not None
                    else STOP_TERMINAL
                ),
                records=records,
            )

        if replayed_next is None:
            replayed_next, logical_issue = _logical_state(
                dependencies.logical_state_reader,
                next_evidence,
            )
        if logical_issue is not None:
            record = Plan2NativeUnattendedStepRecord(
                PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
                step_index,
                evidence,
                decision,
                action,
                execution,
                predicted_before,
                predicted_after,
                next_evidence,
                None,
                (),
                STOP_DECISION_UNAVAILABLE,
                (logical_issue,),
            )
            record, sink_issue = _emit(record, dependencies.record_sink)
            records.append(record)
            return _result(
                max_actions=max_actions,
                actions_executed=actions_executed,
                terminal_reached=False,
                stop_reason=(
                    STOP_RECORD_SINK_ERROR
                    if sink_issue is not None
                    else STOP_DECISION_UNAVAILABLE
                ),
                records=records,
            )
        comparison_source = (
            predicted_before if execution.replan else predicted_after
        )
        if replayed_next is not None and actions_executed >= max_actions:
            differences = compare_plan2_native_states(
                comparison_source, replayed_next
            )
            record = Plan2NativeUnattendedStepRecord(
                PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
                step_index,
                evidence,
                decision,
                action,
                execution,
                predicted_before,
                predicted_after,
                next_evidence,
                replayed_next,
                differences,
                STOP_MAX_ACTIONS,
            )
            record, sink_issue = _emit(record, dependencies.record_sink)
            records.append(record)
            return _result(
                max_actions=max_actions,
                actions_executed=actions_executed,
                terminal_reached=False,
                stop_reason=(
                    STOP_RECORD_SINK_ERROR
                    if sink_issue is not None
                    else STOP_MAX_ACTIONS
                ),
                records=records,
            )
        if replayed_next is not None:
            next_decision, next_plan_issue = _plan_logical(
                dependencies.logical_decision_orchestrator,
                replayed_next,
            )
            actual_next = replayed_next
            comparison = None
        else:
            next_decision, next_plan_issue = _plan(
                dependencies.decision_orchestrator, next_evidence
            )
            actual_next = _root(next_decision)
            comparison = compare_plan2_native_prediction_with_local_save(
                comparison_source,
                actual_next,
            )
        differences = (
            compare_plan2_native_states(comparison_source, actual_next)
            if comparison is None
            else comparison.differences
        )
        if next_plan_issue is not None or next_decision is None or actual_next is None:
            stop_reason = STOP_DECISION_UNAVAILABLE
            issues = () if next_plan_issue is None else (next_plan_issue,)
            terminal_reached = False
        elif next_decision.terminal and next_decision.decision_ready:
            stop_reason = STOP_TERMINAL
            issues = ()
            terminal_reached = True
        elif not next_decision.decision_ready:
            stop_reason = STOP_DECISION_UNAVAILABLE
            issues = ()
            terminal_reached = False
        elif actions_executed >= max_actions:
            stop_reason = STOP_MAX_ACTIONS
            issues = ()
            terminal_reached = False
        else:
            stop_reason = None
            issues = ()
            terminal_reached = False

        record = Plan2NativeUnattendedStepRecord(
            PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION,
            step_index,
            evidence,
            decision,
            action,
            execution,
            predicted_before,
            predicted_after,
            next_evidence,
            actual_next,
            differences,
            stop_reason,
            issues,
            () if comparison is None else comparison.provenance_differences,
            () if comparison is None else comparison.play_count_diagnostics,
        )
        record, sink_issue = _emit(record, dependencies.record_sink)
        records.append(record)
        if sink_issue is not None:
            return _result(
                max_actions=max_actions,
                actions_executed=actions_executed,
                terminal_reached=terminal_reached,
                stop_reason=STOP_RECORD_SINK_ERROR,
                records=records,
            )
        if stop_reason is not None:
            return _result(
                max_actions=max_actions,
                actions_executed=actions_executed,
                terminal_reached=terminal_reached,
                stop_reason=stop_reason,
                records=records,
            )

        evidence = next_evidence
        decision = next_decision
        plan_issue = None
        step_index += 1


__all__ = [
    "ExamBoundaryDisposition",
    "PLAN2_NATIVE_UNATTENDED_LOOP_SCHEMA_VERSION",
    "RUN_COMPLETED",
    "RUN_STOPPED",
    "STOP_ACTION_EXECUTOR_ERROR",
    "STOP_ACTION_REJECTED",
    "STOP_DECISION_UNAVAILABLE",
    "STOP_EVIDENCE_READ_ERROR",
    "STOP_MAX_ACTIONS",
    "STOP_RECORD_SINK_ERROR",
    "STOP_SAME_STATE_NO_PROGRESS",
    "STOP_TERMINAL",
    "Plan2NativeActionExecution",
    "Plan2NativeJsonlRecordSink",
    "Plan2NativeLoopIssue",
    "LogicalDecisionOrchestrator",
    "Plan2NativePlannerCandidateProvider",
    "Plan2NativePlayCountAuthorityDiagnostic",
    "Plan2NativeStateComparison",
    "Plan2NativeStateDifference",
    "Plan2NativeUnattendedDependencies",
    "Plan2NativeUnattendedLoopResult",
    "Plan2NativeUnattendedStepRecord",
    "append_plan2_native_unattended_jsonl",
    "bind_plan2_native_exam_save_orchestrator",
    "compare_plan2_native_prediction_with_local_save",
    "compare_plan2_native_states",
    "run_plan2_native_unattended_loop",
]
