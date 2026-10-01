"""Plan3 production execution from DLL snapshots and native settlement.

The ordinary advisor/native simulator still selects actions. The shared
RuntimeExamGateway owns input and settlement, including initial native evidence
identity. The legacy path argument is only a compatibility label; no save read,
screenshot, PNG matcher, encrypted-save write, or save-removal guess is part of
the default loop.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
import uuid

from .audition_local_save_state import AuditionLocalSaveStateEvidence
from .plan3_local_save_bridge import DecodedPlan3LocalSave
from .runtime_exam_executor import RuntimeExamGateway, RuntimeExamOutcome


@dataclass(frozen=True, slots=True)
class RuntimePlan3Payload:
    """Adapter for existing raw-JSON consumers; no LocalSave envelope exists."""
    plaintext: bytes
    save_data_version: None = None
    encrypted_body_size: int = 0
    native_context: Mapping[str, Any] | None = None


def decoded_runtime_plan3(outcome: RuntimeExamOutcome) -> DecodedPlan3LocalSave:
    if outcome.evidence is None or not isinstance(outcome.raw_state, Mapping):
        raise ValueError("DLL Plan3 observation requires matching typed and raw state")
    if outcome.raw_state.get("planType") != 4:
        raise ValueError("native Exam is not Plan3")
    raw_bytes = json.dumps(outcome.raw_state, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    # The gateway is the sole state parser/owner. Check its content binding,
    # rather than running a second independent native-state decoder here.
    if hashlib.sha256(raw_bytes).hexdigest() != outcome.evidence.source_sha256:
        raise ValueError("native raw and typed evidence digest differ")
    payload = RuntimePlan3Payload(raw_bytes, native_context=getattr(outcome, "native_context", None))
    # Existing Plan3 engines consume envelope.plaintext. This native payload
    # explicitly has no disk format version/ciphertext and is never saved as
    # a .localsave file. All game values are the observed DLL raw_state.
    return DecodedPlan3LocalSave(payload, outcome.evidence.state)  # type: ignore[arg-type]


def build_native_plan3_execution_plan(report, decoded):
    """Bind exact policy identities without screen-slot/grid constraints."""
    from .plan3_audition_executor import Plan3AuditionExecutionPlan, _drink_ids
    from .plan3_drink import Plan3DrinkSelectedCardMove, load_plan3_drink
    if not report.available or report.first_action is None or report.issues or report.diagnostics:
        raise ValueError("exact Plan3 policy report is unavailable")
    action, state = dict(report.first_action), decoded.exam_state
    kind = action.get("kind")
    selected = None
    selected_guids = action.get("selected_card_guids", ())
    if not isinstance(selected_guids, (list, tuple)) or len(selected_guids) > 1:
        raise ValueError("multiple secondary GUIDs require an exact ordered native continuation protocol")
    guid = action.get("selected_card_guid")
    if selected_guids:
        if guid is not None and guid != selected_guids[0]:
            raise ValueError("policy secondary GUID fields disagree")
        guid = selected_guids[0]
        action["selected_card_guid"] = guid
    if guid is not None:
        if not isinstance(guid, str) or not guid:
            raise ValueError("policy secondary card GUID is invalid")
        zones = ("deck", "grave") if kind == "drink" else ("hand", "deck", "grave", "lost", "hold")
        found = [(zone, index, card) for zone in zones for index, card in enumerate(getattr(state.zones, zone)) if card.guid == guid]
        if len(found) != 1:
            raise ValueError("policy secondary GUID is not unique in its actual native eligible zones")
        zone, index, card = found[0]
        selected = {"guid": guid, "card_id": card.card_id, "upgrade": card.effective_upgrade, "zone": zone, "zone_index": index}
    if kind == "card":
        index = action.get("hand_index")
        if type(index) is not int or not 0 <= index < len(state.zones.hand):
            raise ValueError("policy hand index is outside native Hand")
        card = state.zones.hand[index]
        if (action.get("card_guid"), action.get("card_id"), action.get("card_upgrade")) != (card.guid, card.card_id, card.effective_upgrade):
            raise ValueError("policy card identity differs from native Hand")
    elif kind == "drink":
        slot, drinks = action.get("drink_slot_index"), _drink_ids(decoded)
        if type(slot) is not int or not 0 <= slot < len(drinks) or drinks[slot] != action.get("drink_id"):
            raise ValueError("policy drink identity differs from native inventory")
        drink = load_plan3_drink(drinks[slot])
        requires_selection = any(isinstance(effect, Plan3DrinkSelectedCardMove) for effect in drink.effects)
        if requires_selection != bool(guid):
            raise ValueError("known native drink selection requirement differs from policy")
    elif kind != "skip" or selected is not None:
        raise ValueError("unsupported exact Plan3 action kind")
    return Plan3AuditionExecutionPlan(kind, action, ("dll-semantic-command",), selected)


class RuntimePlan3Session:
    def __init__(self, path: str | Path, *, gateway: RuntimeExamGateway | None = None,
                 base_evidence: AuditionLocalSaveStateEvidence | None = None,
                 advisor_mode: str = "auto", advisor_factory=None,
                 timeout_seconds: float = 30, prediction_actual_callback=None,
                 stop_requested=lambda: False, progress_callback=None,
                 expected_run_id: str | None = None, evidence_loader=None) -> None:
        self.path = Path(path).resolve()
        self.stop_requested = stop_requested
        self.progress_callback = progress_callback
        self.gateway = gateway or RuntimeExamGateway(timeout=timeout_seconds, cancelled=stop_requested)
        if gateway is not None:
            previous_cancelled = getattr(gateway, "cancelled", None)
            gateway.cancelled = lambda: stop_requested() or bool(previous_cancelled and previous_cancelled())
        self.base = base_evidence if base_evidence is not None else evidence_loader(self.path) if evidence_loader is not None else None
        self.run_id = expected_run_id
        self.advisor_mode = advisor_mode
        self.advisor_factory = advisor_factory
        self.prediction_actual_callback = prediction_actual_callback
        self.last_before: RuntimeExamOutcome | None = None
        self.last_after: RuntimeExamOutcome | None = None
        self.first_evidence: AuditionLocalSaveStateEvidence | None = None

    def _progress(self, evidence, *, action=None, policy_source=None, status="observed") -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback({"source_run_id": evidence.run_id, "current_page": "exam",
                "monitor_snapshot": {"page": "exam", "source": "dll", "state": evidence.state.to_dict(),
                                     "recommendation": action, "policy_source": policy_source,
                                     "native_status": status}})
        except Exception:
            pass  # GUI progress is an observer, never an input owner.

    def step(self, *, dry_run: bool = False):
        from .plan3_audition_executor import (
            Plan3AuditionStepExecutionResult, Plan3ExecutionIssue,
            _default_advisor_factory, _exam_type_advisor_mode, _resolved_advisor_mode,
            is_plan3_exam_terminal,
        )
        from .plan3_training_logger import summarize_local_save
        source = f"dll:{self.path}"
        before_report = None
        plan = None
        mode = self.advisor_mode
        trace = []

        def result(status, *, inputs=0, after_report=None, issues=(), artifact=None,
                   native_changed=False, native_status=None):
            return Plan3AuditionStepExecutionResult(
                status=status, dry_run=dry_run, source=source, input_count=inputs,
                before_advisor=None if before_report is None else before_report.to_dict(),
                plan=None if plan is None else {**plan.to_dict(), "ui_steps": ["dll-semantic-command"]},
                settled_local_save_changed=False,
                after_advisor=None if after_report is None else after_report.to_dict(),
                ui_trace=tuple(trace), issues=tuple(issues), advisor_mode=mode,
                prediction_actual=artifact, settled_native_state_changed=native_changed,
                executor_backend="dll", native_action_status=native_status)

        for _attempt in range(3):
            submission_attempted = False
            try:
                if self.stop_requested():
                    return result("unavailable", native_status="cancelled",
                        issues=(Plan3ExecutionIssue("user-stop-requested", "stopped before native observation/input"),))
                observed = self.gateway.read_native(None, run_id=self.run_id) if self.base is None else self.gateway.read_native(self.base)
                self.last_before = observed
                decoded = decoded_runtime_plan3(observed)
                self.base = observed.evidence
                if self.first_evidence is None:
                    self.first_evidence = observed.evidence
                source = f"dll:{self.base.source_path}"
                self._progress(self.base)
                mode = _resolved_advisor_mode(decoded, self.advisor_mode)
                if mode != _exam_type_advisor_mode(decoded):
                    raise ValueError("advisor mode does not match the current native Exam type")
                if is_plan3_exam_terminal(decoded.exam_state):
                    artifact = {"schema": "gkms.plan3-dll-prediction-actual.v1",
                        "actual": {"terminal": True, "state_source": "dll",
                                   "local_save": summarize_local_save(decoded.exam_state)}}
                    return result("planned" if dry_run else "executed", artifact=artifact,
                                  native_status="already-terminal")
                advisor = self.advisor_factory or _default_advisor_factory(source, mode)
                if self.stop_requested():
                    return result("unavailable", native_status="cancelled",
                        issues=(Plan3ExecutionIssue("user-stop-requested", "stopped before policy evaluation"),))
                before_report = advisor(decoded)
                if not before_report.available or before_report.diagnostics or before_report.issues:
                    issues = tuple(Plan3ExecutionIssue(issue.code, issue.detail) for issue in before_report.issues)
                    if before_report.diagnostics:
                        issues += (Plan3ExecutionIssue("advisor-diagnostics", "advisor diagnostics block DLL input"),)
                    return result("unavailable", issues=issues, native_status="not-submitted")
                plan = build_native_plan3_execution_plan(before_report, decoded)
                if plan.action.get("selected_card_guid") and "exam.continuation" not in self.gateway.client.read_status().get("capabilities", ()):
                    return result("unavailable", issues=(Plan3ExecutionIssue("native-secondary-selection-capability-required",
                                  "the exact secondary policy GUID requires the native continuation capability"),),
                                  native_status="not-submitted")
                if dry_run:
                    return result("planned", native_status="not-submitted")
                kind = {"card": "play", "drink": "drink", "skip": "end_turn"}[plan.kind]
                selected = {"kind": kind, "card_guid": plan.action.get("card_guid"),
                            "slot": plan.action.get("drink_slot_index"), "drink_id": plan.action.get("drink_id")}
                search = before_report.search if isinstance(before_report.search, Mapping) else {}
                learned = search.get("learned_policy", {})
                self._progress(observed.evidence, action=selected,
                    policy_source=learned.get("source") if isinstance(learned, Mapping) else None)
                if self.stop_requested():
                    return result("unavailable", native_status="cancelled",
                        issues=(Plan3ExecutionIssue("user-stop-requested", "stopped after policy evaluation, before native input"),))
                submission_attempted = True
                outcome = self.gateway.execute(kind, observed.evidence,
                    card_guid=plan.action.get("card_guid"), slot=plan.action.get("drink_slot_index"),
                    drink_id=plan.action.get("drink_id"),
                    **({"preferred_selection_guid": plan.action["selected_card_guid"]} if plan.action.get("selected_card_guid") else {}))
            except Exception as error:
                if submission_attempted:
                    trace.append({"step": "dll-semantic-command", "status": "unknown",
                                  "detail": "gateway raised while command outcome may be pending; no retry"})
                    return result("failed", issues=(Plan3ExecutionIssue(
                        "dll-command-outcome-unknown", f"{type(error).__name__}:{error}"),), native_status="pending")
                return result("unavailable", issues=(Plan3ExecutionIssue(
                    "dll-observation-or-plan-unavailable", f"{type(error).__name__}:{error}"),), native_status="not-submitted")
            trace.append({"step": "dll-semantic-command", "status": outcome.status,
                          "submitted": outcome.submitted, "request_id": outcome.request_id,
                          "detail": outcome.detail})
            if outcome.status == "replan":
                if outcome.evidence is not None:
                    self.base = outcome.evidence
                continue
            if outcome.status != "settled":
                return result("failed" if outcome.submitted or outcome.status == "pending" else "unavailable",
                    inputs=int(outcome.submitted), native_status=outcome.status,
                    issues=(Plan3ExecutionIssue(f"dll-{outcome.status}", outcome.detail),))
            self.last_after = outcome
            try:
                after = decoded_runtime_plan3(outcome)
                self.base = outcome.evidence
                self._progress(self.base, status="settled")
                terminal = is_plan3_exam_terminal(after.exam_state)
                native_changed = after.exam_state != decoded.exam_state
                summary = {**summarize_local_save(after.exam_state),
                    "exam_type": after.exam_state.exam_type,
                    "executor_actionable_settled": after.exam_state.is_native_actionable_settled,
                    "executor_terminal": terminal, "executor_completed_card_replay": False,
                    "executor_completed_card_replay_provenance": None,
                    "state_source": "runtime-command-native-state"}
                artifact = {"schema": "gkms.plan3-dll-prediction-actual.v1", "advisor_mode": mode,
                    "action": {"kind": plan.kind, "action": dict(plan.action)},
                    "prediction": {"current_state": before_report.current_state,
                                   "terminal": before_report.terminal},
                    "actual": {"terminal": terminal, "state_source": "dll", "local_save": summary,
                               "native_state": dict(outcome.raw_state)},
                    "native_context_before": getattr(observed, "native_context", None),
                    "native_context_after": getattr(outcome, "native_context", None),
                    "runtime_provenance": None if outcome.provenance is None else outcome.provenance.to_dict(),
                    "request_id": outcome.request_id}
                if self.prediction_actual_callback is not None:
                    try:
                        self.prediction_actual_callback(artifact)
                    except Exception as error:
                        trace.append({"step": "prediction-observer-error", "detail": f"{type(error).__name__}:{error}"})
                issues = ()
                if not native_changed and not terminal:
                    issues = (Plan3ExecutionIssue("dll-settled-state-unchanged", "native action receipt has no changed state; stop before another input"),)
                return result("executed", inputs=1, artifact=artifact,
                              issues=issues, native_changed=native_changed, native_status="settled")
            except Exception as error:
                # Once the gateway proves submission, projection/replanning
                # failures must retain that fact. They cannot become a retry.
                return result("failed", inputs=1, native_status="settled-projection-failed",
                    issues=(Plan3ExecutionIssue("dll-after-state-unavailable", f"{type(error).__name__}:{error}"),))
        return result("unavailable", native_status="replan-limit",
                      issues=(Plan3ExecutionIssue("dll-replan-limit", "native state kept advancing before input"),))


def execute_runtime_plan3_first_action(path: str | Path, *, advisor_mode="auto", dry_run=False,
                                      timeout_seconds=30, advisor_factory=None,
                                      prediction_actual_callback=None, gateway=None,
                                      base_evidence=None, stop_requested=lambda: False,
                                      progress_callback=None, expected_run_id=None, evidence_loader=None, **_legacy_options):
    from .plan3_audition_executor import Plan3AuditionStepExecutionResult, Plan3ExecutionIssue
    try:
        session = RuntimePlan3Session(path, gateway=gateway, base_evidence=base_evidence,
            advisor_mode=advisor_mode, advisor_factory=advisor_factory,
            timeout_seconds=timeout_seconds, prediction_actual_callback=prediction_actual_callback,
            stop_requested=stop_requested, progress_callback=progress_callback,
            expected_run_id=expected_run_id, evidence_loader=evidence_loader)
        return session.step(dry_run=dry_run)
    except Exception as error:
        return Plan3AuditionStepExecutionResult("unavailable", dry_run, f"dll:{Path(path).resolve()}",
            0, None, None, False, None, (),
            (Plan3ExecutionIssue("dll-initial-identity-unavailable", f"{type(error).__name__}:{error}"),),
            advisor_mode=advisor_mode, executor_backend="dll", native_action_status="not-submitted")


def run_runtime_plan3_orchestrator(path: str | Path, *, advisor_mode="auto", dry_run=False,
                                   max_actions=100, archive_directory=None, timeout_seconds=30,
                                   executor_options=None, step_observer=None,
                                   gateway=None, base_evidence=None, stop_requested=lambda: False,
                                   progress_callback=None, expected_run_id=None, evidence_loader=None, **_legacy_options):
    from .plan3_exam_orchestrator import (
        DEFAULT_ARCHIVE_ROOT, Plan3ExamOrchestrationResult, Plan3ExamOrchestrationStep,
        _plan3_learned_transition_receipt,
    )
    if type(max_actions) is not int or max_actions < 1:
        raise ValueError("max_actions must be a positive integer")
    options = dict(executor_options or {})
    archive = Path(archive_directory).resolve() if archive_directory is not None else DEFAULT_ARCHIVE_ROOT / f"dll-{uuid.uuid4().hex}"
    archive.mkdir(parents=True, exist_ok=False)
    def write(name, value):
        (archive / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    session = RuntimePlan3Session(path, gateway=gateway, base_evidence=base_evidence,
        advisor_mode=advisor_mode, advisor_factory=options.get("advisor_factory"),
        timeout_seconds=timeout_seconds, prediction_actual_callback=options.get("prediction_actual_callback"),
        stop_requested=stop_requested, progress_callback=progress_callback,
        expected_run_id=expected_run_id, evidence_loader=evidence_loader)
    native_capture = None
    if not dry_run:
        try:
            from .native_runtime_stage_capture import begin_native_runtime_stage_capture
            native_capture = begin_native_runtime_stage_capture(session.gateway.client.read_status()["pid"])
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            pass  # Archival is observational and cannot block gameplay.
    steps = []
    actions = 0
    terminal = False
    stop = "max-actions-reached"
    for index in range(1, (1 if dry_run else max_actions) + 1):
        if stop_requested():
            stop = "user-stop-requested"
            break
        session.last_before = session.last_after = None
        execution = session.step(dry_run=dry_run)
        actual = (execution.prediction_actual or {}).get("actual", {})
        terminal = actual.get("terminal") is True
        counted = execution.status == "executed" and execution.input_count > 0
        actions += int(counted)
        diagnostics = [issue.to_dict() for issue in execution.issues]
        stop_after = None
        if stop_requested() and not terminal: stop_after = "user-stop-requested"
        elif execution.status == "unavailable": stop_after = "step-unavailable"
        elif execution.status == "failed": stop_after = "step-failed"
        elif diagnostics: stop_after = "step-diagnostic"
        elif terminal: stop_after = "terminal"
        elif dry_run: stop_after = "dry-run-one-step"
        elif not counted: stop_after = "unexpected-step-status"
        elif not execution.settled_native_state_changed: stop_after = "native-state-unchanged"
        elif index == max_actions: stop_after = "max-actions-reached"
        before_name = after_name = None
        for label, observation in (("before", session.last_before), ("after", session.last_after)):
            if observation is not None and observation.raw_state is not None:
                name = f"step-{index:04d}-{label}-native.json"
                write(name, observation.raw_state)
                if label == "before": before_name = name
                else: after_name = name
        prediction_name = None
        if execution.prediction_actual is not None:
            prediction_name = f"step-{index:04d}-prediction-actual.json"
            write(prediction_name, execution.prediction_actual)
        learned = _plan3_learned_transition_receipt(execution, None)
        if learned is not None and session.last_after is not None:
            from .exact_exam_state_projection import project_exact_exam_native_state
            learned = {**dict(learned), "native_state_after": project_exact_exam_native_state(session.last_after.evidence.state.to_dict()),
                       "state_after_authority": "runtime-command-native-settlement"}
        step = Plan3ExamOrchestrationStep(index, execution.to_dict(), counted, terminal,
            tuple(diagnostics), stop_after, prediction_name, None,
            {"executor": "dll", "native_before_archive": before_name, "native_after_archive": after_name,
             "pre_action_snapshot_archive": None, "post_action_snapshot_archive": None,
             "outcome": {"kind": "native", "status": execution.native_action_status},
             "learned_transition": learned})
        steps.append(step)
        write(f"step-{index:04d}.json", step.to_dict())
        if step_observer is not None:
            try:
                if (hasattr(step_observer, "observe_native") and session.last_before is not None
                        and session.last_after is not None):
                    step_observer.observe_native(step, decoded_runtime_plan3(session.last_before),
                                                  decoded_runtime_plan3(session.last_after))
                else:
                    step_observer(step, None, None)
            except Exception as error:
                write(f"step-{index:04d}-observer-error.json", {"error": f"{type(error).__name__}:{error}"})
        if stop_after is not None:
            stop = stop_after
            break
    status = "planned" if dry_run and stop == "dry-run-one-step" else "completed" if stop == "terminal" else "stopped"
    capture_payload = None
    if native_capture is not None:
        try:
            capture_payload = native_capture.finish().to_dict()
            first = session.first_evidence
            if first is not None:
                capture_payload["stage_binding"] = {"source_run_id": first.run_id,
                    "session_transition_id": first.session_transition_id,
                    "step_context_digest": first.step_context_digest, "step_type_value": first.state.step_type_value}
        except (OSError, TypeError, ValueError) as error:
            capture_payload = {**native_capture.to_dict(), "capture_error": f"{type(error).__name__}:{error}"}
    result = Plan3ExamOrchestrationResult(status, dry_run, advisor_mode, f"dll:{Path(path).resolve()}",
        max_actions, actions, terminal, stop, str(archive), None, tuple(steps), capture_payload,
        session.base.run_id if session.base is not None else expected_run_id)
    write("manifest.json", result.to_dict())
    return result
