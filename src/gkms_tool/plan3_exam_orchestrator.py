"""Repeat the existing one-action Plan 3 executor until a hard stop.

The orchestration layer does not inspect screenshots or send controller input
itself.  In execute mode it delegates exactly one semantic action at a time to
``execute_plan3_exam_first_action``.  A second action is requested only after
that call has returned a changed result without diagnostics.  Ordinary results
must be actionable settled states; an exact same-turn completed-card replay is
carried forward with the archived pre-action settled snapshot and card GUID.

Dry-run is deliberately a one-step planning operation.  Repeating a dry-run
would keep advising from the same unchanged LocalSave forever.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import sys
from typing import Callable, Mapping, Protocol, Sequence
import uuid

from .plan3_exam_executor import (
    ADVISOR_MODES,
    MODE_AUTO,
    STATUS_EXECUTED,
    STATUS_FAILED,
    STATUS_PLANNED,
    STATUS_UNAVAILABLE,
    MaaPlan3ActionDriver,
    Plan3AuditionStepExecutionResult,
    Plan3ExecutionIssue,
    execute_plan3_exam_first_action,
    is_plan3_exam_actionable_settled,
)
from .plan3_card_history import Plan3CompletedCardReplay
from .plan3_local_save_bridge import decode_plan3_local_save_file


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARCHIVE_ROOT = PROJECT_ROOT / "var" / "plan3_exam_orchestrator"

SCHEMA_NAME = "gkms_tool.plan3_exam_orchestration"
STEP_SCHEMA_NAME = "gkms_tool.plan3_exam_orchestration_step"
SCHEMA_VERSION = 1

RUN_STATUS_PLANNED = "planned"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_STOPPED = "stopped"

STOP_DRY_RUN_ONE_STEP = "dry-run-one-step"
STOP_TERMINAL = "terminal"
STOP_MAX_ACTIONS = "max-actions-reached"
STOP_STEP_UNAVAILABLE = "step-unavailable"
STOP_STEP_FAILED = "step-failed"
STOP_STEP_DIAGNOSTIC = "step-diagnostic"
STOP_UNEXPECTED_STEP_STATUS = "unexpected-step-status"


class StepExecutor(Protocol):
    def __call__(
        self,
        path: str | Path,
        *,
        advisor_mode: str,
        dry_run: bool,
        timeout_seconds: float,
        poll_interval_seconds: float,
        driver: MaaPlan3ActionDriver | None,
        prior_settled_path: str | Path | None,
        completed_card_guid: str | None,
        prior_completed_card_replay: Plan3CompletedCardReplay | None,
    ) -> Plan3AuditionStepExecutionResult: ...


class StepObserver(Protocol):
    def __call__(
        self,
        step: "Plan3ExamOrchestrationStep",
        pre_action_snapshot: Path | None,
        post_action_snapshot: Path | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class Plan3ExamOrchestrationStep:
    index: int
    execution: Mapping[str, object]
    semantic_action_executed: bool
    terminal: bool
    diagnostics: tuple[Mapping[str, object], ...]
    stop_reason_after_step: str | None
    prediction_actual_archive: str | None
    bootstrap: Mapping[str, object] | None
    provenance: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": STEP_SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "index": self.index,
            "semantic_action_executed": self.semantic_action_executed,
            "terminal": self.terminal,
            "diagnostics": [dict(item) for item in self.diagnostics],
            "stop_reason_after_step": self.stop_reason_after_step,
            "prediction_actual_archive": self.prediction_actual_archive,
            "bootstrap": (
                None if self.bootstrap is None else dict(self.bootstrap)
            ),
            "provenance": dict(self.provenance),
            "execution": dict(self.execution),
        }


@dataclass(frozen=True, slots=True)
class Plan3ExamOrchestrationResult:
    status: str
    dry_run: bool
    advisor_mode: str
    source: str
    max_actions: int
    semantic_actions_executed: int
    terminal_reached: bool
    stop_reason: str
    archive_directory: str
    bootstrap: Mapping[str, object] | None
    steps: tuple[Plan3ExamOrchestrationStep, ...]
    native_runtime_stage_capture: Mapping[str, object] | None = None
    source_run_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "dry_run": self.dry_run,
            "advisor_mode": self.advisor_mode,
            "source": self.source,
            "max_actions": self.max_actions,
            "semantic_actions_executed": self.semantic_actions_executed,
            "terminal_reached": self.terminal_reached,
            "stop_reason": self.stop_reason,
            "archive_directory": self.archive_directory,
            "bootstrap": (
                None if self.bootstrap is None else dict(self.bootstrap)
            ),
            "steps": [step.to_dict() for step in self.steps],
            **({"native_runtime_stage_capture": dict(self.native_runtime_stage_capture)}
               if self.native_runtime_stage_capture is not None else {}),
            **({"source_run_id": self.source_run_id} if self.source_run_id is not None else {}),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, indent=indent, allow_nan=False
        )


def _default_archive_directory() -> Path:
    return DEFAULT_ARCHIVE_ROOT / f"run-{uuid.uuid4().hex}"


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _actual_terminal(result: Plan3AuditionStepExecutionResult) -> bool:
    artifact = result.prediction_actual
    if not isinstance(artifact, Mapping):
        return False
    actual = artifact.get("actual")
    return bool(isinstance(actual, Mapping) and actual.get("terminal") is True)


def _executor_outcome_evidence(
    result: Plan3AuditionStepExecutionResult,
) -> tuple[dict[str, object], tuple[Mapping[str, object], ...]]:
    if result.status != STATUS_EXECUTED:
        return {"kind": "not-executed"}, ()
    artifact = result.prediction_actual
    actual = artifact.get("actual") if isinstance(artifact, Mapping) else None
    local_save = actual.get("local_save") if isinstance(actual, Mapping) else None
    if not isinstance(local_save, Mapping):
        return (
            {"kind": "missing"},
            (
                {
                    "code": "executor-outcome-evidence-missing",
                    "detail": (
                        "prediction_actual.actual.local_save is required before "
                        "another autonomous action"
                    ),
                },
            ),
        )
    replay = local_save.get("executor_completed_card_replay")
    actionable = local_save.get("executor_actionable_settled")
    if not isinstance(replay, bool) or not isinstance(actionable, bool):
        return (
            {"kind": "invalid"},
            (
                {
                    "code": "executor-outcome-evidence-invalid",
                    "detail": (
                        "executor_completed_card_replay and "
                        "executor_actionable_settled must be boolean"
                    ),
                },
            ),
        )
    terminal = bool(isinstance(actual, Mapping) and actual.get("terminal") is True)
    if replay:
        provenance = local_save.get(
            "executor_completed_card_replay_provenance"
        )
        if provenance not in {
            "completed-card-replay",
            "completed-card-replay-chain",
        }:
            return (
                {"kind": "invalid"},
                (
                    {
                        "code": "executor-replay-provenance-invalid",
                        "detail": (
                            "completed replay requires exact single/chain "
                            "provenance"
                        ),
                    },
                ),
            )
        kind = str(provenance)
    elif terminal:
        kind = "terminal"
    elif actionable:
        kind = "ordinary-settled"
    else:
        return (
            {
                "kind": "unsettled",
                "executor_completed_card_replay": False,
                "executor_actionable_settled": False,
            },
            (
                {
                    "code": "executor-outcome-not-settled",
                    "detail": (
                        "executed outcome is neither terminal, ordinary settled, "
                        "nor an exact completed-card replay"
                    ),
                },
            ),
        )
    return (
        {
            "kind": kind,
            "executor_completed_card_replay": replay,
            "executor_actionable_settled": actionable,
            "executor_completed_card_replay_provenance": (
                local_save.get("executor_completed_card_replay_provenance")
            ),
        },
        (),
    )


def _derive_next_bootstrap(
    result: Plan3AuditionStepExecutionResult,
    *,
    step_index: int,
    input_bootstrap: Mapping[str, object] | None,
    pre_action_snapshot: Path | None,
    post_action_snapshot: Path | None,
    outcome_evidence: Mapping[str, object],
) -> tuple[Mapping[str, object] | None, tuple[Mapping[str, object], ...]]:
    if outcome_evidence.get("kind") not in {
        "completed-card-replay",
        "completed-card-replay-chain",
    }:
        return None, ()
    replay = result.completed_card_replay
    if replay is None:
        return (
            None,
            (
                {
                    "code": "self-carry-replay-object-missing",
                    "detail": (
                        "executor reported a completed replay without its "
                        "typed logical/native replay"
                    ),
                },
            ),
        )
    if post_action_snapshot is None or not post_action_snapshot.is_file():
        return (
            None,
            (
                {
                    "code": "self-carry-after-snapshot-missing",
                    "detail": "post-action replay LocalSave snapshot is missing",
                },
            ),
        )
    plan = result.plan
    action = plan.get("action") if isinstance(plan, Mapping) else None
    card_guid = action.get("card_guid") if isinstance(action, Mapping) else None
    if not isinstance(card_guid, str) or not card_guid:
        return (
            None,
            (
                {
                    "code": "self-carry-card-guid-missing",
                    "detail": "completed-card replay result has no exact action GUID",
                },
            ),
        )
    transitions: list[dict[str, object]] = []
    if input_bootstrap is None:
        if pre_action_snapshot is None or not pre_action_snapshot.is_file():
            return (
                None,
                (
                    {
                        "code": "self-carry-snapshot-missing",
                        "detail": "pre-action settled LocalSave snapshot is missing",
                    },
                ),
            )
        prior_settled_source = str(pre_action_snapshot.resolve())
    else:
        raw_prior = input_bootstrap.get("prior_settled_source")
        if not isinstance(raw_prior, str) or not raw_prior:
            return (
                None,
                (
                    {
                        "code": "self-carry-prior-source-missing",
                        "detail": "input bootstrap has no stable prior source",
                    },
                ),
            )
        prior_settled_source = raw_prior
        raw_transitions = input_bootstrap.get("transitions")
        if isinstance(raw_transitions, Sequence) and not isinstance(
            raw_transitions, (str, bytes, bytearray)
        ):
            for value in raw_transitions:
                if not isinstance(value, Mapping):
                    return (
                        None,
                        (
                            {
                                "code": "self-carry-transition-invalid",
                                "detail": "bootstrap transition must be an object",
                            },
                        ),
                    )
                transitions.append(dict(value))
        else:
            previous_guid = input_bootstrap.get("completed_card_guid")
            if (
                not isinstance(previous_guid, str)
                or not previous_guid
                or pre_action_snapshot is None
                or not pre_action_snapshot.is_file()
            ):
                return (
                    None,
                    (
                        {
                            "code": "self-carry-legacy-bootstrap-invalid",
                            "detail": (
                                "legacy bootstrap requires its prior GUID and "
                                "current transition snapshot"
                            ),
                        },
                    ),
                )
            transitions.append(
                {
                    "card_guid": previous_guid,
                    "snapshot_source": str(pre_action_snapshot.resolve()),
                }
            )
    transitions.append(
        {
            "card_guid": card_guid,
            "snapshot_source": str(post_action_snapshot.resolve()),
        }
    )
    if len(transitions) != replay.chain_depth:
        return (
            None,
            (
                {
                    "code": "self-carry-chain-depth-mismatch",
                    "detail": (
                        f"archived={len(transitions)};logical={replay.chain_depth}"
                    ),
                },
            ),
        )
    if tuple(str(value.get("card_guid")) for value in transitions) != (
        replay.chain_card_guids
    ):
        return (
            None,
            (
                {
                    "code": "self-carry-chain-guid-mismatch",
                    "detail": "archived GUID order differs from logical replay",
                },
            ),
        )
    return (
        {
            "prior_settled_source": prior_settled_source,
            # Keep the latest GUID at the legacy key for human-readable
            # manifests and older artifact consumers.  Execution authority
            # for a chained replay remains the typed replay plus the ordered
            # ``transitions`` list below.
            "completed_card_guid": card_guid,
            "transitions": transitions,
            "chain_depth": replay.chain_depth,
            "derived_from_step": step_index,
            "evidence": replay.provenance,
        },
        (),
    )


def _advisor_diagnostics(
    label: str,
    payload: Mapping[str, object] | None,
    *,
    terminal: bool,
) -> list[dict[str, object]]:
    # A terminal persisted or screen-proven boundary legitimately has no next
    # advisor action.  Do not turn that into an orchestration failure.
    if label == "after-advisor" and terminal:
        return []
    if payload is None:
        return [{"code": f"{label}-missing", "detail": f"{label} is missing"}]
    diagnostics: list[dict[str, object]] = []
    raw_diagnostics = payload.get("diagnostics")
    if isinstance(raw_diagnostics, Sequence) and not isinstance(
        raw_diagnostics, (str, bytes, bytearray)
    ):
        for value in raw_diagnostics:
            if value:
                diagnostics.append(
                    {
                        "code": f"{label}-diagnostic",
                        "detail": value,
                    }
                )
    raw_issues = payload.get("issues")
    if isinstance(raw_issues, Sequence) and not isinstance(
        raw_issues, (str, bytes, bytearray)
    ):
        for value in raw_issues:
            if value:
                diagnostics.append(
                    {"code": f"{label}-issue", "detail": value}
                )
    if (
        payload.get("status") == STATUS_UNAVAILABLE
        or payload.get("available") is False
    ):
        diagnostics.append(
            {
                "code": f"{label}-unavailable",
                "detail": "advisor is unavailable",
            }
        )
    return diagnostics


def _execution_diagnostics(
    result: Plan3AuditionStepExecutionResult,
    *,
    terminal: bool,
) -> tuple[Mapping[str, object], ...]:
    diagnostics: list[dict[str, object]] = [
        {"code": issue.code, "detail": issue.detail} for issue in result.issues
    ]
    diagnostics.extend(
        _advisor_diagnostics(
            "before-advisor", result.before_advisor, terminal=False
        )
    )
    if result.status == STATUS_EXECUTED:
        diagnostics.extend(
            _advisor_diagnostics(
                "after-advisor", result.after_advisor, terminal=terminal
            )
        )
        if not result.settled_local_save_changed:
            diagnostics.append(
                {
                    "code": "settled-local-save-missing",
                    "detail": "executed step did not report a changed settled LocalSave",
                }
            )
        if result.prediction_actual is None:
            diagnostics.append(
                {
                    "code": "prediction-actual-missing",
                    "detail": "executed step did not produce prediction/actual data",
                }
            )
        if result.input_count < 1:
            diagnostics.append(
                {
                    "code": "semantic-action-input-missing",
                    "detail": "executed step reported no controller input",
                }
            )
    elif result.status == STATUS_PLANNED and result.input_count != 0:
        diagnostics.append(
            {
                "code": "dry-run-input-present",
                "detail": "planned dry-run unexpectedly reported controller input",
            }
        )
    return tuple(diagnostics)


def _failed_execution_result(
    *,
    source: str,
    advisor_mode: str,
    dry_run: bool,
    error: Exception,
    code: str = "step-executor-exception",
) -> Plan3AuditionStepExecutionResult:
    return Plan3AuditionStepExecutionResult(
        status=STATUS_FAILED,
        dry_run=dry_run,
        advisor_mode=advisor_mode,
        source=source,
        input_count=0,
        before_advisor=None,
        plan=None,
        settled_local_save_changed=False,
        after_advisor=None,
        prediction_actual=None,
        ui_trace=(),
        issues=(
            Plan3ExecutionIssue(
                code, f"{type(error).__name__}:{error}"
            ),
        ),
    )


def _stop_reason(
    result: Plan3AuditionStepExecutionResult,
    *,
    dry_run: bool,
    terminal: bool,
    diagnostics: Sequence[Mapping[str, object]],
    action_index: int,
    max_actions: int,
) -> str | None:
    if result.status == STATUS_UNAVAILABLE:
        return STOP_STEP_UNAVAILABLE
    if result.status == STATUS_FAILED:
        return STOP_STEP_FAILED
    if diagnostics:
        return STOP_STEP_DIAGNOSTIC
    if dry_run:
        if result.status == STATUS_PLANNED:
            return STOP_DRY_RUN_ONE_STEP
        return STOP_UNEXPECTED_STEP_STATUS
    if result.status != STATUS_EXECUTED:
        return STOP_UNEXPECTED_STEP_STATUS
    if terminal:
        return STOP_TERMINAL
    if action_index >= max_actions:
        return STOP_MAX_ACTIONS
    return None


def _run_status(*, dry_run: bool, stop_reason: str) -> str:
    if dry_run and stop_reason == STOP_DRY_RUN_ONE_STEP:
        return RUN_STATUS_PLANNED
    if stop_reason in {STOP_TERMINAL, STOP_MAX_ACTIONS}:
        return RUN_STATUS_COMPLETED
    return RUN_STATUS_STOPPED


def _plan3_learned_transition_receipt(
    result: Plan3AuditionStepExecutionResult,
    post_action_snapshot: Path | None,
) -> Mapping[str, object] | None:
    """Bind one executed learned choice to its observed native after-state.

    Learned metadata is emitted by the Plan3 advisor wrapper.  This helper
    only promotes it into the formal orchestration step after the existing
    executor has proved an action and a changed outcome.  It never supplies
    an action or affects a stop reason.
    """

    if result.status != STATUS_EXECUTED:
        return None
    before = result.before_advisor
    search = before.get("search") if isinstance(before, Mapping) else None
    learned = (
        search.get("learned_policy") if isinstance(search, Mapping) else None
    )
    if not isinstance(learned, Mapping) or learned.get("ready") is not True:
        return None
    source = learned.get("source")
    action_id = learned.get("action_id")
    if source not in {"offline_rl", "behavior_cloning"}:
        return None
    if not isinstance(action_id, str) or not action_id:
        return None

    native_state_after: Mapping[str, object] | None = None
    state_after_authority: str | None = None
    if post_action_snapshot is not None and post_action_snapshot.is_file():
        try:
            decoded = decode_plan3_local_save_file(post_action_snapshot)
            if result.completed_card_replay is None:
                from .exact_exam_state_projection import (
                    project_exact_exam_native_state,
                )

                native_state_after = project_exact_exam_native_state(
                    decoded.exam_state.to_dict()
                )
                state_after_authority = "settled-native-exam-save"
            else:
                from .plan3_card_history import (
                    settle_completed_plan3_card_replay_native_state,
                )
                from .plan3_learned_policy_runtime import (
                    project_plan3_replay_learned_state,
                )

                replay = result.completed_card_replay
                native = settle_completed_plan3_card_replay_native_state(replay)
                native_state_after = project_plan3_replay_learned_state(
                    decoded,
                    replay.after,
                    native,
                )
                state_after_authority = "replay-proven-logical-native"
        except (AttributeError, OSError, TypeError, ValueError):
            native_state_after = None

    # Terminal ExamSave removal has no serialized post-action native state.
    # Keep the executor's explicit actual summary rather than fabricating one.
    if native_state_after is None:
        artifact = result.prediction_actual
        actual = artifact.get("actual") if isinstance(artifact, Mapping) else None
        local_save = actual.get("local_save") if isinstance(actual, Mapping) else None
        if isinstance(local_save, Mapping):
            native_state_after = dict(local_save)
            state_after_authority = "executor-actual-summary"

    plan = result.plan
    action = plan.get("action") if isinstance(plan, Mapping) else None
    decision = learned.get("decision")
    boundary_digest = (
        decision.get("boundary_digest")
        if isinstance(decision, Mapping)
        else None
    )
    return {
        "schema": "gkms.plan3-learned-transition-receipt.v1",
        "source": source,
        "confidence": learned.get("confidence"),
        "action_id": action_id,
        "action": dict(action) if isinstance(action, Mapping) else None,
        "policy_id": learned.get("policy_id"),
        "boundary_digest": boundary_digest,
        "native_state_after": (
            None if native_state_after is None else dict(native_state_after)
        ),
        "state_after_authority": state_after_authority,
    }


def run_plan3_exam_orchestrator(
    path: str | Path,
    *,
    advisor_mode: str = MODE_AUTO,
    dry_run: bool = True,
    max_actions: int = 100,
    archive_directory: str | Path | None = None,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.25,
    driver: MaaPlan3ActionDriver | None = None,
    prior_settled_path: str | Path | None = None,
    completed_card_guid: str | None = None,
    executor: StepExecutor = execute_plan3_exam_first_action,
    executor_options: Mapping[str, object] | None = None,
    step_observer: StepObserver | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
    progress_callback: Callable[[Mapping[str, object]], None] | None = None,
    expected_run_id: str | None = None,
) -> Plan3ExamOrchestrationResult:
    """Plan once, or execute settled Plan 3 actions until a hard stop.

    ``executor`` and ``driver`` are injectable so the complete loop can be
    verified offline.  The production default is the existing MAA-backed
    single-step executor; this module itself never talks to a controller.
    """

    from .runtime_command_client import input_backend
    if (driver is None and executor is execute_plan3_exam_first_action
            and input_backend() == "dll"):
        from .runtime_plan3_executor import run_runtime_plan3_orchestrator
        return run_runtime_plan3_orchestrator(path, advisor_mode=advisor_mode, dry_run=dry_run,
            max_actions=max_actions, archive_directory=archive_directory,
            timeout_seconds=timeout_seconds, executor_options=executor_options,
            step_observer=step_observer, stop_requested=stop_requested, progress_callback=progress_callback,
            expected_run_id=expected_run_id)

    if (
        not isinstance(max_actions, int)
        or isinstance(max_actions, bool)
        or max_actions < 1
    ):
        raise ValueError("max_actions must be a positive integer")
    if advisor_mode not in ADVISOR_MODES:
        raise ValueError("advisor_mode must be auto, lesson, or audition")
    if (prior_settled_path is None) != (completed_card_guid is None):
        raise ValueError(
            "prior_settled_path and completed_card_guid must be supplied together"
        )
    if completed_card_guid is not None and (
        not isinstance(completed_card_guid, str) or not completed_card_guid
    ):
        raise ValueError("completed_card_guid must be non-empty text")
    options = {} if executor_options is None else dict(executor_options)
    if step_observer is not None and not callable(step_observer):
        raise TypeError("step_observer must be callable or None")

    source = str(Path(path).resolve())
    bootstrap = (
        None
        if prior_settled_path is None
        else {
            "prior_settled_source": str(Path(prior_settled_path).resolve()),
            "completed_card_guid": completed_card_guid,
        }
    )
    archive = (
        _default_archive_directory()
        if archive_directory is None
        else Path(archive_directory).resolve()
    )
    archive.mkdir(parents=True, exist_ok=False)

    steps: list[Plan3ExamOrchestrationStep] = []
    actions_executed = 0
    terminal_reached = False
    stop_reason: str | None = None

    # A dry-run has no state transition, so its loop bound is always one.
    loop_limit = 1 if dry_run else max_actions
    pending_bootstrap = bootstrap
    pending_replay: Plan3CompletedCardReplay | None = None
    for index in range(1, loop_limit + 1):
        if stop_requested():
            stop_reason = "user-stop-requested"
            break
        step_bootstrap = pending_bootstrap
        step_replay = pending_replay
        snapshot_name: str | None = None
        snapshot_path: Path | None = None
        snapshot_role = "not-required-dry-run"
        snapshot_settled: bool | None = None
        snapshot_error: Exception | None = None
        if not dry_run:
            snapshot_name = f"step-{index:04d}-before.localsave"
            snapshot_path = archive / snapshot_name
            snapshot_role = (
                "ordinary-settled-prior"
                if step_bootstrap is None
                else "transitional-replay-chain-source"
                if step_replay is not None
                else "transitional-bootstrap-source"
            )
            try:
                shutil.copyfile(source, snapshot_path)
                snapshot_settled = is_plan3_exam_actionable_settled(
                    decode_plan3_local_save_file(snapshot_path).exam_state
                )
                if step_bootstrap is None and not snapshot_settled:
                    raise ValueError(
                        "ordinary autonomous input source is not actionable settled"
                    )
            except Exception as error:
                snapshot_error = error

        if snapshot_error is not None:
            execution_result = _failed_execution_result(
                source=source,
                advisor_mode=advisor_mode,
                dry_run=dry_run,
                error=snapshot_error,
                code="pre-action-snapshot-unavailable",
            )
        else:
            try:
                execution_result = executor(
                    source,
                    advisor_mode=advisor_mode,
                    dry_run=dry_run,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    driver=driver,
                    prior_settled_path=(
                        None
                        if step_bootstrap is None or step_replay is not None
                        else str(step_bootstrap["prior_settled_source"])
                    ),
                    completed_card_guid=(
                        None
                        if step_bootstrap is None or step_replay is not None
                        else str(step_bootstrap["completed_card_guid"])
                    ),
                    prior_completed_card_replay=step_replay,
                    **options,
                )
            except Exception as error:
                execution_result = _failed_execution_result(
                    source=source,
                    advisor_mode=advisor_mode,
                    dry_run=dry_run,
                    error=error,
                )

        if execution_result.status == STATUS_EXECUTED:
            actions_executed += 1
        terminal = _actual_terminal(execution_result)
        terminal_reached = terminal_reached or terminal
        outcome_evidence, outcome_diagnostics = _executor_outcome_evidence(
            execution_result
        )
        post_action_snapshot_name: str | None = None
        post_action_snapshot_path: Path | None = None
        post_action_snapshot_diagnostics: tuple[Mapping[str, object], ...] = ()
        if (
            execution_result.status == STATUS_EXECUTED
            and execution_result.settled_local_save_changed
            and Path(source).is_file()
        ):
            post_action_snapshot_name = (
                f"step-{index:04d}-after-replay.localsave"
                if execution_result.completed_card_replay is not None
                else f"step-{index:04d}-after-settled.localsave"
            )
            post_action_snapshot_path = archive / post_action_snapshot_name
            try:
                shutil.copyfile(source, post_action_snapshot_path)
            except Exception as error:
                post_action_snapshot_path = None
                # Replay self-carry requires its snapshot; an ordinary after
                # snapshot is learning telemetry only and cannot stop input.
                if execution_result.completed_card_replay is not None:
                    post_action_snapshot_diagnostics = (
                        {
                            "code": "post-action-replay-snapshot-unavailable",
                            "detail": f"{type(error).__name__}:{error}",
                        },
                    )
        next_bootstrap, carry_diagnostics = _derive_next_bootstrap(
            execution_result,
            step_index=index,
            input_bootstrap=step_bootstrap,
            pre_action_snapshot=snapshot_path,
            post_action_snapshot=post_action_snapshot_path,
            outcome_evidence=outcome_evidence,
        )
        diagnostics = (
            *_execution_diagnostics(execution_result, terminal=terminal),
            *outcome_diagnostics,
            *post_action_snapshot_diagnostics,
            *carry_diagnostics,
        )
        stop_reason = _stop_reason(
            execution_result,
            dry_run=dry_run,
            terminal=terminal,
            diagnostics=diagnostics,
            action_index=index,
            max_actions=max_actions,
        )

        prediction_path: str | None = None
        if execution_result.prediction_actual is not None:
            prediction_name = f"step-{index:04d}-prediction-actual.json"
            _write_json(
                archive / prediction_name,
                execution_result.prediction_actual,
            )
            prediction_path = prediction_name

        learned_transition = _plan3_learned_transition_receipt(
            execution_result,
            post_action_snapshot_path,
        )

        step = Plan3ExamOrchestrationStep(
            index=index,
            execution=execution_result.to_dict(),
            semantic_action_executed=(
                execution_result.status == STATUS_EXECUTED
            ),
            terminal=terminal,
            diagnostics=diagnostics,
            stop_reason_after_step=stop_reason,
            prediction_actual_archive=prediction_path,
            bootstrap=step_bootstrap,
            provenance={
                "pre_action_snapshot_archive": snapshot_name,
                "pre_action_snapshot_role": snapshot_role,
                "pre_action_snapshot_actionable_settled": snapshot_settled,
                "post_action_replay_snapshot_archive": (
                    post_action_snapshot_name
                    if execution_result.completed_card_replay is not None
                    else None
                ),
                "post_action_snapshot_archive": post_action_snapshot_name,
                "outcome": outcome_evidence,
                "next_bootstrap": next_bootstrap,
                "learned_transition": learned_transition,
            },
        )
        steps.append(step)
        _write_json(archive / f"step-{index:04d}.json", step.to_dict())
        if step_observer is not None:
            try:
                step_observer(
                    step,
                    snapshot_path,
                    post_action_snapshot_path,
                )
            except Exception as error:
                # Learning telemetry never owns Plan3 input or stop reasons.
                _write_json(
                    archive / f"step-{index:04d}-observer-error.json",
                    {
                        "schema": "gkms.plan3-step-observer-error.v1",
                        "step": index,
                        "error": f"{type(error).__name__}:{error}",
                    },
                )

        current_manifest = {
            "schema": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "status": "running" if stop_reason is None else _run_status(
                dry_run=dry_run, stop_reason=stop_reason
            ),
            "dry_run": dry_run,
            "advisor_mode": advisor_mode,
            "source": source,
            "max_actions": max_actions,
            "semantic_actions_executed": actions_executed,
            "terminal_reached": terminal_reached,
            "stop_reason": stop_reason,
            "archive_directory": str(archive),
            "bootstrap": bootstrap,
            "steps": [value.to_dict() for value in steps],
        }
        _write_json(archive / "manifest.json", current_manifest)
        if stop_reason is not None:
            break
        pending_bootstrap = next_bootstrap
        pending_replay = execution_result.completed_card_replay

    assert stop_reason is not None
    result = Plan3ExamOrchestrationResult(
        status=_run_status(dry_run=dry_run, stop_reason=stop_reason),
        dry_run=dry_run,
        advisor_mode=advisor_mode,
        source=source,
        max_actions=max_actions,
        semantic_actions_executed=actions_executed,
        terminal_reached=terminal_reached,
        stop_reason=stop_reason,
        archive_directory=str(archive),
        bootstrap=bootstrap,
        steps=tuple(steps),
    )
    _write_json(archive / "manifest.json", result.to_dict())
    return result


def main(
    argv: Sequence[str] | None = None,
    *,
    executor: StepExecutor = execute_plan3_exam_first_action,
    driver: MaaPlan3ActionDriver | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Plan one Plan3 exam action, or explicitly execute settled actions "
            "one at a time through the existing MAA executor."
        )
    )
    parser.add_argument("local_save", type=Path)
    execution_mode = parser.add_mutually_exclusive_group()
    execution_mode.add_argument(
        "--execute",
        action="store_true",
        help="opt in to MAA background input; default plans exactly one step",
    )
    execution_mode.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mode", choices=sorted(ADVISOR_MODES), default=MODE_AUTO
    )
    parser.add_argument("--max-actions", type=int, default=100)
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument(
        "--prior-settled",
        type=Path,
        help=(
            "explicit prior settled LocalSave for exact completed-card "
            "transitional bootstrap"
        ),
    )
    parser.add_argument(
        "--completed-card-guid",
        help="completed card GUID paired with --prior-settled",
    )
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = run_plan3_exam_orchestrator(
            args.local_save,
            advisor_mode=args.mode,
            dry_run=not args.execute,
            max_actions=args.max_actions,
            archive_directory=args.archive_dir,
            timeout_seconds=args.timeout,
            poll_interval_seconds=args.poll_interval,
            driver=driver,
            prior_settled_path=args.prior_settled,
            completed_card_guid=args.completed_card_guid,
            executor=executor,
        )
    except (OSError, TypeError, ValueError) as error:
        parser.error(str(error))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(result.to_json(indent=None if args.compact else 2))
    return 0 if result.status in {RUN_STATUS_PLANNED, RUN_STATUS_COMPLETED} else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_ARCHIVE_ROOT",
    "Plan3ExamOrchestrationResult",
    "Plan3ExamOrchestrationStep",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_PLANNED",
    "RUN_STATUS_STOPPED",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "STEP_SCHEMA_NAME",
    "STOP_DRY_RUN_ONE_STEP",
    "STOP_MAX_ACTIONS",
    "STOP_STEP_DIAGNOSTIC",
    "STOP_STEP_FAILED",
    "STOP_STEP_UNAVAILABLE",
    "STOP_TERMINAL",
    "STOP_UNEXPECTED_STEP_STATUS",
    "StepObserver",
    "main",
    "run_plan3_exam_orchestrator",
]
